"""Отчет по оплате работ подрядчиков на заданную дату.

Запуск:
    python report.py --date 2026-11-10
    python report.py --date 10.11.2026 --csv report.csv

Все суммы считаются в Decimal (до копейки), float не используется.
Правила расчета описаны в DECISIONS.md.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


# ---------------------------------------------------------------- загрузка

def parse_money(value: str, where: str) -> Decimal:
    try:
        d = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f"{where}: не число: {value!r}")
    if d != d.quantize(CENT):
        raise ValueError(f"{where}: больше двух знаков после запятой: {value!r}")
    if d < 0:
        raise ValueError(f"{where}: отрицательная сумма: {value!r}")
    return d.quantize(CENT)


def parse_date(value: str, where: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{where}: неверная дата: {value!r}")


COLUMNS = {
    "contracts.csv": ["contract_id", "contractor", "amount", "signed_at"],
    "works.csv": ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"],
    "payments.csv": ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
                     "created_at", "paid_at", "refunded_at"],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in COLUMNS[path.name] if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: нет колонок {', '.join(missing)}")
        rows = list(reader)
    key = COLUMNS[path.name][0]
    seen = set()
    for r in rows:
        for k, v in r.items():
            if v is None:
                r[k] = ""
        if r[key] in seen:
            raise ValueError(f"{path.name}: повторяется номер {r[key]!r}")
        seen.add(r[key])
    return rows


@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date | None


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: date | None
    cancelled_at: date | None


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date | None
    paid_at: date | None
    refunded_at: date | None


def load(data_dir: Path):
    contracts = []
    for r in read_csv(data_dir / "contracts.csv"):
        w = f"contracts.csv {r['contract_id']}"
        contracts.append(Contract(r["contract_id"].strip(), r["contractor"].strip(),
                                  parse_money(r["amount"], w), parse_date(r["signed_at"], w)))
    works = []
    for r in read_csv(data_dir / "works.csv"):
        w = f"works.csv {r['work_id']}"
        works.append(Work(r["work_id"].strip(), r["contract_id"].strip(), r["title"].strip(),
                          parse_money(r["price"], w), r["status"].strip(),
                          parse_date(r["completed_at"], w), parse_date(r["cancelled_at"], w)))
    payments = []
    for r in read_csv(data_dir / "payments.csv"):
        w = f"payments.csv {r['payment_id']}"
        payments.append(Payment(r["payment_id"].strip(), r["contract_id"].strip(),
                                (r["work_id"] or "").strip() or None, r["purpose"].strip(),
                                parse_money(r["amount"], w), r["status"].strip(),
                                parse_date(r["created_at"], w), parse_date(r["paid_at"], w),
                                parse_date(r["refunded_at"], w)))
    return contracts, works, payments


# ---------------------------------------------------- состояние на дату

def work_state(w: Work, on: date, warnings: list[str]) -> str:
    """Статус работы на дату: accepted / planned / cancelled."""
    if w.status == "done":
        if w.completed_at is None:
            warnings.append(f"{w.work_id}: статус done без даты приемки - считаю принятой")
            return "accepted"
        return "accepted" if w.completed_at <= on else "planned"
    if w.status == "cancelled":
        if w.cancelled_at is None:
            warnings.append(f"{w.work_id}: статус cancelled без даты отмены - считаю отмененной")
            return "cancelled"
        return "cancelled" if w.cancelled_at <= on else "planned"
    if w.status == "planned":
        return "planned"
    warnings.append(f"{w.work_id}: неизвестный статус {w.status!r} - считаю запланированной")
    return "planned"


def payment_state(p: Payment, on: date, warnings: list[str]) -> str:
    """Состояние платежа на дату: paid / pending / refunded / none."""
    if p.status in ("paid", "refunded"):
        if p.paid_at is None:
            warnings.append(f"{p.payment_id}: статус {p.status} без даты оплаты - не учитываю")
            return "none"
        if p.paid_at > on:
            return "pending" if (p.created_at and p.created_at <= on) else "none"
        if p.status == "refunded":
            if p.refunded_at is None:
                warnings.append(f"{p.payment_id}: возврат без даты - считаю возвращенным")
                return "refunded"
            return "refunded" if p.refunded_at <= on else "paid"
        return "paid"
    if p.status == "queued":
        return "pending" if (p.created_at is None or p.created_at <= on) else "none"
    warnings.append(f"{p.payment_id}: неизвестный статус {p.status!r} - не учитываю")
    return "none"


# -------------------------------------------------------------- расчет

@dataclass
class WorkLine:
    work: Work
    state: str
    paid_linked: Decimal = ZERO      # платежи, привязанные к работе
    paid_allocated: Decimal = ZERO   # доля платежей по договору без привязки

    @property
    def to_pay(self) -> Decimal:     # сколько по работе вообще причитается
        return ZERO if self.state == "cancelled" else self.work.price

    @property
    def paid(self) -> Decimal:
        return self.paid_linked + self.paid_allocated

    @property
    def remaining(self) -> Decimal:
        return max(self.to_pay - self.paid, ZERO)

    @property
    def overpaid(self) -> Decimal:
        return max(self.paid - self.to_pay, ZERO)


@dataclass
class ContractReport:
    contract: Contract
    lines: list[WorkLine] = field(default_factory=list)
    unlinked_paid: Decimal = ZERO
    unallocated: Decimal = ZERO      # остаток платежей без привязки, не разнесенный на работы
    pending: Decimal = ZERO          # заведено, но деньги еще не ушли
    pending_ids: list[str] = field(default_factory=list)

    @property
    def works_total(self) -> Decimal:
        return sum((l.to_pay for l in self.lines), ZERO)

    @property
    def paid_total(self) -> Decimal:
        return sum((l.paid for l in self.lines), ZERO) + self.unallocated

    @property
    def remaining_total(self) -> Decimal:
        return sum((l.remaining for l in self.lines), ZERO)

    @property
    def due_now(self) -> Decimal:     # долг за уже принятые работы
        return sum((l.remaining for l in self.lines if l.state == "accepted"), ZERO)

    @property
    def overpaid_total(self) -> Decimal:
        return sum((l.overpaid for l in self.lines), ZERO) + self.unallocated


ORDER = {"accepted": 0, "planned": 1}


def build_report(contracts, works, payments, on: date):
    warnings: list[str] = []
    reports: dict[str, ContractReport] = {c.contract_id: ContractReport(c) for c in contracts}
    lines: dict[str, WorkLine] = {}

    for w in works:
        if w.contract_id not in reports:
            warnings.append(f"{w.work_id}: договор {w.contract_id} не найден - работа пропущена")
            continue
        line = WorkLine(w, work_state(w, on, warnings))
        lines[w.work_id] = line
        reports[w.contract_id].lines.append(line)

    for p in payments:
        rep = reports.get(p.contract_id)
        if rep is None:
            warnings.append(f"{p.payment_id}: договор {p.contract_id} не найден - платеж пропущен")
            continue
        st = payment_state(p, on, warnings)
        if st == "pending":
            rep.pending += p.amount
            rep.pending_ids.append(p.payment_id)
        if st != "paid":
            continue
        if p.work_id:
            line = lines.get(p.work_id)
            if line is None or line.work.contract_id != p.contract_id:
                warnings.append(f"{p.payment_id}: работа {p.work_id} не найдена в договоре "
                                f"{p.contract_id} - считаю платежом по договору")
                rep.unlinked_paid += p.amount
            else:
                line.paid_linked += p.amount
        else:
            rep.unlinked_paid += p.amount

    # Разнесение платежей без привязки: сначала принятые работы по дате приемки,
    # затем запланированные по номеру. Отмененные работы денег не получают.
    for rep in reports.values():
        pool = rep.unlinked_paid
        queue = sorted((l for l in rep.lines if l.state != "cancelled"),
                       key=lambda l: (ORDER[l.state],
                                      (l.work.completed_at or date.max) if l.state == "accepted" else date.max,
                                      l.work.work_id))
        for line in queue:
            if pool <= ZERO:
                break
            take = min(pool, line.remaining)
            line.paid_allocated += take
            pool -= take
        rep.unallocated = pool

        # Проверки целостности
        c = rep.contract
        all_prices = sum((l.work.price for l in rep.lines), ZERO)
        if all_prices != c.amount:
            warnings.append(f"{c.contract_id}: сумма договора {fmt(c.amount)}, а сумма цен всех "
                            f"работ {fmt(all_prices)}")
        if rep.works_total != c.amount:
            warnings.append(f"{c.contract_id}: к оплате по неотмененным работам {fmt(rep.works_total)} "
                            f"≠ сумме договора {fmt(c.amount)}")
        if rep.overpaid_total > ZERO:
            warnings.append(f"{c.contract_id}: переплата {fmt(rep.overpaid_total)} - выплачено больше, "
                            f"чем стоят неотмененные работы")
        for l in rep.lines:
            if l.state == "cancelled" and l.paid_linked > ZERO:
                warnings.append(f"{l.work.work_id}: работа отменена, но по ней выплачено "
                                f"{fmt(l.paid_linked)} - нужен возврат или перенос")

    return list(reports.values()), warnings


# --------------------------------------------------------------- вывод

STATE_RU = {"accepted": "принята", "planned": "запланирована", "cancelled": "отменена"}


def fmt(d: Decimal) -> str:
    s = f"{d:,.2f}".replace(",", " ")
    return s


def print_report(reports, warnings, on: date, out=sys.stdout):
    print(f"ОТЧЕТ ОБ ОПЛАТЕ РАБОТ НА {on.strftime('%d.%m.%Y')}", file=out)
    for rep in reports:
        c = rep.contract
        print(file=out)
        print(f"Договор {c.contract_id} - {c.contractor}, сумма договора {fmt(c.amount)}", file=out)
        hdr = f"  {'Работа':<6} {'Название':<28} {'Статус':<14} {'Цена':>12} {'Выплачено':>12} {'Осталось':>12}"
        print(hdr, file=out)
        print("  " + "-" * (len(hdr) - 2), file=out)
        for l in rep.lines:
            w = l.work
            print(f"  {w.work_id:<6} {w.title[:28]:<28} {STATE_RU[l.state]:<14} "
                  f"{fmt(w.price):>12} {fmt(l.paid):>12} {fmt(l.remaining):>12}", file=out)
        print("  " + "-" * (len(hdr) - 2), file=out)
        print(f"  Итого к оплате (без отмененных работ): {fmt(rep.works_total):>14}", file=out)
        print(f"  Выплачено:                             {fmt(rep.paid_total):>14}", file=out)
        print(f"  Осталось оплатить:                     {fmt(rep.remaining_total):>14}", file=out)
        print(f"    из них за уже принятые работы:       {fmt(rep.due_now):>14}", file=out)
        if rep.overpaid_total:
            print(f"  ПЕРЕПЛАТА:                             {fmt(rep.overpaid_total):>14}", file=out)
        if rep.pending:
            print(f"  В очереди на оплату (не учтено):       {fmt(rep.pending):>14}  "
                  f"({', '.join(rep.pending_ids)})", file=out)
    if warnings:
        print(file=out)
        print("ОБРАТИТЕ ВНИМАНИЕ:", file=out)
        for w in warnings:
            print(f"  - {w}", file=out)


def write_csv(reports, path: Path):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Договор", "Подрядчик", "Работа", "Название", "Статус на дату",
                     "Цена", "К оплате", "Выплачено", "из них привязано к работе",
                     "из них аванс/платежи по договору", "Осталось оплатить"])
        for rep in reports:
            c = rep.contract
            for l in rep.lines:
                wr.writerow([c.contract_id, c.contractor, l.work.work_id, l.work.title,
                             STATE_RU[l.state], l.work.price, l.to_pay, l.paid,
                             l.paid_linked, l.paid_allocated, l.remaining])
            wr.writerow([c.contract_id, c.contractor, "ИТОГО", "", "", c.amount,
                         rep.works_total, rep.paid_total, "", "", rep.remaining_total])
            if rep.overpaid_total:
                wr.writerow([c.contract_id, c.contractor, "ПЕРЕПЛАТА", "", "", "", "",
                             rep.overpaid_total, "", "", ""])


def parse_report_date(value: str) -> date:
    """Дата отчета: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ."""
    value = value.strip()
    try:
        if "." in value:
            d, m, y = value.split(".")
            return date(int(y), int(m), int(d))
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"неверная дата отчета {value!r}, нужно ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Отчет об оплате работ по договорам на дату")
    ap.add_argument("--data", default=str(Path(__file__).parent / "data"),
                    help="папка с contracts.csv, works.csv, payments.csv")
    ap.add_argument("--date", required=True, help="дата отчета, ГГГГ-ММ-ДД или ДД.ММ.ГГГГ (включительно)")
    ap.add_argument("--csv", help="дополнительно сохранить отчет в CSV (для Excel)")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        on = parse_report_date(args.date)
        reports, warnings = build_report(*load(Path(args.data)), on)
    except (ValueError, OSError, KeyError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print_report(reports, warnings, on)
    if args.csv:
        write_csv(reports, Path(args.csv))
        print(f"\nCSV сохранен: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
