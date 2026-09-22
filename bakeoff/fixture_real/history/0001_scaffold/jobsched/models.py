"""Dataclasses for jobsched's domain records.

These mirror the `jobsched.repository` tables one-to-one; a repository
method returns one of these, never a raw sqlite3.Row.
"""

from __future__ import annotations

from dataclasses import dataclass
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
