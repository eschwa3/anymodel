"""Retry backoff schedule for failed jobs awaiting their next retry.

Read-only over the jobs table; owns no storage and modifies no existing
module. `jobsched.scheduler.fail_job` puts a retriable failed job back into
`JobStatus.PENDING` with `retry_count >= 1` (see
`JobRepository.mark_failed_retry`), stamping `updated_at` at failure time;
that status + counter pair is what "failed, awaiting retry" means here, and
`updated_at` is the timestamp the backoff is measured from.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from jobsched.errors import ValidationError
from jobsched.models import Job, JobStatus


def backoff_delay_s(retry_count: int, *, base_s: int, cap_s: int) -> int:
    """Seconds to wait before attempt `retry_count`: base_s doubled per retry, capped."""
    _validate_base_cap(base_s, cap_s)
    if retry_count < 1:
        raise ValidationError("retry_count must be >= 1")
    return min(base_s * 2 ** (retry_count - 1), cap_s)


def next_eligible_at(job: Job, *, base_s: int, cap_s: int) -> datetime:
    """When `job` (failed, awaiting retry) may be retried.

    Measured from `updated_at`, which `JobRepository.mark_failed_retry`
    stamps with `utils.time.now().isoformat()` (naive local ISO), so the
    result is naive in the same clock as stored timestamps.
    """
    failed_at = datetime.fromisoformat(job.updated_at)
    delay_s = backoff_delay_s(job.retry_count, base_s=base_s, cap_s=cap_s)
    return failed_at + timedelta(seconds=delay_s)


def eligible_jobs(conn: sqlite3.Connection, *, now: datetime, base_s: int, cap_s: int) -> list[Job]:
    """Failed-awaiting-retry jobs whose backoff has elapsed by `now`.

    Exactly elapsed counts as eligible. Ordered by next-eligible time, then
    id. Read-only.
    """
    _validate_base_cap(base_s, cap_s)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? AND retry_count >= ?",
        (JobStatus.PENDING.value, 1),
    ).fetchall()
    due = [
        (job, next_eligible_at(job, base_s=base_s, cap_s=cap_s))
        for job in (_row_to_job(row) for row in rows)
    ]
    due = [pair for pair in due if pair[1] <= now]
    due.sort(key=lambda pair: (pair[1], pair[0].id))
    return [job for job, _ in due]


def _validate_base_cap(base_s: int, cap_s: int) -> None:
    if base_s <= 0:
        raise ValidationError("base_s must be > 0")
    if cap_s < base_s:
        raise ValidationError("cap_s must not be less than base_s")


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
