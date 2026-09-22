"""Filtered, stably ordered job listing plus a tiny operator CLI.

`list_jobs` is the library surface; `main` is the command-line surface.
Both are read-only over the jobs table (the only write is applying
migrations, exactly like `jobsched.cli.main` does).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from jobsched import db
from jobsched.errors import JobSchedError, ValidationError
from jobsched.models import Job, JobStatus
from jobsched.repository import CustomerRepository


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


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    customer_id: int | None = None,
    limit: int = 50,
) -> list[Job]:
    """Return matching jobs ordered by priority DESC, then id ASC."""
    if limit <= 0:
        raise ValidationError("limit must be >= 1")
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        try:
            params.append(JobStatus(status).value)
        except ValueError:
            raise ValidationError(f"invalid status: {status!r}") from None
        clauses.append("status = ?")
    if customer_id is not None:
        CustomerRepository(conn).get(customer_id)  # NotFoundError if missing
        clauses.append("customer_id = ?")
        params.append(customer_id)
    sql = "SELECT * FROM jobs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY priority DESC, id ASC LIMIT ?"
    params.append(limit)
    return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsched-joblist")
    parser.add_argument("--db", default="jobsched.db")
    parser.add_argument("--status", default=None)
    parser.add_argument("--customer-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=50)
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    conn = db.connect(args.db)
    db.apply_migrations(conn)
    try:
        jobs = list_jobs(conn, status=args.status, customer_id=args.customer_id, limit=args.limit)
    except JobSchedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for job in jobs:
        print(f"job {job.id} {job.status.value} {job.name}")
    return 0
