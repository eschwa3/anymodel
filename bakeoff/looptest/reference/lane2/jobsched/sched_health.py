"""Queue health report: per-status counts and the oldest pending job's age.

Read-only reporting module; owns no storage and modifies no existing module.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime

from jobsched.errors import ValidationError
from jobsched.models import JobStatus

_STATUS_VALUES = [status.value for status in JobStatus]


def queue_report(conn: sqlite3.Connection, *, now: datetime) -> dict:
    """Snapshot the queue: counts per status, total, and pending-job age."""
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValidationError("now must be a timezone-aware datetime")
    counts = {value: 0 for value in _STATUS_VALUES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        if row["status"] in counts:
            counts[row["status"]] = row["n"]
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "dead_count": counts[JobStatus.DEAD.value],
        "oldest_pending_age_s": _oldest_pending_age(conn, now),
    }


def format_report(report: dict) -> str:
    """Render a `queue_report` dict as fixed-order text lines."""
    lines = [f"total: {report['total']}"]
    lines += [f"{value}: {report['counts'][value]}" for value in _STATUS_VALUES]
    age = report["oldest_pending_age_s"]
    lines.append("oldest pending: none" if age is None else f"oldest pending: {age}")
    return "\n".join(lines)


def _oldest_pending_age(conn: sqlite3.Connection, now: datetime) -> int | None:
    rows = conn.execute(
        "SELECT created_at FROM jobs WHERE status = ?", (JobStatus.PENDING.value,)
    ).fetchall()
    created = [_parse_created_at(row["created_at"]) for row in rows]
    if not created:
        return None
    return max(0, math.floor((now - min(created)).total_seconds()))


def _parse_created_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError("job created_at is not an ISO timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
