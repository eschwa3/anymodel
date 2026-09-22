"""sqlite-backed repositories. Every query is parameterized -- see AGENTS.md."""

from __future__ import annotations

import sqlite3

from jobsched.errors import NotFoundError
from jobsched.models import Customer, Job, JobStatus, Plan
from jobsched.utils.time import now


def _row_to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=row["id"],
        name=row["name"],
        monthly_price_cents=row["monthly_price_cents"],
        job_quota=row["job_quota"],
    )


def _row_to_customer(row: sqlite3.Row) -> Customer:
    return Customer(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        plan_id=row["plan_id"],
        created_at=row["created_at"],
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        customer_id=row["customer_id"],
        name=row["name"],
        status=JobStatus(row["status"]),
        priority=row["priority"],
        retry_count=row["retry_count"],
        reserved_by=row["reserved_by"],
        reserved_until=row["reserved_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PlanRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, name: str, monthly_price_cents: int, job_quota: int = 0) -> Plan:
        cur = self.conn.execute(
            "INSERT INTO plans (name, monthly_price_cents, job_quota) VALUES (?, ?, ?)",
            (name, monthly_price_cents, job_quota),
        )
        self.conn.commit()
        return Plan(id=cur.lastrowid, name=name, monthly_price_cents=monthly_price_cents, job_quota=job_quota)

    def get(self, plan_id: int) -> Plan:
        row = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"plan {plan_id} not found")
        return _row_to_plan(row)

    def list_all(self) -> list[Plan]:
        rows = self.conn.execute("SELECT * FROM plans ORDER BY id").fetchall()
        return [_row_to_plan(r) for r in rows]


class CustomerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, name: str, email: str, plan_id: int) -> Customer:
        created_at = now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO customers (name, email, plan_id, created_at) VALUES (?, ?, ?, ?)",
            (name, email, plan_id, created_at),
        )
        self.conn.commit()
        return Customer(id=cur.lastrowid, name=name, email=email, plan_id=plan_id, created_at=created_at)

    def get(self, customer_id: int) -> Customer:
        row = self.conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"customer {customer_id} not found")
        return _row_to_customer(row)

    def list_all(self) -> list[Customer]:
        rows = self.conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
        return [_row_to_customer(r) for r in rows]

    def update_plan(self, customer_id: int, plan_id: int) -> None:
        cur = self.conn.execute(
            "UPDATE customers SET plan_id = ? WHERE id = ?", (plan_id, customer_id)
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"customer {customer_id} not found")
        self.conn.commit()


class JobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, customer_id: int, name: str, priority: int = 0) -> Job:
        ts = now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO jobs (customer_id, name, status, priority, retry_count, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (customer_id, name, JobStatus.PENDING.value, priority, ts, ts),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, job_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return _row_to_job(row)

    def list_for_customer(self, customer_id: int) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE customer_id = ? ORDER BY id", (customer_id,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def count_active_for_customer(self, customer_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE customer_id = ? AND status IN (?, ?)",
            (customer_id, JobStatus.PENDING.value, JobStatus.RUNNING.value),
        ).fetchone()
        return row["n"]
