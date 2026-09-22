from __future__ import annotations

from jobsched import billing
from jobsched.config import load_config
from jobsched.repository import CustomerRepository, PlanChangeRepository, PlanRepository


def test_compute_tax_basic():
    cfg = load_config({"tax_rate_bp": 1000})  # 10%
    assert billing.compute_tax(10000, cfg) == 1000


def test_compute_tax_zero_rate():
    cfg = load_config({"tax_rate_bp": 0})
    assert billing.compute_tax(5000, cfg) == 0


def test_generate_invoice_no_plan_change(conn):
    cfg = load_config({"tax_rate_bp": 1000})
    plan = PlanRepository(conn).create("starter", 2000)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)

    invoice = billing.generate_invoice(conn, cust.id, "2024-01-01", "2024-02-01", cfg)

    assert invoice.subtotal_cents == 2000
    assert invoice.tax_cents == 200
    assert invoice.total_cents == 2200
    assert len(invoice.line_items) == 1


def test_generate_invoice_with_single_plan_change(conn):
    cfg = load_config({"tax_rate_bp": 0})
    plan_a = PlanRepository(conn).create("starter", 3000)
    plan_b = PlanRepository(conn).create("pro", 6000)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan_a.id)
    PlanChangeRepository(conn).create(cust.id, plan_a.id, plan_b.id, "2024-01-16")

    invoice = billing.generate_invoice(conn, cust.id, "2024-01-01", "2024-01-31", cfg)

    # 15 days on starter (3000/mo) + 15 days on pro (6000/mo) out of a 30-day period.
    assert len(invoice.line_items) == 2
    assert invoice.subtotal_cents == 4500
    assert invoice.total_cents == invoice.subtotal_cents
