from __future__ import annotations

from datetime import datetime

import pytest
from jobsched.billing_credits import add_credit_note
from jobsched.errors import NotFoundError, ValidationError
from jobsched.integ_invoice_total import final_invoice_total
from jobsched.models import Invoice
from jobsched.repository import CustomerRepository, InvoiceRepository, PlanRepository


def _invoice(conn, subtotal_cents=10000, period_end="2024-02-01"):
    plan = PlanRepository(conn).create("starter", 2000)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    inv = Invoice(
        id=0,
        customer_id=cust.id,
        period_start="2024-01-01",
        period_end=period_end,
        subtotal_cents=subtotal_cents,
        tax_cents=2000,
        total_cents=subtotal_cents + 2000,
        currency="USD",
    )
    return InvoiceRepository(conn).create(inv)


def test_worked_example_discount_credits_then_late_fee(conn):
    inv = _invoice(conn)
    add_credit_note(conn, inv.id, 1500)
    result = final_invoice_total(conn, inv.id, today=datetime(2024, 2, 11), percent_off_bp=2500)
    assert result == {
        "subtotal": 10000,
        "discount": 2500,
        "credits": 1500,
        "late_fee": 150,
        "total": 6150,
    }


def test_no_discount_no_credits_and_not_overdue(conn):
    inv = _invoice(conn)  # due 2024-02-01
    result = final_invoice_total(conn, inv.id, today=datetime(2024, 1, 15))
    assert result == {
        "subtotal": 10000,
        "discount": 0,
        "credits": 0,
        "late_fee": 0,
        "total": 10000,
    }


def test_discount_inherits_half_up_rounding(conn):
    # L1-F1: 50% of 1001 removes round_half_up(500.5) = 501, net 500.
    inv = _invoice(conn, subtotal_cents=1001)
    result = final_invoice_total(conn, inv.id, today=datetime(2024, 2, 2), percent_off_bp=5000)
    assert result["discount"] == 501
    assert result["total"] == 500


def test_late_fee_inherits_rounding_and_cap(conn):
    # L1-F2: total 333, one chargeable day at 50 bp: 1.665 -> 2 cents.
    small = _invoice(conn, subtotal_cents=333)
    fee = final_invoice_total(conn, small.id, today=datetime(2024, 2, 7))
    assert fee["late_fee"] == 2
    # L1-F2 cap: 100 * 20000 bp * 1 day = 200, capped at the owed 100.
    capped = _invoice(conn, subtotal_cents=100)
    result = final_invoice_total(
        conn, capped.id, today=datetime(2024, 2, 2), grace_days=0, daily_rate_bp=20000
    )
    assert result["late_fee"] == 100
    assert result["total"] == 200


def test_credits_subtract_from_discounted_amount_and_fee_targets_owed(conn):
    # owed = 10000 - 2000 (fixed) - 3000 (credits) = 5000; 6 days overdue,
    # so 1 chargeable day: fee on 5000 is 25 (fee on the subtotal would be 50).
    inv = _invoice(conn)
    add_credit_note(conn, inv.id, 3000)
    result = final_invoice_total(conn, inv.id, today=datetime(2024, 2, 7), off_cents=2000)
    assert result["discount"] == 2000 and result["credits"] == 3000
    assert result["late_fee"] == 25 and result["total"] == 5025


def test_invoice_due_today_accrues_no_fee(conn):
    inv = _invoice(conn)  # due 2024-02-01
    result = final_invoice_total(conn, inv.id, today=datetime(2024, 2, 1))
    assert result["late_fee"] == 0
    assert result["total"] == 10000
    future = final_invoice_total(conn, inv.id, today=datetime(2024, 1, 31), grace_days=0)
    assert future["late_fee"] == 0  # not-yet-due is 0 days overdue


def test_unknown_invoice_raises_not_found_error(conn):
    with pytest.raises(NotFoundError):
        final_invoice_total(conn, 9999, today=datetime(2024, 3, 1))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"percent_off_bp": 10001}, {"percent_off_bp": -1}, {"off_cents": -1},
        {"grace_days": -1}, {"daily_rate_bp": -1},
    ],
)
def test_invalid_parameters_raise_validation_error(conn, kwargs):
    inv = _invoice(conn)
    with pytest.raises(ValidationError):
        final_invoice_total(conn, inv.id, today=datetime(2024, 3, 1), **kwargs)
