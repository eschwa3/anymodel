from __future__ import annotations

from jobsched import scheduler
from jobsched.config import load_config
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository


def _make_job(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, "render-frame")


def test_reserve_next_job_claims_pending(conn):
    cfg = load_config()
    job = _make_job(conn)
    reserved = scheduler.reserve_next_job(conn, "worker-1", cfg)
    assert reserved.id == job.id
    assert reserved.status == JobStatus.RUNNING
    assert reserved.reserved_by == "worker-1"


def test_reserve_next_job_empty_queue_returns_none(conn):
    cfg = load_config()
    assert scheduler.reserve_next_job(conn, "worker-1", cfg) is None


def test_complete_job_marks_done_and_releases(conn):
    cfg = load_config()
    job = _make_job(conn)
    scheduler.reserve_next_job(conn, "worker-1", cfg)
    done = scheduler.complete_job(conn, job.id)
    assert done.status == JobStatus.DONE
    assert done.reserved_by is None


def test_fail_job_retries_under_limit(conn):
    cfg = load_config({"max_job_retries": 3})
    job = _make_job(conn)
    scheduler.reserve_next_job(conn, "worker-1", cfg)
    failed = scheduler.fail_job(conn, job.id, cfg)
    assert failed.status == JobStatus.PENDING
    assert failed.retry_count == 1
    assert failed.reserved_by is None


def test_fail_job_reaches_limit_marks_dead(conn):
    cfg = load_config({"max_job_retries": 3})
    job = _make_job(conn)
    result = None
    for _ in range(3):
        scheduler.reserve_next_job(conn, "worker-1", cfg)
        result = scheduler.fail_job(conn, job.id, cfg)
    assert result.status == JobStatus.DEAD
    assert result.retry_count == 3
