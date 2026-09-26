import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 3}
# 批准后每期应还 3400.0（7000 - (18000-9000)*0.4），共 3 期


class PerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _activate(self, reference="MORT-P1"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
        record = self.service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True, "first_due_date": "2026-01-10", "grace_days": 5})
        return record

    def _pay(self, record, amount, paid_at):
        return self.service.act(Actor("op", "servicer"), record["id"], record["version"], "payment", {"amount": amount, "paid_at": paid_at})

    def _evaluate(self, record, as_of):
        return self.service.act(Actor("op", "servicer"), record["id"], record["version"], "evaluate", {"as_of": as_of})

    def test_activate_generates_schedule(self):
        record = self._activate()
        schedule = record["payload"]["schedule"]
        self.assertEqual(len(schedule), 3)
        self.assertEqual(schedule[0]["due_date"], "2026-01-10")
        self.assertEqual(schedule[0]["grace_deadline"], "2026-01-15")
        self.assertEqual(schedule[1]["due_date"], "2026-02-10")
        self.assertEqual(schedule[2]["grace_deadline"], "2026-03-15")
        for period in schedule:
            self.assertEqual(period["due_amount"], 3400.0)
            self.assertEqual(period["paid_amount"], 0.0)
        self.assertEqual(record["payload"]["payments"], [])

    def test_partial_payment_keeps_active(self):
        record = self._activate()
        record = self._pay(record, 1000.0, "2026-01-12")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["schedule"][0]["paid_amount"], 1000.0)
        payment = record["payload"]["payments"][0]
        self.assertEqual(payment["seq"], 1)
        self.assertEqual(payment["allocations"], [{"period": 1, "amount": 1000.0}])
        self.assertIsNone(payment["trigger"])
        self.assertEqual(record["payload"]["last_evaluation"]["result"], "active")

    def test_early_settlement_triggers_cure(self):
        record = self._activate()
        record = self._pay(record, 10200.0, "2026-01-12")
        self.assertEqual(record["state"], "cured")
        payment = record["payload"]["payments"][0]
        self.assertEqual(payment["trigger"], "cured")
        self.assertEqual(len(payment["allocations"]), 3)
        timeline = self.service.timeline(Actor("op", "servicer"), record["id"])
        last = timeline[-1]
        self.assertEqual(last["action"], "payment")
        self.assertEqual(last["details"]["to"], "cured")
        self.assertEqual(last["details"]["payment"]["seq"], 1)

    def test_installments_cure_on_final_payment(self):
        record = self._activate()
        record = self._pay(record, 3400.0, "2026-01-10")
        record = self._pay(record, 3400.0, "2026-02-10")
        self.assertEqual(record["state"], "active")
        self.assertIsNone(record["payload"]["payments"][1]["trigger"])
        record = self._pay(record, 3400.0, "2026-03-10")
        self.assertEqual(record["state"], "cured")
        self.assertEqual(record["payload"]["payments"][2]["trigger"], "cured")

    def test_single_missed_grace_keeps_active(self):
        record = self._activate()
        record = self._evaluate(record, "2026-01-20")
        self.assertEqual(record["state"], "active")
        evaluation = record["payload"]["last_evaluation"]
        self.assertEqual(evaluation["missed_streak"], 1)
        self.assertEqual(evaluation["outstanding_total"], 10200.0)

    def test_two_consecutive_missed_grace_defaults(self):
        record = self._activate()
        record = self._evaluate(record, "2026-02-20")
        self.assertEqual(record["state"], "defaulted")
        self.assertEqual(record["payload"]["last_evaluation"]["missed_streak"], 2)
        timeline = self.service.timeline(Actor("op", "servicer"), record["id"])
        self.assertEqual(timeline[-1]["action"], "evaluate")
        self.assertEqual(timeline[-1]["details"]["to"], "defaulted")

    def test_makeup_payment_resets_streak(self):
        record = self._activate()
        record = self._evaluate(record, "2026-01-20")
        self.assertEqual(record["state"], "active")
        record = self._pay(record, 3400.0, "2026-01-21")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["last_evaluation"]["missed_streak"], 0)
        record = self._evaluate(record, "2026-02-20")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["last_evaluation"]["missed_streak"], 1)
        record = self._evaluate(record, "2026-03-20")
        self.assertEqual(record["state"], "defaulted")

    def test_small_partial_payment_can_trigger_default(self):
        record = self._activate()
        record = self._pay(record, 100.0, "2026-02-20")
        self.assertEqual(record["state"], "defaulted")
        payment = record["payload"]["payments"][0]
        self.assertEqual(payment["trigger"], "defaulted")
        self.assertEqual(payment["allocations"], [{"period": 1, "amount": 100.0}])

    def test_payment_audited_without_state_change(self):
        record = self._activate()
        record = self._pay(record, 1000.0, "2026-01-12")
        timeline = self.service.timeline(Actor("op", "servicer"), record["id"])
        last = timeline[-1]
        self.assertEqual(last["action"], "payment")
        self.assertEqual(last["details"]["from"], "active")
        self.assertEqual(last["details"]["to"], "active")
        self.assertEqual(last["details"]["payment"]["amount"], 1000.0)
        self.assertEqual(last["details"]["evaluation"]["result"], "active")

    def test_overpayment_rejected(self):
        record = self._activate()
        with self.assertRaises(ValidationError):
            self._pay(record, 10200.01, "2026-01-12")

    def test_payment_validation_and_guards(self):
        record = self._activate()
        with self.assertRaises(ValidationError):
            self._pay(record, 0, "2026-01-12")
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "payment", {"amount": 100.0})
        record = self._pay(record, 10200.0, "2026-01-12")
        with self.assertRaises(Conflict):
            self._pay(record, 100.0, "2026-01-13")
