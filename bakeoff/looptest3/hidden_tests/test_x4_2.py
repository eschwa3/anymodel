from __future__ import annotations

import csv
from io import StringIO

import pytest
from jobsched.errors import NotFoundError
from jobsched.models import Invoice, InvoiceLineItem
from jobsched.reports.invoice_export import export_invoice_csv
from jobsched.repository import CustomerRepository, InvoiceRepository, PlanRepository

HEADER = "invoice_id,customer_id,period_start,period_end,currency,description,quantity,amount_cents"


def _invoice(conn, line_items, total_cents=2500):
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
        line_items=line_items,
    )
    return InvoiceRepository(conn).create(inv)


def _rows(text):
    return text.split("\r\n")


def test_header_row_is_exact(conn):
    inv = _invoice(conn, [InvoiceLineItem(description="starter plan", amount_cents=2000)])
    out = export_invoice_csv(conn, inv.id)
    assert _rows(out)[0] == HEADER


def test_one_row_per_line_item_in_stored_order(conn):
    items = [
        InvoiceLineItem(description="starter plan", amount_cents=2000),
        InvoiceLineItem(description="overage", amount_cents=350, quantity=7),
    ]
    inv = _invoice(conn, items)
    rows = _rows(export_invoice_csv(conn, inv.id))
    base = f"{inv.id},{inv.customer_id},2024-01-01,2024-02-01,USD"
    assert rows[1] == f"{base},starter plan,1,2000"
    assert rows[2] == f"{base},overage,7,350"
    assert rows[3].startswith(f"{base},TOTAL,")


def test_total_row_is_last_with_stored_total_cents(conn):
    inv = _invoice(conn, [InvoiceLineItem(description="starter plan", amount_cents=2000)])
    rows = _rows(export_invoice_csv(conn, inv.id))
    assert rows[-1] == ""
    assert rows[-2] == f"{inv.id},{inv.customer_id},2024-01-01,2024-02-01,USD,TOTAL,,2500"


def test_invoice_with_zero_line_items_is_header_plus_total(conn):
    inv = _invoice(conn, [])
    rows = _rows(export_invoice_csv(conn, inv.id))
    assert rows == [HEADER, f"{inv.id},{inv.customer_id},2024-01-01,2024-02-01,USD,TOTAL,,2500", ""]


def test_description_containing_comma_is_standard_quoted(conn):
    inv = _invoice(conn, [InvoiceLineItem(description="setup, takedown", amount_cents=900)])
    parsed = list(csv.reader(StringIO(export_invoice_csv(conn, inv.id))))
    assert parsed[1] == [
        str(inv.id),
        str(inv.customer_id),
        "2024-01-01",
        "2024-02-01",
        "USD",
        "setup, takedown",
        "1",
        "900",
    ]


def test_amounts_are_raw_cents_not_formatted(conn):
    inv = _invoice(conn, [InvoiceLineItem(description="starter plan", amount_cents=2000)])
    out = export_invoice_csv(conn, inv.id)
    assert "2000" in out
    assert "20.00" not in out


def test_missing_invoice_raises_not_found_error(conn):
    with pytest.raises(NotFoundError):
        export_invoice_csv(conn, 9999)
