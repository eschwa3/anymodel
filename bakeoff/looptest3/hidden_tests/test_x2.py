"""Hidden acceptance tests for X2 (retry backoff schedule)."""

from __future__ import annotations

from datetime import datetime

import pytest
from jobsched import scheduler
from jobsched.config import load_config
from jobsched.errors import ValidationError
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository
from jobsched.sched_backoff import backoff_delay_s, eligible_jobs, next_eligible_at


def _make_job(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, "render-frame")


def _set_job_state(conn, job_id, *, retry_count, updated_at, status=None):
    conn.execute(
        "UPDATE jobs SET retry_count = ?, updated_at = ? WHERE id = ?",
        (retry_count, updated_at, job_id),
    )
    if status is not None:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def _failed_job(conn, cfg, updated_at):
    """A job that failed once and is awaiting retry (pending, retry_count 1)."""
    job = _make_job(conn)
    _set_job_state(conn, job.id, retry_count=1, updated_at=updated_at, status="pending")
    return JobRepository(conn).get(job.id)


def test_backoff_delay_doubles_per_retry_and_caps():
    assert [backoff_delay_s(n, base_s=30, cap_s=1000) for n in (1, 2, 3)] == [30, 60, 120]
    assert backoff_delay_s(3, base_s=30, cap_s=100) == 100  # 120 capped at 100
    assert backoff_delay_s(10, base_s=30, cap_s=100) == 100
    assert backoff_delay_s(5, base_s=30, cap_s=30) == 30  # cap == base is allowed


@pytest.mark.parametrize(
    ("retry_count", "base_s", "cap_s"),
    [(0, 30, 60), (-2, 30, 60), (1, 0, 60), (1, -5, 60), (1, 60, 30)],
)
def test_invalid_backoff_arguments_raise_validation_error(retry_count, base_s, cap_s):
    with pytest.raises(ValidationError):
        backoff_delay_s(retry_count, base_s=base_s, cap_s=cap_s)


def test_next_eligible_at_measures_from_updated_at(conn):
    cfg = load_config({"max_job_retries": 4})
    job = _make_job(conn)
    for _ in range(2):  # two failed attempts -> retry_count 2, still pending
        scheduler.reserve_next_job(conn, "worker-1", cfg)
        scheduler.fail_job(conn, job.id, cfg)
    _set_job_state(conn, job.id, retry_count=2, updated_at="2024-06-01T12:00:00")
    stored = JobRepository(conn).get(job.id)
    eligible = next_eligible_at(stored, base_s=30, cap_s=50)
    assert eligible == datetime(2024, 6, 1, 12, 0, 50)  # min(30 * 2, 50) = 50s
    assert eligible.tzinfo is None  # naive, same clock as stored stamps


def test_next_eligible_at_rejects_fresh_pending_job(conn):
    job = _make_job(conn)  # pending, retry_count 0
    with pytest.raises(ValidationError):
        next_eligible_at(job, base_s=30, cap_s=60)


def test_eligible_jobs_boundary_exactly_elapsed_is_included(conn):
    cfg = load_config()
    job = _failed_job(conn, cfg, "2024-06-01T12:00:00")  # retry 1, base 30 -> 12:00:30
    due = eligible_jobs(conn, now=datetime(2024, 6, 1, 12, 0, 30), base_s=30, cap_s=600)
    assert [j.id for j in due] == [job.id]
    assert eligible_jobs(conn, now=datetime(2024, 6, 1, 12, 0, 29), base_s=30, cap_s=600) == []


def test_eligible_jobs_only_failed_awaiting_retry(conn):
    cfg = load_config()
    failed = _failed_job(conn, cfg, "2024-06-01T12:00:00")
    fresh = _make_job(conn)
    _set_job_state(conn, fresh.id, retry_count=0, updated_at="2024-06-01T00:00:00")
    done_job = _make_job(conn)
    _set_job_state(
        conn, done_job.id, retry_count=1, updated_at="2024-06-01T00:00:00", status="done"
    )
    dead = _make_job(conn)
    _set_job_state(conn, dead.id, retry_count=3, updated_at="2024-06-01T00:00:00", status="dead")
    due = eligible_jobs(conn, now=datetime(2024, 6, 1, 13, 0, 0), base_s=30, cap_s=600)
    assert [j.id for j in due] == [failed.id]
    assert due[0].status == JobStatus.PENDING and due[0].retry_count == 1


def test_eligible_jobs_orders_by_eligible_time_then_id(conn):
    cfg = load_config()
    a = _failed_job(conn, cfg, "2024-06-01T12:05:00")  # eligible 12:06
    b = _failed_job(conn, cfg, "2024-06-01T12:00:00")  # eligible 12:01
    c = _failed_job(conn, cfg, "2024-06-01T12:05:00")  # eligible 12:06 (ties with a)
    due = eligible_jobs(conn, now=datetime(2024, 6, 1, 12, 10, 0), base_s=60, cap_s=600)
    assert [j.id for j in due] == [b.id, a.id, c.id]  # time first, then id on ties


def test_eligible_jobs_validates_backoff_even_when_empty(conn):
    now = datetime(2024, 6, 1, 12, 0, 0)
    with pytest.raises(ValidationError):
        eligible_jobs(conn, now=now, base_s=0, cap_s=100)
    with pytest.raises(ValidationError):
        eligible_jobs(conn, now=now, base_s=60, cap_s=30)
    assert eligible_jobs(conn, now=now, base_s=30, cap_s=600) == []
