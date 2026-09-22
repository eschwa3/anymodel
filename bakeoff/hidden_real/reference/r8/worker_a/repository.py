"""sqlite-backed repositories. Every query is parameterized -- see AGENTS.md."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from jobsched.errors import NotFoundError
from jobsched.models import Customer, Invoice, InvoiceLineItem, Job, JobStatus, Plan, PlanChange
from jobsched.utils.time import now


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t for t in raw.split(",") if t]


def _dump_tags(tags: list[str]) -> str:
    return ",".join(tags)


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
    keys = row.keys()
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
        tags=_parse_tags(row["tags"]) if "tags" in keys else [],
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

    def create(self, customer_id: int, name: str, priority: int = 0, tags: list[str] | None = None) -> Job:
        ts = now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO jobs (customer_id, name, status, priority, retry_count, "
            "created_at, updated_at, tags) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (customer_id, name, JobStatus.PENDING.value, priority, ts, ts, _dump_tags(tags or [])),
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

    def reserve_next(self, worker_id: str, lease_seconds: int) -> Job | None:
        """Atomically claim the highest-priority pending job, if any."""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY priority DESC, id ASC LIMIT 1",
            (JobStatus.PENDING.value,),
        ).fetchone()
        if row is None:
            return None
        lease_until = (now() + timedelta(seconds=lease_seconds)).isoformat()
        ts = now().isoformat()
        self.conn.execute(
            "UPDATE jobs SET status = ?, reserved_by = ?, reserved_until = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (JobStatus.RUNNING.value, worker_id, lease_until, ts, row["id"], JobStatus.PENDING.value),
        )
        self.conn.commit()
        return self.get(row["id"])

    def mark_done(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (JobStatus.DONE.value, now().isoformat(), job_id),
        )
        self.conn.commit()

    def mark_failed_retry(self, job_id: int, retry_count: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, retry_count = ?, updated_at = ? WHERE id = ?",
            (JobStatus.PENDING.value, retry_count, now().isoformat(), job_id),
        )
        self.conn.commit()

    def mark_dead(self, job_id: int, retry_count: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, retry_count = ?, updated_at = ? WHERE id = ?",
            (JobStatus.DEAD.value, retry_count, now().isoformat(), job_id),
        )
        self.conn.commit()

    def release_reservation(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET reserved_by = NULL, reserved_until = NULL WHERE id = ?",
            (job_id,),
        )
        self.conn.commit()

    def search_by_name(self, fragment: str) -> list[Job]:
        pattern = f"%{fragment}%"
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE name LIKE ? ORDER BY id", (pattern,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def add_tag(self, job_id: int, tag: str) -> Job:
        job = self.get(job_id)
        if tag not in job.tags:
            new_tags = [*job.tags, tag]
            self.conn.execute(
                "UPDATE jobs SET tags = ? WHERE id = ?", (_dump_tags(new_tags), job_id)
            )
            self.conn.commit()
        return self.get(job_id)

    def jobs_with_tag(self, tag: str) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [j for j in (_row_to_job(r) for r in rows) if tag in j.tags]


class PlanChangeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, customer_id: int, old_plan_id: int, new_plan_id: int, effective_at: str) -> PlanChange:
        cur = self.conn.execute(
            "INSERT INTO plan_changes (customer_id, old_plan_id, new_plan_id, effective_at) "
            "VALUES (?, ?, ?, ?)",
            (customer_id, old_plan_id, new_plan_id, effective_at),
        )
        self.conn.commit()
        return PlanChange(
            id=cur.lastrowid,
            customer_id=customer_id,
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id,
            effective_at=effective_at,
        )

    def list_for_period(self, customer_id: int, period_start: str, period_end: str) -> list[PlanChange]:
        rows = self.conn.execute(
            "SELECT * FROM plan_changes WHERE customer_id = ? AND effective_at >= ? "
            "AND effective_at < ? ORDER BY effective_at",
            (customer_id, period_start, period_end),
        ).fetchall()
        return [
            PlanChange(
                id=r["id"],
                customer_id=r["customer_id"],
                old_plan_id=r["old_plan_id"],
                new_plan_id=r["new_plan_id"],
                effective_at=r["effective_at"],
            )
            for r in rows
        ]


class InvoiceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, invoice: Invoice) -> Invoice:
        cur = self.conn.execute(
            "INSERT INTO invoices (customer_id, period_start, period_end, subtotal_cents, "
            "tax_cents, total_cents, currency, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invoice.customer_id,
                invoice.period_start,
                invoice.period_end,
                invoice.subtotal_cents,
                invoice.tax_cents,
                invoice.total_cents,
                invoice.currency,
                invoice.created_at or now().isoformat(),
            ),
        )
        invoice_id = cur.lastrowid
        for item in invoice.line_items:
            self.conn.execute(
                "INSERT INTO invoice_line_items (invoice_id, description, amount_cents, quantity) "
                "VALUES (?, ?, ?, ?)",
                (invoice_id, item.description, item.amount_cents, item.quantity),
            )
        self.conn.commit()
        invoice.id = invoice_id
        return invoice

    def get(self, invoice_id: int) -> Invoice:
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"invoice {invoice_id} not found")
        item_rows = self.conn.execute(
            "SELECT * FROM invoice_line_items WHERE invoice_id = ? ORDER BY id", (invoice_id,)
        ).fetchall()
        items = [
            InvoiceLineItem(description=r["description"], amount_cents=r["amount_cents"], quantity=r["quantity"])
            for r in item_rows
        ]
        return Invoice(
            id=row["id"],
            customer_id=row["customer_id"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            subtotal_cents=row["subtotal_cents"],
            tax_cents=row["tax_cents"],
            total_cents=row["total_cents"],
            currency=row["currency"],
            created_at=row["created_at"],
            line_items=items,
        )
