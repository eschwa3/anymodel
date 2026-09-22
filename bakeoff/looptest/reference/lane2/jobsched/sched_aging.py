"""Priority aging for the pending queue: old pending jobs outrank fresh ones.

New scheduler feature; owns no storage and modifies no existing module.
Time is injected by the caller -- nothing here reads the wall clock.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from jobsched.errors import ValidationError
from jobsched.models import Job, JobStatus

_MICROSECOND = timedelta(microseconds=1)


def effective_priority(job: Job, *, now: datetime, step_s: int, boost: int, max_boost: int) -> int:
    """Return `job.priority` plus the aging boost earned since `created_at`.

    The boost is `min(max_boost, floor(age_seconds / step_s) * boost)` where
    `age_seconds` is the seconds from `created_at` to `now`, clamped at 0.
    """
    _validate(step_s, boost, max_boost)
    age_us = max(0, (_as_utc(now) - _as_utc(job.created_at)) // _MICROSECOND)
    steps = age_us // (step_s * 1_000_000)
    return job.priority + min(max_boost, steps * boost)


def pick_next(
    conn: sqlite3.Connection, *, now: datetime, step_s: int, boost: int, max_boost: int
) -> Job | None:
    """Return the pending job with the highest effective priority, or None.

    Read-only: nothing is reserved, updated, or committed. Ties on effective
    priority go to the oldest `created_at`, then the lowest id.
    """
    _validate(step_s, boost, max_boost)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ?",
        (JobStatus.PENDING.value,),
    ).fetchall()
    jobs = [
        Job(
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
        for row in rows
    ]
    if not jobs:
        return None
    return min(
        jobs,
        key=lambda job: (
            -effective_priority(job, now=now, step_s=step_s, boost=boost, max_boost=max_boost),
            _as_utc(job.created_at),
            job.id,
        ),
    )


def _validate(step_s: int, boost: int, max_boost: int) -> None:
    if step_s <= 0:
        raise ValidationError("step_s must be positive")
    if boost < 0:
        raise ValidationError("boost must not be negative")
    if max_boost < 0:
        raise ValidationError("max_boost must not be negative")


def _as_utc(value: str | datetime) -> datetime:
    """Parse ISO text or pass a datetime through, normalized to aware UTC."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
