"""Проверки расчета. Запуск: python -m unittest test_report.py"""
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from report import build_report, load

DATA = Path(__file__).parent / "data"


def run(on):
    reports, warnings = build_report(*load(DATA), on)
    return {r.contract.contract_id: r for r in reports}, warnings


def by_work(rep):
    return {l.work.work_id: l for l in rep.lines}


D = Decimal


class CheckDate(unittest.TestCase):
    """Проверочная дата из задания: 10.11.2026."""

    def setUp(self):
        self.r, self.warnings = run(date(2026, 11, 10))

    def test_d1_totals(self):
        d1 = self.r["D-1"]
        self.assertEqual(d1.works_total, D("500000.00"))   # W-13 отменена
        self.assertEqual(d1.paid_total, D("315000.00"))    # P-1 + P-3; P-2 возвращен, P-4 в очереди
        self.assertEqual(d1.remaining_total, D("185000.00"))
        self.assertEqual(d1.due_now, D("0.00"))
        self.assertEqual(d1.pending, D("150000.00"))

    def test_d1_works(self):
        w = by_work(self.r["D-1"])
        self.assertEqual(w["W-14"].paid_linked, D("15000.00"))
        self.assertEqual(w["W-14"].paid, D("100000.00"))
        self.assertEqual(w["W-13"].paid, D("0.00"))
        self.assertEqual(w["W-15"].paid, D("15000.00"))
        self.assertEqual(w["W-16"].remaining, D("100000.00"))

    def test_d2(self):
        d2 = self.r["D-2"]
        w = by_work(d2)
        self.assertEqual(w["W-23"].state, "planned")        # принята только 11.11
        self.assertEqual(d2.paid_total, D("75000.00"))      # P-8 оплачен 12.11
        self.assertEqual(w["W-23"].remaining, D("15000.00"))
        self.assertEqual(d2.overpaid_total, D("0.00"))

    def test_money_balance(self):
        for rep in self.r.values():
            self.assertEqual(rep.paid_total,
                             sum((l.paid for l in rep.lines), D("0")) + rep.unallocated)


class OtherDates(unittest.TestCase):
    def test_refund_not_yet_returned(self):
        r, _ = run(date(2026, 9, 5))                        # P-2 ушел, но еще не вернулся
        self.assertEqual(r["D-1"].paid_total, D("350000.00"))

    def test_overpayment_after_all_payments(self):
        r, warnings = run(date(2026, 11, 17))
        self.assertEqual(r["D-2"].paid_total, D("95000.00"))
        self.assertEqual(r["D-2"].overpaid_total, D("5000.00"))
        self.assertTrue(any("переплата" in w for w in warnings))

    def test_before_everything(self):
        r, _ = run(date(2026, 8, 1))
        for rep in r.values():
            self.assertEqual(rep.paid_total, D("0.00"))


if __name__ == "__main__":
    unittest.main()
