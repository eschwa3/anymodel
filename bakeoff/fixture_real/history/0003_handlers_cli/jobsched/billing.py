"""Invoice generation: plan charges, mid-cycle plan-change proration, tax."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from jobsched.config import AppConfig
from jobsched.models import Invoice, InvoiceLineItem
from jobsched.repository import (
    CustomerRepository,
    InvoiceRepository,
    PlanChangeRepository,
    PlanRepository,
)
from jobsched.utils.money import round_half_up
from jobsched.utils.time import now


def compute_tax(subtotal_cents: int, cfg: AppConfig) -> int:
    return round_half_up(subtotal_cents * cfg.tax_rate_bp / 10000)


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _prorated_plan_charge(monthly_price_cents: int, days_on_plan: int, days_in_period: int) -> int:
    exact = monthly_price_cents * days_on_plan / days_in_period
    return round_half_up(exact)


def generate_invoice(
    conn: sqlite3.Connection,
    customer_id: int,
    period_start: str,
    period_end: str,
    cfg: AppConfig,
) -> Invoice:
    """Generate (and persist) an invoice for `customer_id` covering
    `[period_start, period_end)`.

    A customer with no plan change in the period is billed the plan's full
    monthly price. A customer who changed plans mid-period is billed a
    prorated amount for each plan they held during the period.
    """
    customers = CustomerRepository(conn)
    plans = PlanRepository(conn)
    plan_changes = PlanChangeRepository(conn)

    customer = customers.get(customer_id)
    changes = plan_changes.list_for_period(customer_id, period_start, period_end)

    line_items: list[InvoiceLineItem] = []
    subtotal_cents = 0

    if not changes:
        plan = plans.get(customer.plan_id)
        subtotal_cents = plan.monthly_price_cents
        line_items.append(InvoiceLineItem(description=f"{plan.name} plan", amount_cents=subtotal_cents))
    else:
        total_days = (_parse_date(period_end) - _parse_date(period_start)).days
        boundary = _parse_date(period_start)
        current_plan_id = changes[0].old_plan_id
        segments: list[tuple[int, int]] = []  # (plan_id, days)

        for change in changes:
            change_date = _parse_date(change.effective_at)
            days = (change_date - boundary).days
            if days > 0:
                segments.append((current_plan_id, days))
            boundary = change_date
            current_plan_id = change.new_plan_id

        trailing_days = (_parse_date(period_end) - boundary).days
        if trailing_days > 0:
            segments.append((current_plan_id, trailing_days))

        for plan_id, days in segments:
            plan = plans.get(plan_id)
            amount = _prorated_plan_charge(plan.monthly_price_cents, days, total_days)
            subtotal_cents += amount
            line_items.append(
                InvoiceLineItem(description=f"{plan.name} (prorated {days}d)", amount_cents=amount)
            )

    tax_cents = compute_tax(subtotal_cents, cfg)
    invoice = Invoice(
        id=0,
        customer_id=customer_id,
        period_start=period_start,
        period_end=period_end,
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        total_cents=subtotal_cents + tax_cents,
        currency=cfg.currency,
        created_at=now().isoformat(),
        line_items=line_items,
    )
    return InvoiceRepository(conn).create(invoice)
