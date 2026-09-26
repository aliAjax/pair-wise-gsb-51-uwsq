"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
import calendar
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'pay': {'servicer'},
    'evaluate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
}
# pay/evaluate的目标状态由履约数据决定，元组列出全部可能结果。
TRANSITIONS = {
    'assess': {'submitted': 'assessed'},
    'approve': {'assessed': 'approved'},
    'activate': {'approved': 'active'},
    'pay': {'active': ('active', 'cured')},
    'evaluate': {'active': ('active', 'defaulted')},
    'cure': {'active': 'cured'},
    'default': {'active': 'defaulted'},
}
DEFAULT_GRACE_DAYS = 10
MONEY_EPS = 0.004


def _money(value: float) -> float:
    return round(float(value) + 0.0, 2)


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValidationError("%s必须是YYYY-MM-DD日期" % field)
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % field) from exc


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _outstanding(schedule: List[Dict[str, Any]]) -> float:
    return _money(sum(_money(item["due_amount"] - item["paid_amount"]) for item in schedule))


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

    def require_transition(self, record: Dict[str, Any], action: str):
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if not allowed:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def _build_schedule(self, p: Dict[str, Any], data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
        months = int(p["approved_months"])
        per_installment = _money(p["approved_payment"])
        grace_days = integer(data, "grace_days", 0, 90) if "grace_days" in data else DEFAULT_GRACE_DAYS
        first_due = _parse_date(data["first_due_date"], "first_due_date") if data.get("first_due_date") else _add_months(date.today(), 1)
        schedule = []
        for i in range(months):
            due = _add_months(first_due, i)
            schedule.append({
                "period": i + 1,
                "due_amount": per_installment,
                "paid_amount": 0.0,
                "due_date": due.isoformat(),
                "grace_deadline": (due + timedelta(days=grace_days)).isoformat(),
                "status": "pending",
            })
        return schedule, grace_days

    def _apply_payment(self, p: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        schedule = [dict(item) for item in p.get("schedule", [])]
        if not schedule:
            raise Conflict("方案尚未生成分期履约计划")
        payments = list(p.get("payments", []))
        amount = _money(number(data, "amount", 0))
        if amount <= 0:
            raise ValidationError("还款金额必须大于0")
        total_outstanding = _outstanding(schedule)
        if amount > total_outstanding + MONEY_EPS:
            raise ValidationError("还款金额超过剩余应还%.2f" % total_outstanding)
        paid_at = _parse_date(data["paid_at"], "paid_at") if data.get("paid_at") else date.today()

        remaining = amount
        allocations = []
        # 还款按时间顺序冲抵最早未结清的期次，允许跨期提前补缴。
        for item in schedule:
            if remaining <= MONEY_EPS:
                break
            owed = _money(item["due_amount"] - item["paid_amount"])
            if owed <= 0:
                continue
            take = _money(min(owed, remaining))
            item["paid_amount"] = _money(item["paid_amount"] + take)
            item["status"] = "paid" if item["paid_amount"] >= item["due_amount"] - MONEY_EPS else "partial"
            allocations.append({"period": item["period"], "amount": take})
            remaining = _money(remaining - take)

        payment = {"id": len(payments) + 1, "amount": amount, "paid_at": paid_at.isoformat(), "allocations": allocations}
        payments.append(payment)
        changes = {"schedule": schedule, "payments": payments}
        unpaid_periods = [item["period"] for item in schedule if item["status"] != "paid"]
        if not unpaid_periods:
            new_state = "cured"
            changes["cured_by_payment"] = payment["id"]
            summary = "还款#%s结清全部剩余期款，贷款恢复正常" % payment["id"]
        else:
            new_state = "active"
            summary = "收到还款#%s %.2f元，第%s期仍有欠款，方案保持生效" % (
                payment["id"], amount, ",".join(str(n) for n in unpaid_periods),
            )
        return new_state, changes, summary

    def _evaluate(self, p: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        schedule = p.get("schedule", [])
        if not schedule:
            raise Conflict("方案尚未生成分期履约计划")
        as_of = _parse_date(data["as_of"], "as_of") if data.get("as_of") else date.today()
        run: List[int] = []
        longest: List[int] = []
        for item in schedule:
            owed = _money(item["due_amount"] - item["paid_amount"])
            # 宽限截止日次日仍未补足，才算错过该期宽限。
            if owed > 0 and date.fromisoformat(item["grace_deadline"]) < as_of:
                run.append(item["period"])
                if len(run) > len(longest):
                    longest = list(run)
            else:
                run = []
        changes = {"last_evaluated_at": as_of.isoformat()}
        if len(longest) >= 2:
            new_state = "defaulted"
            changes["default_reason"] = "连续%s期宽限截止仍未补足（第%s期）" % (
                len(longest), ",".join(str(n) for n in longest),
            )
            changes["defaulted_periods"] = longest
            summary = "第%s期连续%s个宽限期末未补足，方案转违约" % (
                ",".join(str(n) for n in longest), len(longest),
            )
        else:
            new_state = "active"
            if longest:
                summary = "截至%s有1期宽限截止未补足，尚未达到连续两期，方案保持生效" % as_of.isoformat()
            else:
                summary = "截至%s宽限期内欠款允许补足，方案保持生效" % as_of.isoformat()
        return new_state, changes, summary

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        allowed = self.require_transition(record, action)
        new_state = allowed if isinstance(allowed, str) else ""
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
            schedule, grace_days = self._build_schedule(p, data)
            changes["schedule"] = schedule
            changes["payments"] = []
            changes["grace_days"] = grace_days
            summary = "纾困方案生效，共%s期分期履约，每期应还%.2f元" % (len(schedule), schedule[0]["due_amount"])
        elif action == "pay":
            new_state, changes, summary = self._apply_payment(p, data)
        elif action == "evaluate":
            new_state, changes, summary = self._evaluate(p, data)
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
