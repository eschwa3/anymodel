"""CSV statement export for a single invoice.

New reporting feature; reads through InvoiceRepository and modifies no
existing module. Style follows jobsched.reports.legacy_export (csv + StringIO).
"""

from __future__ import annotations

import csv
import sqlite3
from io import StringIO

from jobsched.repository import InvoiceRepository

_HEADER = [
    "invoice_id",
    "customer_id",
    "period_start",
    "period_end",
    "currency",
    "description",
    "quantity",
    "amount_cents",
]

_TOTAL_DESCRIPTION = "TOTAL"


def export_invoice_csv(conn: sqlite3.Connection, invoice_id: int) -> str:
    """Return invoice `invoice_id` as CSV: header, one row per line item, TOTAL.

    Raises NotFoundError (via InvoiceRepository.get) for a missing invoice.
    """
    invoice = InvoiceRepository(conn).get(invoice_id)
    invoice_fields = [
        invoice.id,
        invoice.customer_id,
        invoice.period_start,
        invoice.period_end,
        invoice.currency,
    ]
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADER)
    for item in invoice.line_items:
        writer.writerow([*invoice_fields, item.description, item.quantity, item.amount_cents])
    writer.writerow([*invoice_fields, _TOTAL_DESCRIPTION, "", invoice.total_cents])
    return buf.getvalue()
