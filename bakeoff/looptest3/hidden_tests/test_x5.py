"""Merged hidden tests for feature X5 (X5.1 aging, X5.2 stuck, X5.3 health).

Combined from the lane-2 per-feature hidden tests; every original assertion
is kept. The only collisions renamed were the health tests' `NOW` constant
(now `NOW_UTC`, the stuck tests keep the naive `NOW`) and the shared
`_make_job` helper, which grew a `name` parameter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jobsched import sched_health
from jobsched.errors import ValidationError
from jobsched.models import Job, JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository
from jobsched.sched_aging import effective_priority, pick_next
from jobsched.sched_stuck import find_stuck, release_stuck

BASE = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
NOW = datetime(2024, 6, 1, 12, 0, 0)  # injected clock; no wall-clock reads
NOW_UTC = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_job(conn, name="render-frame", priority=0):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, name, priority)


def _fresh(conn, job):
    return JobRepository(conn).get(job.id)


def _set_age(conn, job_id, created_at):
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job_id))
    conn.commit()


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


def _set_job_state(conn, job, status=None, created_at=None):
    if status is not None:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job.id))
    if created_at is not None:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job.id))
    conn.commit()


# --------------------------------------------------------------------------
# X5.1 Priority aging (from L2-F2)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# X5.2 Stuck reservation finder (from L2-F3)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# X5.3 Queue health report (from L2-F4)
# --------------------------------------------------------------------------


def test_empty_queue_reports_zero_counts_and_none_age(conn):
    report = sched_health.queue_report(conn, now=NOW_UTC)
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
    report = sched_health.queue_report(conn, now=NOW_UTC)
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
    report = sched_health.queue_report(conn, now=NOW_UTC)
    assert report["oldest_pending_age_s"] == 90  # 90.5 s -> floored (rule 2)


def test_age_never_negative_when_now_before_created_at(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, created_at="2024-06-01T12:00:05")
    report = sched_health.queue_report(conn, now=NOW_UTC)
    assert report["oldest_pending_age_s"] == 0


def test_none_age_when_no_job_is_pending(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, status=JobStatus.RUNNING)
    report = sched_health.queue_report(conn, now=NOW_UTC)
    assert report["oldest_pending_age_s"] is None


def test_naive_now_raises_validation_error(conn):
    with pytest.raises(ValidationError):
        sched_health.queue_report(conn, now=datetime(2024, 6, 1, 12, 0, 0))


def test_unparseable_created_at_raises_validation_error(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, created_at="not-a-timestamp")
    with pytest.raises(ValidationError):
        sched_health.queue_report(conn, now=NOW_UTC)


def test_format_report_exact_lines(conn):
    empty = sched_health.format_report(sched_health.queue_report(conn, now=NOW_UTC))
    assert empty == (
        "total: 0\npending: 0\nrunning: 0\ndone: 0\nfailed: 0\ndead: 0\noldest pending: none"
    )
    pending = _make_job(conn)
    done = _make_job(conn, "thumbnail")
    dead = _make_job(conn, "caption")
    _set_job_state(conn, pending, created_at="2024-06-01T11:58:30")
    _set_job_state(conn, done, status=JobStatus.DONE)
    _set_job_state(conn, dead, status=JobStatus.DEAD)
    report = sched_health.format_report(sched_health.queue_report(conn, now=NOW_UTC))
    assert report == (
        "total: 3\npending: 1\nrunning: 0\ndone: 1\nfailed: 0\ndead: 1\noldest pending: 90"
    )
