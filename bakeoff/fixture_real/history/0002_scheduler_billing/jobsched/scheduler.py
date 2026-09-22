"""Job reservation lifecycle: reserve, complete, fail/retry."""

from __future__ import annotations

import sqlite3

from jobsched.config import AppConfig
from jobsched.models import Job
from jobsched.repository import JobRepository


def reserve_next_job(conn: sqlite3.Connection, worker_id: str, cfg: AppConfig) -> Job | None:
    """Claim the next pending job for `worker_id`, or None if the queue is empty."""
    repo = JobRepository(conn)
    return repo.reserve_next(worker_id, cfg.reservation_lease_seconds)


def complete_job(conn: sqlite3.Connection, job_id: int) -> Job:
    """Mark a running job done and release its reservation."""
    repo = JobRepository(conn)
    repo.mark_done(job_id)
    repo.release_reservation(job_id)
    return repo.get(job_id)


def fail_job(conn: sqlite3.Connection, job_id: int, cfg: AppConfig) -> Job:
    """Record a failed attempt: retry if under the limit, otherwise mark dead.

    Either way the reservation is released so the job can be picked up again
    (if retried) or is no longer counted against the customer's active quota
    (if dead).
    """
    repo = JobRepository(conn)
    job = repo.get(job_id)
    next_retry_count = job.retry_count + 1

    if next_retry_count >= cfg.max_job_retries:
        repo.mark_dead(job_id, next_retry_count)
    else:
        repo.mark_failed_retry(job_id, next_retry_count)
    repo.release_reservation(job_id)
    return repo.get(job_id)
