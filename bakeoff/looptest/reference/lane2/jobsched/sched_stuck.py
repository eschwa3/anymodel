"""Finder/releaser for jobs stuck in the running state with a stale reservation.

New scheduler feature; owns no storage and modifies no existing module. All
time is injected: `now` must come from the same clock `JobRepository` stamps
`updated_at` with (see AGENTS.md).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from jobsched.errors import ValidationError
from jobsched.models import Job, JobStatus

_RUNNING = JobStatus.RUNNING.value
_PENDING = JobStatus.PENDING.value


def _validate_max_age(max_age_s: int) -> None:
    if max_age_s <= 0:
        raise ValidationError("max_age_s must be positive")


def _cutoff(now: datetime, max_age_s: int) -> str:
    """ISO timestamp a running job must predate (strictly) to count as stuck."""
    return (now - timedelta(seconds=max_age_s)).isoformat()


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


def find_stuck(conn: sqlite3.Connection, *, now: datetime, max_age_s: int) -> list[Job]:
    """Jobs reserved (running) for more than `max_age_s` seconds as of `now`.

    `JobRepository.reserve_next` stamps `updated_at` in the same UPDATE that
    sets the status to running, so `updated_at` is the reservation time.
    Oldest reservation first, ties by id.
    """
    _validate_max_age(max_age_s)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? AND updated_at < ? ORDER BY updated_at, id",
        (_RUNNING, _cutoff(now, max_age_s)),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def release_stuck(conn: sqlite3.Connection, *, now: datetime, max_age_s: int) -> int:
    """Return every stuck job to pending; return how many were released.

    One parameterized UPDATE: back to pending, stale reservation cleared (the
    post-state `scheduler.fail_job` leaves a retried job in), `updated_at`
    bumped to `now`. `retry_count` is untouched -- `mark_failed_retry` would
    stamp the wall clock, so it cannot be reused here.
    """
    _validate_max_age(max_age_s)
    cur = conn.execute(
        "UPDATE jobs SET status = ?, reserved_by = NULL, reserved_until = NULL, updated_at = ? "
        "WHERE status = ? AND updated_at < ?",
        (_PENDING, now.isoformat(), _RUNNING, _cutoff(now, max_age_s)),
    )
    conn.commit()
    return cur.rowcount
