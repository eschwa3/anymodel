from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jobsched import sched_health
from jobsched.errors import ValidationError
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_job(conn, name="render-frame"):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, name)


def _set_job_state(conn, job, status=None, created_at=None):
    if status is not None:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job.id))
    if created_at is not None:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job.id))
    conn.commit()


def test_empty_queue_reports_zero_counts_and_none_age(conn):
    report = sched_health.queue_report(conn, now=NOW)
    assert set(report) == {"counts", "total", "dead_count", "oldest_pending_age_s"}
    assert list(report["counts"]) == [s.value for s in JobStatus]
    assert all(n == 0 for n in report["counts"].values())
    assert report["total"] == 0
    assert report["dead_count"] == 0
    assert report["oldest_pending_age_s"] is None


def test_counts_total_and_dead_count(conn):
    _make_job(conn)
    done = _make_job(conn, "thumbnail")
    dead = _make_job(conn, "caption")
    _set_job_state(conn, done, status=JobStatus.DONE)
    _set_job_state(conn, dead, status=JobStatus.DEAD)
    report = sched_health.queue_report(conn, now=NOW)
    assert report["counts"] == {
        "pending": 1,
        "running": 0,
        "done": 1,
        "failed": 0,
        "dead": 1,
    }
    assert report["total"] == 3
    assert report["dead_count"] == 1


def test_oldest_pending_age_takes_earliest_and_floors(conn):
    older = _make_job(conn)
    newer = _make_job(conn, "thumbnail")
    _set_job_state(conn, older, created_at="2024-06-01T11:58:29.500000")
    _set_job_state(conn, newer, created_at="2024-06-01T11:59:00")
    report = sched_health.queue_report(conn, now=NOW)
    assert report["oldest_pending_age_s"] == 90  # 90.5 s -> floored (rule 2)


def test_age_never_negative_when_now_before_created_at(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, created_at="2024-06-01T12:00:05")
    report = sched_health.queue_report(conn, now=NOW)
    assert report["oldest_pending_age_s"] == 0


def test_none_age_when_no_job_is_pending(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, status=JobStatus.RUNNING)
    report = sched_health.queue_report(conn, now=NOW)
    assert report["oldest_pending_age_s"] is None


def test_naive_now_raises_validation_error(conn):
    with pytest.raises(ValidationError):
        sched_health.queue_report(conn, now=datetime(2024, 6, 1, 12, 0, 0))


def test_unparseable_created_at_raises_validation_error(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, created_at="not-a-timestamp")
    with pytest.raises(ValidationError):
        sched_health.queue_report(conn, now=NOW)


def test_format_report_exact_lines(conn):
    empty = sched_health.format_report(sched_health.queue_report(conn, now=NOW))
    assert empty == (
        "total: 0\npending: 0\nrunning: 0\ndone: 0\nfailed: 0\ndead: 0\noldest pending: none"
    )
    pending = _make_job(conn)
    done = _make_job(conn, "thumbnail")
    dead = _make_job(conn, "caption")
    _set_job_state(conn, pending, created_at="2024-06-01T11:58:30")
    _set_job_state(conn, done, status=JobStatus.DONE)
    _set_job_state(conn, dead, status=JobStatus.DEAD)
    report = sched_health.format_report(sched_health.queue_report(conn, now=NOW))
    assert report == (
        "total: 3\npending: 1\nrunning: 0\ndone: 1\nfailed: 0\ndead: 1\noldest pending: 90"
    )
