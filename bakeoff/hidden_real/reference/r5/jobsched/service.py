"""Service layer: use-case functions that combine repositories, the
scheduler, and billing. This is what `handlers.py` and `cli.py` call --
neither talks to `repository.py` directly.
"""

from __future__ import annotations

import sqlite3

from jobsched.errors import ValidationError
from jobsched.models import Customer, Job
from jobsched.repository import (
    CustomerRepository,
    JobRepository,
    PlanChangeRepository,
    PlanRepository,
)
from jobsched.utils.time import utcnow


def create_customer(conn: sqlite3.Connection, name: str, email: str, plan_id: int) -> Customer:
    if not name.strip():
        raise ValidationError("customer name is required")
    if "@" not in email:
        raise ValidationError(f"invalid email: {email!r}")
    PlanRepository(conn).get(plan_id)  # raises NotFoundError if the plan doesn't exist
    return CustomerRepository(conn).create(name.strip(), email.strip(), plan_id)


def create_job(conn: sqlite3.Connection, customer_id: int, name: str, priority: int = 0) -> Job:
    if not name.strip():
        raise ValidationError("job name is required")
    customer = CustomerRepository(conn).get(customer_id)
    plan = PlanRepository(conn).get(customer.plan_id)
    jobs = JobRepository(conn)
    if plan.job_quota and jobs.count_active_for_customer(customer_id) >= plan.job_quota:
        raise ValidationError(f"customer {customer_id} is at their job quota ({plan.job_quota})")
    return jobs.create(customer_id, name.strip(), priority=priority)


def change_plan(conn: sqlite3.Connection, customer_id: int, new_plan_id: int) -> Customer:
    customers = CustomerRepository(conn)
    customer = customers.get(customer_id)
    PlanRepository(conn).get(new_plan_id)  # validate it exists
    if customer.plan_id == new_plan_id:
        return customer
    PlanChangeRepository(conn).create(
        customer_id, customer.plan_id, new_plan_id, utcnow().isoformat()
    )
    customers.update_plan(customer_id, new_plan_id)
    return customers.get(customer_id)


def audit_trail_for_customer(conn: sqlite3.Connection, customer_id: int) -> list[dict]:
    """A lightweight, in-memory audit trail: one entry per job the customer
    currently has, timestamped as of the call (used by the admin CLI's
    `status` command, not persisted anywhere).
    """
    jobs = JobRepository(conn).list_for_customer(customer_id)
    return [{"job_id": j.id, "status": j.status.value, "checked_at": utcnow()} for j in jobs]
