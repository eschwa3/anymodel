"""Hidden regression test for the R4 (bugfix-from-symptom) bake-off task.

Injected into the worker's repo copy as `tests/test_invoice_proration_hidden.py`
*after* the worker has finished (never shown to the worker). Fails against
the original, unfixed `jobsched/billing.py` and passes against
`bakeoff/hidden_real/reference/r4/billing.py`.

Scenario: two segments (1 day @ 502 cents/mo, 3 days @ 1502 cents/mo, out of
a 4-day period) each round to a distinct cent value when rounded
independently (126 + 1127 = 1253), but rounding the exact combined subtotal
once gives 1252. AGENTS.md's money convention says to round once, at the
point a value is finalized -- not per intermediate step.
"""

from __future__ import annotations

from jobsched import billing
from jobsched.config import load_config
from jobsched.repository import CustomerRepository, PlanChangeRepository, PlanRepository


def test_mid_cycle_plan_change_rounds_subtotal_once(conn):
    cfg = load_config({"tax_rate_bp": 0})
    plan_a = PlanRepository(conn).create("a", 502)
    plan_b = PlanRepository(conn).create("b", 1502)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan_a.id)
    PlanChangeRepository(conn).create(cust.id, plan_a.id, plan_b.id, "2024-01-02")

    invoice = billing.generate_invoice(conn, cust.id, "2024-01-01", "2024-01-05", cfg)

    assert invoice.subtotal_cents == 1252
    assert invoice.total_cents == 1252
    assert sum(item.amount_cents for item in invoice.line_items) == invoice.subtotal_cents
