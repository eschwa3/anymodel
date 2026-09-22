"""Plain-function request handlers -- the boundary a real HTTP framework
would sit in front of. Each takes a plain dict payload and returns a plain
dict response; no framework types leak in here.
"""

from __future__ import annotations

import sqlite3

from jobsched import service
from jobsched.errors import JobSchedError
from jobsched.repository import JobRepository
from jobsched.utils.time import now as clock


def handle_create_job(conn: sqlite3.Connection, payload: dict) -> dict:
    try:
        job = service.create_job(
            conn,
            customer_id=int(payload["customer_id"]),
            name=str(payload["name"]),
            priority=int(payload.get("priority", 0)),
        )
    except JobSchedError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "job_id": job.id, "status": job.status.value, "received_at": clock().isoformat()}


def handle_get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    try:
        job = JobRepository(conn).get(job_id)
    except JobSchedError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status.value,
        "retry_count": job.retry_count,
        "reserved_by": job.reserved_by,
    }


def handle_change_plan(conn: sqlite3.Connection, payload: dict) -> dict:
    try:
        customer = service.change_plan(
            conn, customer_id=int(payload["customer_id"]), new_plan_id=int(payload["new_plan_id"])
        )
    except JobSchedError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "customer_id": customer.id, "plan_id": customer.plan_id}
