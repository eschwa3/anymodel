"""Final invoice total: discounts (L1-F1), credits (L1-F4), late fee (L1-F2).

New integration feature; owns no storage and modifies no existing module.
Composes the lane 1 functions rather than re-implementing their rules.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from jobsched.billing_credits import list_credit_notes
from jobsched.billing_discounts import apply_discount, discount_amount
from jobsched.billing_latefees import late_fee_cents
from jobsched.repository import InvoiceRepository


def final_invoice_total(
    conn: sqlite3.Connection,
    invoice_id: int,
    *,
    today: datetime,
    percent_off_bp: int = 0,
    off_cents: int = 0,
    grace_days: int = 5,
    daily_rate_bp: int = 50,
) -> dict[str, int]:
    """Settle `invoice_id` as of `today`: discount, credits, then late fee.

    The due date is the invoice's `period_end` (the table has no due-date
    column). Returns the five intermediate amounts; see SPEC.md L4-F1.
    """
    invoice = InvoiceRepository(conn).get(invoice_id)  # NotFoundError
    subtotal = invoice.subtotal_cents
    discount = discount_amount(subtotal, percent_off_bp, off_cents)
    discounted = apply_discount(subtotal, percent_off_bp, off_cents)
    credits = sum(note.amount_cents for note in list_credit_notes(conn, invoice_id))
    owed = max(0, discounted - credits)
    due = date.fromisoformat(invoice.period_end)
    days_overdue = max(0, (today.date() - due).days)
    late_fee = late_fee_cents(
        owed, days_overdue, grace_days=grace_days, daily_rate_bp=daily_rate_bp
    )
    return {
        "subtotal": subtotal,
        "discount": discount,
        "credits": credits,
        "late_fee": late_fee,
        "total": max(0, owed + late_fee),
    }
