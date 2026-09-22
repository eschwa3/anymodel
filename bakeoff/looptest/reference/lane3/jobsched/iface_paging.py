"""Offset/limit pagination handler for the jobs table.

Same plain-dict handler style as `jobsched.handlers`: a dict payload in, a
dict response out, with domain errors surfaced as `{"ok": False, ...}`
instead of raised.
"""

from __future__ import annotations

import sqlite3

from jobsched.errors import JobSchedError, ValidationError
from jobsched.models import JobStatus

DEFAULT_OFFSET = 0
DEFAULT_LIMIT = 10


def handle_list_jobs_page(conn: sqlite3.Connection, payload: dict) -> dict:
    """Return one page of jobs plus the offset of the next page, if any."""
    try:
        try:
            offset = int(payload.get("offset", DEFAULT_OFFSET))
            limit = int(payload.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            raise ValidationError("offset and limit must be integers") from None
        if offset < 0:
            raise ValidationError("offset must be >= 0")
        if limit <= 0:
            raise ValidationError("limit must be >= 1")
    except JobSchedError as exc:
        return {"ok": False, "error": str(exc)}

    # One extra row detects whether a next page exists.
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id ASC LIMIT ? OFFSET ?",
        (limit + 1, offset),
    ).fetchall()
    items = [
        {
            "job_id": row["id"],
            "name": row["name"],
            "status": JobStatus(row["status"]).value,
            "priority": row["priority"],
        }
        for row in rows[:limit]
    ]
    has_next = len(rows) > limit
    next_offset = offset + len(items) if has_next else None
    return {"ok": True, "items": items, "next_offset": next_offset}
