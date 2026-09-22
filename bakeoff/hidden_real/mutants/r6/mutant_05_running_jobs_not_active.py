"""Builds the reminder digest the ops CLI prints (or would email, in a
deployment that wired up an actual mail sender).
"""

from __future__ import annotations

from dataclasses import dataclass

from jobsched.errors import ValidationError
from jobsched.models import Customer, Job, JobStatus
from jobsched.utils.time import now


@dataclass
class Reminder:
    customer_id: int
    kind: str  # "overdue_invoice" | "quota_exceeded" | "plan_renewal"
    message: str
    created_at: object = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = now()


def validate_customer_for_notification(customer: Customer | None) -> bool:
    if customer is None:
        raise ValidationError("customer is required")
    if not customer.email or "@" not in customer.email:
        raise ValidationError(f"customer {customer.id} has no usable email")
    return True


def build_reminder_batch(
    customers: list[Customer],
    jobs_by_customer: dict[int, list[Job]],
    overdue_customer_ids: set[int],
    quota_by_customer: dict[int, int],
) -> list[Reminder]:
    """Build the set of reminders to send this run.

    A customer is skipped entirely if they have no usable email. Overdue
    invoices always produce a reminder. A quota reminder is produced only
    when the customer's number of active (pending/running) jobs has reached
    or passed their plan's quota; a quota of 0 means "unlimited", so it
    never triggers a reminder. Each (customer, kind) pair produces at most
    one reminder per batch.
    """
    reminders: list[Reminder] = []
    seen: set[tuple[int, str]] = set()

    for customer in customers:
        if not customer.email or "@" not in customer.email:
            continue

        if customer.id in overdue_customer_ids:
            key = (customer.id, "overdue_invoice")
            if key not in seen:
                reminders.append(
                    Reminder(customer.id, "overdue_invoice", f"Invoice overdue for {customer.name}")
                )
                seen.add(key)

        quota = quota_by_customer.get(customer.id, 0)
        jobs = jobs_by_customer.get(customer.id, [])
        active = [j for j in jobs if j.status == JobStatus.PENDING]
        if quota > 0 and len(active) >= quota:
            key = (customer.id, "quota_exceeded")
            if key not in seen:
                reminders.append(
                    Reminder(customer.id, "quota_exceeded", f"{customer.name} is at their job quota")
                )
                seen.add(key)

    return reminders


def format_reminder_digest(reminders: list[Reminder]) -> str:
    if not reminders:
        return "No reminders."
    lines = [f"- [{r.kind}] {r.message}" for r in reminders]
    return "\n".join(lines)
