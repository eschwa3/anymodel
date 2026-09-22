"""Credit notes against invoices, with outstanding-balance tracking.

Owns the `credit_notes` table (lazily created on the caller's connection);
modifies no existing module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobsched.errors import ConflictError, ValidationError
from jobsched.repository import InvoiceRepository
from jobsched.utils.time import utcnow

_TABLE = """
CREATE TABLE IF NOT EXISTS credit_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""


@dataclass
class CreditNote:
    id: int
    invoice_id: int
    amount_cents: int
    reason: str
    created_at: str


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)


def _row_to_note(row: sqlite3.Row) -> CreditNote:
    return CreditNote(
        id=row["id"],
        invoice_id=row["invoice_id"],
        amount_cents=row["amount_cents"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def _credited_cents(conn: sqlite3.Connection, invoice_id: int) -> int:
    rows = conn.execute(
        "SELECT amount_cents FROM credit_notes WHERE invoice_id = ?", (invoice_id,)
    ).fetchall()
    return sum(r["amount_cents"] for r in rows)


def add_credit_note(
    conn: sqlite3.Connection, invoice_id: int, amount_cents: int, reason: str = ""
) -> CreditNote:
    """Record a positive credit against `invoice_id`, capped at its total."""
    _ensure_table(conn)
    if amount_cents <= 0:
        raise ValidationError("credit note amount must be positive")
    invoice = InvoiceRepository(conn).get(invoice_id)  # NotFoundError
    if _credited_cents(conn, invoice_id) + amount_cents > invoice.total_cents:
        raise ConflictError(f"credit exceeds invoice {invoice_id} total")
    created_at = utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO credit_notes (invoice_id, amount_cents, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (invoice_id, amount_cents, reason, created_at),
    )
    conn.commit()
    return CreditNote(
        id=cur.lastrowid,
        invoice_id=invoice_id,
        amount_cents=amount_cents,
        reason=reason,
        created_at=created_at,
    )


def list_credit_notes(conn: sqlite3.Connection, invoice_id: int) -> list[CreditNote]:
    """All credit notes for `invoice_id`, in insertion order."""
    _ensure_table(conn)
    InvoiceRepository(conn).get(invoice_id)  # NotFoundError
    rows = conn.execute(
        "SELECT * FROM credit_notes WHERE invoice_id = ? ORDER BY id", (invoice_id,)
    ).fetchall()
    return [_row_to_note(r) for r in rows]


def outstanding_balance(conn: sqlite3.Connection, invoice_id: int) -> int:
    """Invoice total minus credited amounts, never below zero."""
    _ensure_table(conn)
    invoice = InvoiceRepository(conn).get(invoice_id)  # NotFoundError
    return max(0, invoice.total_cents - _credited_cents(conn, invoice_id))
