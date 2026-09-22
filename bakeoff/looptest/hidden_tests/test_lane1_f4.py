from __future__ import annotations

import pytest
from jobsched.billing_credits import (
    CreditNote,
    add_credit_note,
    list_credit_notes,
    outstanding_balance,
)
from jobsched.errors import ConflictError, NotFoundError, ValidationError
from jobsched.models import Invoice
from jobsched.repository import CustomerRepository, InvoiceRepository, PlanRepository


def _invoice(conn, total_cents=2500):
    plan = PlanRepository(conn).create("starter", 2000)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    inv = Invoice(
        id=0,
        customer_id=cust.id,
        period_start="2024-01-01",
        period_end="2024-02-01",
        subtotal_cents=2000,
        tax_cents=500,
        total_cents=total_cents,
        currency="USD",
    )
    return InvoiceRepository(conn).create(inv)


def test_add_and_list_roundtrip(conn):
    inv = _invoice(conn)
    note = add_credit_note(conn, inv.id, 1000, "goodwill")
    assert isinstance(note, CreditNote)
    assert note.id > 0
    assert note.invoice_id == inv.id
    assert note.amount_cents == 1000
    assert note.reason == "goodwill"
    assert note.created_at.endswith("+00:00")  # timezone-aware utcnow
    notes = list_credit_notes(conn, inv.id)
    assert notes == [note]


def test_reason_defaults_to_empty_string(conn):
    inv = _invoice(conn)
    note = add_credit_note(conn, inv.id, 500)
    assert note.reason == ""


def test_amount_must_be_positive(conn):
    inv = _invoice(conn)
    with pytest.raises(ValidationError):
        add_credit_note(conn, inv.id, 0)
    with pytest.raises(ValidationError):
        add_credit_note(conn, inv.id, -100)


def test_missing_invoice_raises_not_found_error(conn):
    with pytest.raises(NotFoundError):
        add_credit_note(conn, 9999, 100)
    with pytest.raises(NotFoundError):
        list_credit_notes(conn, 9999)
    with pytest.raises(NotFoundError):
        outstanding_balance(conn, 9999)


def test_invalid_amount_takes_precedence_over_missing_invoice(conn):
    with pytest.raises(ValidationError):
        add_credit_note(conn, 9999, 0)


def test_crediting_full_total_is_allowed_and_outstanding_hits_zero(conn):
    inv = _invoice(conn, total_cents=2500)
    add_credit_note(conn, inv.id, 1000)
    add_credit_note(conn, inv.id, 1500)
    assert outstanding_balance(conn, inv.id) == 0


def test_over_credit_raises_conflict_and_inserts_nothing(conn):
    inv = _invoice(conn, total_cents=2500)
    first = add_credit_note(conn, inv.id, 1000)
    with pytest.raises(ConflictError):
        add_credit_note(conn, inv.id, 2000)
    assert list_credit_notes(conn, inv.id) == [first]
    assert outstanding_balance(conn, inv.id) == 1500


def test_credit_notes_table_is_created_lazily(conn):
    inv = _invoice(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credit_notes'"
    ).fetchone()
    assert row is None  # nothing created it yet
    assert outstanding_balance(conn, inv.id) == 2500  # first call creates it
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credit_notes'"
    ).fetchone()
    assert row is not None
