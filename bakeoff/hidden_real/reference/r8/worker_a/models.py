"""Dataclasses for jobsched's domain records.

These mirror the `jobsched.repository` tables one-to-one; a repository
method returns one of these, never a raw sqlite3.Row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


@dataclass
class Plan:
    id: int
    name: str
    monthly_price_cents: int
    job_quota: int


@dataclass
class Customer:
    id: int
    name: str
    email: str
    plan_id: int
    created_at: str = ""


@dataclass
class Job:
    id: int
    customer_id: int
    name: str
    status: JobStatus
    priority: int = 0
    retry_count: int = 0
    reserved_by: str | None = None
    reserved_until: str | None = None
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class PlanChange:
    id: int
    customer_id: int
    old_plan_id: int
    new_plan_id: int
    effective_at: str


@dataclass
class InvoiceLineItem:
    description: str
    amount_cents: int
    quantity: int = 1


@dataclass
class Invoice:
    id: int
    customer_id: int
    period_start: str
    period_end: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    currency: str
    created_at: str = ""
    line_items: list[InvoiceLineItem] = field(default_factory=list)
