from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from jobsched.errors import ValidationError
from jobsched.models import Job, JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository
from jobsched.sched_stuck import find_stuck, release_stuck

NOW = datetime(2024, 6, 1, 12, 0, 0)  # injected clock; no wall-clock reads


def _make_job(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, "render-frame")


def _reserve_at(conn, job_id, reserved_at, retry_count=0):
    # The exact state JobRepository.reserve_next leaves behind, but with a
    # fixed clock: updated_at is the reservation time, reserved_until the
    # lease deadline (reservation + 300 s, the app's default lease).
    conn.execute(
        "UPDATE jobs SET status = ?, reserved_by = ?, reserved_until = ?, "
        "retry_count = ?, updated_at = ? WHERE id = ?",
        (
            JobStatus.RUNNING.value,
            "worker-1",
            (reserved_at + timedelta(seconds=300)).isoformat(),
            retry_count,
            reserved_at.isoformat(),
            job_id,
        ),
    )
    conn.commit()


def _set_status(conn, job_id, status, updated_at):
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, updated_at, job_id),
    )
    conn.commit()


def test_find_stuck_returns_old_running_jobs_oldest_first(conn):
    older = _make_job(conn)
    newer = _make_job(conn)
    _reserve_at(conn, older.id, NOW - timedelta(hours=2))
    _reserve_at(conn, newer.id, NOW - timedelta(hours=1))
    fresh = _make_job(conn)
    _reserve_at(conn, fresh.id, NOW)
    done = _make_job(conn)
    _set_status(conn, done.id, JobStatus.DONE, "2020-01-01T00:00:00")

    stuck = find_stuck(conn, now=NOW, max_age_s=1800)
    assert [j.id for j in stuck] == [older.id, newer.id]  # oldest first
    for job in stuck:
        assert isinstance(job, Job)
        assert job.status == JobStatus.RUNNING
        assert job.reserved_by == "worker-1"


def test_find_stuck_breaks_ties_by_id(conn):
    first = _make_job(conn)
    second = _make_job(conn)
    _reserve_at(conn, first.id, NOW - timedelta(hours=3))
    _reserve_at(conn, second.id, NOW - timedelta(hours=3))
    stuck = find_stuck(conn, now=NOW, max_age_s=60)
    assert [j.id for j in stuck] == [first.id, second.id]


def test_exactly_max_age_is_not_stuck_but_a_microsecond_more_is(conn):
    job = _make_job(conn)
    _reserve_at(conn, job.id, NOW - timedelta(seconds=1800))
    assert find_stuck(conn, now=NOW, max_age_s=1800) == []
    assert release_stuck(conn, now=NOW, max_age_s=1800) == 0

    conn.execute(
        "UPDATE jobs SET updated_at = ? WHERE id = ?",
        ((NOW - timedelta(seconds=1800, microseconds=1)).isoformat(), job.id),
    )
    conn.commit()
    assert [j.id for j in find_stuck(conn, now=NOW, max_age_s=1800)] == [job.id]


def test_release_stuck_returns_job_to_pending_and_clears_reservation(conn):
    job = _make_job(conn)
    _reserve_at(conn, job.id, NOW - timedelta(hours=1), retry_count=2)
    assert release_stuck(conn, now=NOW, max_age_s=60) == 1
    after = JobRepository(conn).get(job.id)
    assert after.status == JobStatus.PENDING
    assert after.reserved_by is None
    assert after.reserved_until is None
    assert after.updated_at == NOW.isoformat()
    assert after.retry_count == 2  # not a retry: count untouched
    assert after.priority == 0
    assert after.name == "render-frame"


def test_release_stuck_leaves_non_running_jobs_alone(conn):
    job_ids = []
    for status in (JobStatus.PENDING, JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD):
        job = _make_job(conn)
        _set_status(conn, job.id, status, "2020-01-01T00:00:00")
        job_ids.append((status, job.id))

    assert find_stuck(conn, now=NOW, max_age_s=60) == []
    assert release_stuck(conn, now=NOW, max_age_s=60) == 0
    for status, job_id in job_ids:
        row = conn.execute("SELECT status, updated_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == status.value
        assert row["updated_at"] == "2020-01-01T00:00:00"  # untouched


def test_no_stuck_jobs_means_empty_list_and_zero(conn):
    assert find_stuck(conn, now=NOW, max_age_s=60) == []
    assert release_stuck(conn, now=NOW, max_age_s=60) == 0


@pytest.mark.parametrize("max_age_s", [0, -1])
def test_invalid_max_age_raises_validation_error(conn, max_age_s):
    with pytest.raises(ValidationError):
        find_stuck(conn, now=NOW, max_age_s=max_age_s)
    with pytest.raises(ValidationError):
        release_stuck(conn, now=NOW, max_age_s=max_age_s)
