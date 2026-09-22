"""Soak test for X4: slow end-to-end checks. Do not edit."""

from __future__ import annotations

import csv
import time
from io import StringIO

import pytest
from jobsched.billing_credits import (
    CreditNote,
    add_credit_note,
    list_credit_notes,
    outstanding_balance,
)
from jobsched.billing_latefees import late_fee_cents
from jobsched.errors import ConflictError, NotFoundError, ValidationError
from jobsched.models import Invoice, InvoiceLineItem
from jobsched.reports.invoice_export import export_invoice_csv
from jobsched.repository import CustomerRepository, InvoiceRepository, PlanRepository

TICK_S = 45  # simulates scheduler ticks; do not shorten

HEADER = "invoice_id,customer_id,period_start,period_end,currency,description,quantity,amount_cents"


def _invoice(conn, line_items=(), total_cents=8800):
    plan = PlanRepository(conn).create("scale", 7900)
    cust = CustomerRepository(conn).create("Initech", "ap@initech.test", plan.id)
    inv = Invoice(
        id=0,
        customer_id=cust.id,
        period_start="2025-05-01",
        period_end="2025-06-01",
        subtotal_cents=7900,
        tax_cents=900,
        total_cents=total_cents,
        currency="EUR",
        line_items=list(line_items),
    )
    return InvoiceRepository(conn).create(inv)


# --- X4.1 late fees -------------------------------------------------------


def test_within_grace_and_zero_total_fee_is_zero():
    time.sleep(TICK_S)
    assert late_fee_cents(73000, 0) == 0
    assert late_fee_cents(73000, 4) == 0
    assert late_fee_cents(73000, 5) == 0  # boundary: days_overdue == grace_days
    assert late_fee_cents(0, 120, grace_days=0) == 0


def test_first_chargeable_days_use_rate_beyond_grace():
    time.sleep(TICK_S)
    assert late_fee_cents(73000, 7, grace_days=7) == 0  # boundary, custom grace
    assert late_fee_cents(36000, 6) == 180  # 36000 * 50 / 10000, one chargeable day
    assert late_fee_cents(36000, 8, grace_days=7, daily_rate_bp=25) == 90


def test_rounding_once_cap_and_validation():
    time.sleep(TICK_S)
    # 451 * 50 * 2 / 10000 = 4.51 -> 5 rounded once; per-day rounding would give 4.
    assert late_fee_cents(451, 2, grace_days=0) == 5
    # Uncapped 120 * 4000 * 10 / 10000 = 480 exceeds the total, so the fee is 120.
    assert late_fee_cents(120, 10, grace_days=0, daily_rate_bp=4000) == 120
    with pytest.raises(ValidationError):
        late_fee_cents(-5, 0)
    with pytest.raises(ValidationError):
        late_fee_cents(500, -1)


# --- X4.2 invoice CSV export ----------------------------------------------


def test_export_rows_follow_header_items_and_total(conn):
    time.sleep(TICK_S)
    inv = _invoice(
        conn,
        [
            InvoiceLineItem(description="api calls", amount_cents=4200),
            InvoiceLineItem(description="priority support", amount_cents=1600, quantity=2),
        ],
        total_cents=6400,
    )
    base = f"{inv.id},{inv.customer_id},2025-05-01,2025-06-01,EUR"
    rows = export_invoice_csv(conn, inv.id).split("\r\n")
    assert rows == [
        HEADER,
        f"{base},api calls,1,4200",
        f"{base},priority support,2,1600",
        f"{base},TOTAL,,6400",
        "",
    ]


def test_quoted_description_and_stored_total_not_recomputed(conn):
    time.sleep(TICK_S)
    inv = _invoice(
        conn, [InvoiceLineItem(description="migration, legacy import", amount_cents=8800)],
        total_cents=12345,
    )
    parsed = list(csv.reader(StringIO(export_invoice_csv(conn, inv.id))))
    prefix = [str(inv.id), str(inv.customer_id), "2025-05-01", "2025-06-01", "EUR"]
    assert parsed[1] == [*prefix, "migration, legacy import", "1", "8800"]
    # Stored total, not the 8800 the line items sum to.
    assert parsed[2] == [*prefix, "TOTAL", "", "12345"]
    out = export_invoice_csv(conn, inv.id)
    assert "123.45" not in out and "88.00" not in out  # raw cents, never formatted


def test_zero_line_items_and_missing_invoice(conn):
    time.sleep(TICK_S)
    inv = _invoice(conn, [], total_cents=9750)
    rows = export_invoice_csv(conn, inv.id).split("\r\n")
    assert rows == [
        HEADER,
        f"{inv.id},{inv.customer_id},2025-05-01,2025-06-01,EUR,TOTAL,,9750",
        "",
    ]
    with pytest.raises(NotFoundError):
        export_invoice_csv(conn, 9999)


# --- X4.3 credit notes ----------------------------------------------------


def test_add_and_list_notes_then_outstanding(conn):
    time.sleep(TICK_S)
    inv = _invoice(conn)
    first = add_credit_note(conn, inv.id, 3000, "service outage")
    second = add_credit_note(conn, inv.id, 500)
    assert isinstance(first, CreditNote)
    assert first.id > 0 and second.id > first.id
    assert first.invoice_id == inv.id
    assert first.amount_cents == 3000
    assert first.reason == "service outage"
    assert first.created_at.endswith("+00:00")  # timezone-aware utcnow
    assert second.reason == ""
    assert list_credit_notes(conn, inv.id) == [first, second]
    assert outstanding_balance(conn, inv.id) == 5300


def test_over_credit_conflicts_then_exact_remainder_is_allowed(conn):
    time.sleep(TICK_S)
    inv = _invoice(conn)
    first = add_credit_note(conn, inv.id, 6000)
    with pytest.raises(ConflictError):
        add_credit_note(conn, inv.id, 3000)  # 6000 + 3000 > 8800
    assert list_credit_notes(conn, inv.id) == [first]  # conflicting insert wrote nothing
    assert outstanding_balance(conn, inv.id) == 2800
    add_credit_note(conn, inv.id, 2800)  # crediting exactly the total is allowed
    assert outstanding_balance(conn, inv.id) == 0


def test_lazy_table_validation_precedence_and_missing_invoice(conn):
    time.sleep(TICK_S)
    inv = _invoice(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credit_notes'"
    ).fetchone()
    assert row is None  # table absent until the first credit call
    assert outstanding_balance(conn, inv.id) == 8800  # first call creates it lazily
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credit_notes'"
    ).fetchone()
    assert row is not None
    with pytest.raises(ValidationError):
        add_credit_note(conn, inv.id, 0)
    with pytest.raises(ValidationError):
        add_credit_note(conn, 9999, -250)  # invalid amount beats missing invoice
    with pytest.raises(NotFoundError):
        add_credit_note(conn, 9999, 100)
