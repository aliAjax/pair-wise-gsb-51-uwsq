"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}, 'payment': {'servicer'}, 'evaluate': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}, 'payment': {'active': 'active'}, 'evaluate': {'active': 'active'}}

DEFAULT_GRACE_DAYS = 15
MAX_GRACE_DAYS = 90
CONSECUTIVE_MISSES_FOR_DEFAULT = 2


def _parse_date(value: Any, key: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc


def _parse_moment(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是ISO日期或时间" % key)
    raw = value.strip()
    try:
        moment = datetime.strptime(raw, "%Y-%m-%d") if len(raw) == 10 else datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO日期或时间" % key) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _outstanding(period: Dict[str, Any]) -> float:
    return round(float(period["due_amount"]) - float(period["paid_amount"]), 2)


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def build_schedule(self, months: int, payment: float, first_due: date, grace_days: int) -> List[Dict[str, Any]]:
        schedule = []
        for offset in range(months):
            due_date = _add_months(first_due, offset)
            schedule.append({
                "period": offset + 1,
                "due_date": due_date.isoformat(),
                "grace_deadline": (due_date + timedelta(days=grace_days)).isoformat(),
                "due_amount": round(float(payment), 2),
                "paid_amount": 0.0,
            })
        return schedule

    def allocate_payment(self, schedule: List[Dict[str, Any]], amount: float) -> List[Dict[str, Any]]:
        total = round(sum(max(_outstanding(period), 0.0) for period in schedule), 2)
        if round(amount - total, 2) > 0:
            raise ValidationError("还款金额超出剩余应还总额%.2f" % total)
        remaining = round(amount, 2)
        allocations = []
        for period in schedule:
            if remaining <= 0:
                break
            outstanding = _outstanding(period)
            if outstanding <= 0:
                continue
            take = min(outstanding, remaining)
            period["paid_amount"] = round(float(period["paid_amount"]) + take, 2)
            allocations.append({"period": int(period["period"]), "amount": take})
            remaining = round(remaining - take, 2)
        return allocations

    def evaluate_schedule(self, schedule: List[Dict[str, Any]], as_of: date) -> Dict[str, Any]:
        streak = 0
        worst_streak = 0
        outstanding_total = 0.0
        for period in schedule:
            outstanding = _outstanding(period)
            if outstanding > 0:
                outstanding_total = round(outstanding_total + outstanding, 2)
            deadline = _parse_date(period["grace_deadline"], "grace_deadline")
            if outstanding > 0 and as_of > deadline:
                streak += 1
                worst_streak = max(worst_streak, streak)
            else:
                streak = 0
        all_paid = all(_outstanding(period) <= 0 for period in schedule)
        if all_paid:
            result = "cured"
        elif worst_streak >= CONSECUTIVE_MISSES_FOR_DEFAULT:
            result = "defaulted"
        else:
            result = "active"
        return {"as_of": as_of.isoformat(), "result": result, "missed_streak": worst_streak, "outstanding_total": outstanding_total, "all_paid": all_paid}

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            first_due_text = optional_text(data, "first_due_date")
            first_due = _parse_date(first_due_text, "first_due_date") if first_due_text else datetime.now(timezone.utc).date()
            grace_days = integer(data, "grace_days", 0, MAX_GRACE_DAYS) if "grace_days" in data else DEFAULT_GRACE_DAYS
            changes["grace_days"] = grace_days
            changes["schedule"] = self.build_schedule(int(p["approved_months"]), float(p["approved_payment"]), first_due, grace_days)
            changes["payments"] = []
            changes["last_evaluation"] = None
            summary = "纾困方案生效，生成%s期还款计划" % int(p["approved_months"])
        elif action == "payment":
            new_state, changes, summary = self._apply_payment(p, data)
        elif action == "evaluate":
            new_state, changes, summary = self._apply_evaluation(p, data)
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def _apply_payment(self, payload: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        amount = round(number(data, "amount", 0.01), 2)
        paid_at_text = optional_text(data, "paid_at")
        paid_at = _parse_moment(paid_at_text, "paid_at") if paid_at_text else datetime.now(timezone.utc)
        schedule = [dict(period) for period in payload.get("schedule") or []]
        if not schedule:
            raise Conflict("缺少还款计划，无法登记还款")
        allocations = self.allocate_payment(schedule, amount)
        evaluation = self.evaluate_schedule(schedule, paid_at.date())
        payments = list(payload.get("payments") or [])
        payments.append({
            "seq": len(payments) + 1,
            "amount": amount,
            "paid_at": paid_at.isoformat(),
            "allocations": allocations,
            "missed_streak": evaluation["missed_streak"],
            "outstanding_total": evaluation["outstanding_total"],
            "state_after": evaluation["result"],
            "trigger": None if evaluation["result"] == "active" else evaluation["result"],
        })
        targets = "、".join("第%s期" % item["period"] for item in allocations)
        if evaluation["result"] == "cured":
            summary = "登记还款%.2f元（%s），剩余期数全部结清，贷款恢复正常" % (amount, targets)
        elif evaluation["result"] == "defaulted":
            summary = "登记还款%.2f元（%s），连续两个宽限期末未补足，纾困方案违约" % (amount, targets)
        else:
            summary = "登记还款%.2f元（%s），方案保持生效" % (amount, targets)
        return evaluation["result"], {"schedule": schedule, "payments": payments, "last_evaluation": evaluation}, summary

    def _apply_evaluation(self, payload: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        as_of_text = optional_text(data, "as_of")
        as_of = _parse_moment(as_of_text, "as_of") if as_of_text else datetime.now(timezone.utc)
        schedule = payload.get("schedule") or []
        if not schedule:
            raise Conflict("缺少还款计划，无法评估履约")
        evaluation = self.evaluate_schedule(schedule, as_of.date())
        if evaluation["result"] == "cured":
            summary = "履约评估：剩余期数全部结清，贷款恢复正常"
        elif evaluation["result"] == "defaulted":
            summary = "履约评估：连续两个宽限期末未补足，纾困方案违约"
        elif evaluation["missed_streak"]:
            summary = "履约评估：未结清%.2f元，连续错过宽限%s期，方案保持生效" % (evaluation["outstanding_total"], evaluation["missed_streak"])
        else:
            summary = "履约评估：未结清%.2f元，均在宽限期内，方案保持生效" % evaluation["outstanding_total"]
        return evaluation["result"], {"last_evaluation": evaluation}, summary
