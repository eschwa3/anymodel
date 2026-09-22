from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jobsched.errors import ValidationError
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository
from jobsched.sched_aging import effective_priority, pick_next

BASE = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_job(conn, priority=0):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, "render-frame", priority)


def _fresh(conn, job):
    return JobRepository(conn).get(job.id)


def _set_age(conn, job_id, created_at):
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job_id))
    conn.commit()


def test_age_zero_returns_base_priority(conn):
    job = _make_job(conn, priority=5)
    _set_age(conn, job.id, "2025-06-01T12:00:00+00:00")
    assert effective_priority(_fresh(conn, job), now=BASE, step_s=60, boost=2, max_boost=10) == 5
    _set_age(conn, job.id, "2025-06-01T12:00:01+00:00")  # created after `now`
    assert effective_priority(_fresh(conn, job), now=BASE, step_s=60, boost=2, max_boost=10) == 5
    _set_age(conn, job.id, "2025-06-01T11:58:00+00:00")  # age 120s; naive now counts as UTC
    assert (
        effective_priority(
            _fresh(conn, job), now=datetime(2025, 6, 1, 12, 0, 0), step_s=60, boost=2, max_boost=10
        )
        == 9
    )


def test_effective_priority_ages_in_steps(conn):
    job = _make_job(conn, priority=5)
    _set_age(conn, job.id, "2025-06-01T11:57:30+00:00")  # 150s -> floor(150/60) = 2 steps
    assert effective_priority(_fresh(conn, job), now=BASE, step_s=60, boost=2, max_boost=100) == 9


def test_exact_step_boundary_then_cap(conn):
    job = _make_job(conn, priority=5)
    _set_age(conn, job.id, "2025-06-01T11:59:00+00:00")  # age exactly 60s -> 1 step
    assert effective_priority(_fresh(conn, job), now=BASE, step_s=60, boost=2, max_boost=100) == 7
    _set_age(conn, job.id, "2025-06-01T10:00:00+00:00")  # 7200s -> 120 steps, over the cap
    assert effective_priority(_fresh(conn, job), now=BASE, step_s=60, boost=2, max_boost=7) == 12


def test_pick_next_lets_aged_job_overtake(conn):
    old = _make_job(conn, priority=1)
    fresh = _make_job(conn, priority=3)
    _set_age(conn, old.id, "2025-06-01T11:50:00+00:00")  # 600s -> 1 + 10*2 = 21
    _set_age(conn, fresh.id, "2025-06-01T12:00:00+00:00")  # age 0 -> 3
    picked = pick_next(conn, now=BASE, step_s=60, boost=2, max_boost=100)
    assert picked.id == old.id


def test_pick_next_tie_broken_by_oldest_then_lowest_id(conn):
    first = _make_job(conn, priority=2)
    second = _make_job(conn, priority=2)
    _set_age(conn, first.id, "2025-06-01T12:00:00+00:00")
    _set_age(conn, second.id, "2025-06-01T12:00:00+00:00")
    assert pick_next(conn, now=BASE, step_s=60, boost=0, max_boost=5).id == first.id
    _set_age(conn, second.id, "2025-06-01T11:50:00+00:00")  # older -> wins the tie
    assert pick_next(conn, now=BASE, step_s=60, boost=0, max_boost=5).id == second.id


def test_pick_next_skips_non_pending_jobs(conn):
    assert pick_next(conn, now=BASE, step_s=60, boost=2, max_boost=5) is None
    running = _make_job(conn)
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.RUNNING.value, running.id))
    conn.commit()
    assert pick_next(conn, now=BASE, step_s=60, boost=2, max_boost=5) is None


def test_pick_next_is_read_only_and_repeatable(conn):
    job = _make_job(conn, priority=4)
    _set_age(conn, job.id, "2025-06-01T11:50:00+00:00")
    before = JobRepository(conn).get(job.id)
    picked = pick_next(conn, now=BASE, step_s=60, boost=2, max_boost=5)
    after = JobRepository(conn).get(job.id)
    assert picked.id == job.id
    assert after.status == JobStatus.PENDING
    assert after.reserved_by is None and after.reserved_until is None
    assert after.updated_at == before.updated_at
    assert pick_next(conn, now=BASE, step_s=60, boost=2, max_boost=5).id == job.id


@pytest.mark.parametrize(
    ("step_s", "boost", "max_boost"),
    [(0, 1, 5), (-60, 1, 5), (60, -1, 5), (60, 1, -5)],
)
def test_invalid_parameters_raise_validation_error(conn, step_s, boost, max_boost):
    job = _make_job(conn)
    with pytest.raises(ValidationError):
        effective_priority(_fresh(conn, job), now=BASE, step_s=step_s, boost=boost, max_boost=max_boost)
    with pytest.raises(ValidationError):
        pick_next(conn, now=BASE, step_s=step_s, boost=boost, max_boost=max_boost)
