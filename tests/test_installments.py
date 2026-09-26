import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 4}
ACTIVATE_DATA = {'borrower_ack': True, 'first_due_date': '2026-01-01', 'grace_days': 10}
SERVICER = Actor('servicer-1', 'servicer')


class InstallmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor('creator', 'intake_officer'), 'MORT-INST-1', CREATE_DATA)
        record = self.service.act(Actor('op', 'intake_officer'), record['id'], record['version'], 'assess', {'assessment_note': '收入波动'})
        record = self.service.act(Actor('op', 'underwriter'), record['id'], record['version'], 'approve', {'exception_approved': False})
        self.record = self.service.act(SERVICER, record['id'], record['version'], 'activate', ACTIVATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def pay(self, amount, paid_at):
        self.record = self.service.act(SERVICER, self.record['id'], self.record['version'], 'pay', {'amount': amount, 'paid_at': paid_at})
        return self.record

    def evaluate(self, as_of):
        self.record = self.service.act(SERVICER, self.record['id'], self.record['version'], 'evaluate', {'as_of': as_of})
        return self.record

    def test_activate_generates_schedule(self):
        payload = self.record['payload']
        schedule = payload['schedule']
        self.assertEqual(self.record['state'], 'active')
        self.assertEqual(len(schedule), 4)
        self.assertEqual(schedule[0]['due_date'], '2026-01-01')
        self.assertEqual(schedule[0]['grace_deadline'], '2026-01-11')
        self.assertEqual(schedule[1]['due_date'], '2026-02-01')
        self.assertTrue(all(item['status'] == 'pending' and item['paid_amount'] == 0.0 for item in schedule))
        self.assertTrue(all(item['due_amount'] == payload['approved_payment'] for item in schedule))

    def test_partial_payment_keeps_active_and_allocates_in_order(self):
        due = self.record['payload']['schedule'][0]['due_amount']
        record = self.pay(due * 1.5, '2026-01-05')
        schedule = record['payload']['schedule']
        self.assertEqual(record['state'], 'active')
        self.assertEqual(schedule[0]['status'], 'paid')
        self.assertEqual(schedule[1]['status'], 'partial')
        self.assertEqual(schedule[1]['paid_amount'], round(due * 0.5, 2))
        payment = record['payload']['payments'][0]
        self.assertEqual(payment['allocations'][0]['period'], 1)
        self.assertEqual(payment['allocations'][1]['period'], 2)

    def test_overpayment_rejected(self):
        total = sum(item['due_amount'] for item in self.record['payload']['schedule'])
        with self.assertRaises(ValidationError):
            self.pay(total + 1, '2026-01-05')

    def test_pay_off_remaining_triggers_cure(self):
        total = sum(item['due_amount'] for item in self.record['payload']['schedule'])
        record = self.pay(total, '2026-01-20')
        self.assertEqual(record['state'], 'cured')
        self.assertEqual(record['payload']['cured_by_payment'], 1)
        timeline = self.service.timeline(SERVICER, record['id'])
        last = timeline[-1]
        self.assertEqual(last['action'], 'pay')
        self.assertEqual(last['details']['to'], 'cured')
        self.assertIn('恢复正常', last['details']['summary'])

    def test_single_missed_grace_stays_active(self):
        self.evaluate('2026-01-12')
        self.assertEqual(self.record['state'], 'active')
        self.evaluate('2026-02-11')
        self.assertEqual(self.record['state'], 'active')

    def test_two_consecutive_missed_grace_default(self):
        record = self.evaluate('2026-02-12')
        self.assertEqual(record['state'], 'defaulted')
        self.assertEqual(record['payload']['defaulted_periods'], [1, 2])
        timeline = self.service.timeline(SERVICER, record['id'])
        last = timeline[-1]
        self.assertEqual(last['action'], 'evaluate')
        self.assertEqual(last['details']['to'], 'defaulted')

    def test_catch_up_payment_breaks_consecutive_run(self):
        due = self.record['payload']['schedule'][0]['due_amount']
        self.pay(due, '2026-01-05')
        # 第3期宽限截止日当天评估：仅第2期错过宽限，连续数不足两期，保持生效。
        record = self.evaluate('2026-03-11')
        self.assertEqual(record['state'], 'active')
        # 第3期宽限也结束后：第2、3期连续未补足，转违约。
        record = self.evaluate('2026-03-12')
        self.assertEqual(record['state'], 'defaulted')
        self.assertEqual(record['payload']['defaulted_periods'], [2, 3])

    def test_pay_not_allowed_before_activation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = build_service(str(Path(temp.name) / 'other.db'))
        record = service.create(Actor('creator', 'intake_officer'), 'MORT-INST-2', CREATE_DATA)
        with self.assertRaises(Conflict):
            service.act(SERVICER, record['id'], record['version'], 'pay', {'amount': 100.0})
