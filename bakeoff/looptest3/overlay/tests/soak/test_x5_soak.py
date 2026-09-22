"""Soak tests for the combined X5 feature set.

These run the three sub-features end-to-end through their public functions
(`jobsched.sched_aging`, `jobsched.sched_stuck`, `jobsched.sched_health`)
against a real sqlite database, using the same `conn` fixture as the rest of
the suite. Every test sleeps one scheduler tick, so the module takes about
5 minutes; that is the point of a soak run -- do not shorten it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from jobsched import sched_health
from jobsched.errors import ValidationError
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository
from jobsched.sched_aging import effective_priority, pick_next
from jobsched.sched_stuck import find_stuck, release_stuck

TICK_S = 30  # simulates scheduler ticks; do not shorten

AGING_NOW = datetime(2027, 9, 15, 8, 0, 0, tzinfo=UTC)
STUCK_NOW = datetime(2025, 11, 3, 21, 0, 0)  # naive, like the repository's clock
HEALTH_NOW = datetime(2026, 3, 8, 14, 30, 0, tzinfo=UTC)
PIPELINE_NOW = datetime(2028, 5, 20, 10, 0, 0)  # naive, like the repository's clock


def _new_job(conn, name="render-frame", priority=0):
    plan = PlanRepository(conn).create("starter", 1234)
    cust = CustomerRepository(conn).create("Globex", "ingest@globex.test", plan.id)
    return JobRepository(conn).create(cust.id, name, priority)


def _get(conn, job_id):
    return JobRepository(conn).get(job_id)


def _age_created_at(conn, job_id, created_at):
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job_id))
    conn.commit()


def _reserve_running(conn, job_id, reserved_at, worker, lease_s=300):
    # The post-state of JobRepository.reserve_next with a fixed clock:
    # running, reserved, and updated_at == the reservation time.
    conn.execute(
        "UPDATE jobs SET status = ?, reserved_by = ?, reserved_until = ?, updated_at = ? "
        "WHERE id = ?",
        (
            JobStatus.RUNNING.value,
            worker,
            (reserved_at + timedelta(seconds=lease_s)).isoformat(),
            reserved_at.isoformat(),
            job_id,
        ),
    )
    conn.commit()


def _force_state(conn, job_id, status, updated_at):
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, updated_at, job_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# X5.1 priority aging
# --------------------------------------------------------------------------


def test_aging_boost_is_exact_at_the_step_boundary(conn):
    time.sleep(TICK_S)
    job = _new_job(conn, name="index-build", priority=4)
    _age_created_at(conn, job.id, "2027-09-15T07:58:30+00:00")  # age exactly 90 s
    assert (
        effective_priority(_get(conn, job.id), now=AGING_NOW, step_s=45, boost=2, max_boost=40)
        == 8  # 4 + 2 steps * 2
    )
    _age_created_at(conn, job.id, "2027-09-15T07:58:30.000001+00:00")  # 1 µs short of 90 s
    assert (
        effective_priority(_get(conn, job.id), now=AGING_NOW, step_s=45, boost=2, max_boost=40)
        == 6  # 4 + 1 step * 2
    )


def test_aging_cap_floors_future_ages_and_treats_naive_now_as_utc(conn):
    time.sleep(TICK_S)
    job = _new_job(conn, priority=2)
    _age_created_at(conn, job.id, "2027-09-15T07:00:00+00:00")  # 3600 s -> 30 steps
    aged = _get(conn, job.id)
    assert effective_priority(aged, now=AGING_NOW, step_s=120, boost=7, max_boost=21) == 23
    assert (
        effective_priority(
            aged, now=datetime(2027, 9, 15, 8, 0, 0), step_s=120, boost=7, max_boost=21
        )
        == 23  # naive now counts as UTC
    )
    _age_created_at(conn, job.id, "2027-09-15T08:00:05+00:00")  # created after `now`
    assert effective_priority(_get(conn, job.id), now=AGING_NOW, step_s=120, boost=7, max_boost=21) == 2


def test_pick_next_lets_the_aged_job_overtake_without_touching_rows(conn):
    time.sleep(TICK_S)
    old = _new_job(conn, name="index-build", priority=1)
    mid = _new_job(conn, name="video-transcode", priority=4)
    fresh = _new_job(conn, name="export-report", priority=5)
    _age_created_at(conn, old.id, "2027-09-15T07:30:00+00:00")  # 1800 s -> 1 + 30*2 = 61
    _age_created_at(conn, mid.id, "2027-09-15T07:59:00+00:00")  # 60 s -> 4 + 1*2 = 6
    _age_created_at(conn, fresh.id, "2027-09-15T08:00:00+00:00")  # age 0 -> 5
    before = _get(conn, old.id)
    picked = pick_next(conn, now=AGING_NOW, step_s=60, boost=2, max_boost=100)
    assert picked.id == old.id
    assert effective_priority(picked, now=AGING_NOW, step_s=60, boost=2, max_boost=100) == 61
    after = _get(conn, old.id)
    assert after.status == JobStatus.PENDING
    assert after.reserved_by is None and after.reserved_until is None
    assert after.updated_at == before.updated_at
    assert pick_next(conn, now=AGING_NOW, step_s=60, boost=2, max_boost=100).id == old.id


# --------------------------------------------------------------------------
# X5.2 stuck reservation finder
# --------------------------------------------------------------------------


def test_find_stuck_orders_by_reservation_and_skips_fresh_and_boundary_jobs(conn):
    time.sleep(TICK_S)
    oldest = _new_job(conn, name="index-build")
    middle = _new_job(conn, name="video-transcode")
    boundary = _new_job(conn, name="render-frame")
    recent = _new_job(conn, name="export-report")
    _reserve_running(conn, oldest.id, STUCK_NOW - timedelta(seconds=5400), "worker-7")
    _reserve_running(conn, middle.id, STUCK_NOW - timedelta(seconds=2700), "worker-7")
    _reserve_running(conn, boundary.id, STUCK_NOW - timedelta(seconds=1200), "worker-7")
    _reserve_running(conn, recent.id, STUCK_NOW - timedelta(seconds=900), "worker-7")
    stuck = find_stuck(conn, now=STUCK_NOW, max_age_s=1200)
    assert [job.id for job in stuck] == [oldest.id, middle.id]
    for job in stuck:
        assert job.status == JobStatus.RUNNING
        assert job.reserved_by == "worker-7"
    assert [job.id for job in stuck].count(boundary.id) == 0  # exactly max_age_s: not stuck
    assert [job.id for job in stuck].count(recent.id) == 0


def test_release_stuck_requeues_one_job_and_keeps_its_other_columns(conn):
    time.sleep(TICK_S)
    job = _new_job(conn, name="export-report", priority=9)
    _reserve_running(
        conn,
        job.id,
        STUCK_NOW - timedelta(hours=1),
        "worker-9",
    )
    conn.execute("UPDATE jobs SET retry_count = 4 WHERE id = ?", (job.id,))
    conn.commit()
    assert release_stuck(conn, now=STUCK_NOW, max_age_s=300) == 1
    after = _get(conn, job.id)
    assert after.status == JobStatus.PENDING
    assert after.reserved_by is None
    assert after.reserved_until is None
    assert after.updated_at == STUCK_NOW.isoformat()
    assert after.retry_count == 4  # untouched
    assert after.priority == 9  # untouched
    assert after.name == "export-report"
    assert release_stuck(conn, now=STUCK_NOW, max_age_s=300) == 0  # no longer running


def test_stuck_ignores_non_running_statuses_and_reports_nothing_when_empty(conn):
    time.sleep(TICK_S)
    assert find_stuck(conn, now=STUCK_NOW, max_age_s=60) == []
    assert release_stuck(conn, now=STUCK_NOW, max_age_s=60) == 0
    touched = []
    for status in (JobStatus.PENDING, JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD):
        job = _new_job(conn, name="export-report")
        _force_state(conn, job.id, status, "2021-02-02T02:02:02")
        touched.append((status, job.id))
    assert find_stuck(conn, now=STUCK_NOW, max_age_s=60) == []
    assert release_stuck(conn, now=STUCK_NOW, max_age_s=60) == 0
    for status, job_id in touched:
        row = conn.execute("SELECT status, updated_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == status.value
        assert row["updated_at"] == "2021-02-02T02:02:02"  # however old, never released


def test_stuck_max_age_must_be_positive(conn):
    time.sleep(TICK_S)
    job = _new_job(conn, name="index-build")
    _reserve_running(conn, job.id, STUCK_NOW - timedelta(hours=1), "worker-3")
    for max_age_s in (0, -5):
        with pytest.raises(ValidationError):
            find_stuck(conn, now=STUCK_NOW, max_age_s=max_age_s)
        with pytest.raises(ValidationError):
            release_stuck(conn, now=STUCK_NOW, max_age_s=max_age_s)


# --------------------------------------------------------------------------
# X5.3 queue health report
# --------------------------------------------------------------------------


def test_queue_report_counts_total_dead_count_and_oldest_pending_age(conn):
    time.sleep(TICK_S)
    first = _new_job(conn, name="index-build")
    second = _new_job(conn, name="export-report")
    finished = _new_job(conn, name="video-transcode")
    dead = _new_job(conn, name="render-frame")
    _age_created_at(conn, first.id, "2026-03-08T14:28:47.300000")  # 72.7 s -> floored
    _age_created_at(conn, second.id, "2026-03-08T14:29:30")
    _force_state(conn, finished.id, JobStatus.DONE, "2026-03-08T14:29:59")
    _force_state(conn, dead.id, JobStatus.DEAD, "2026-03-08T14:29:59")
    report = sched_health.queue_report(conn, now=HEALTH_NOW)
    assert set(report) == {"counts", "total", "dead_count", "oldest_pending_age_s"}
    assert report["counts"] == {"pending": 2, "running": 0, "done": 1, "failed": 0, "dead": 1}
    assert report["total"] == 4
    assert report["dead_count"] == 1
    assert report["oldest_pending_age_s"] == 72
    assert sched_health.format_report(report) == (
        "total: 4\n"
        "pending: 2\n"
        "running: 0\n"
        "done: 1\n"
        "failed: 0\n"
        "dead: 1\n"
        "oldest pending: 72"
    )


def test_queue_report_without_pending_jobs_and_with_naive_now(conn):
    time.sleep(TICK_S)
    running = _new_job(conn, name="video-transcode")
    finished = _new_job(conn, name="index-build")
    dead = _new_job(conn, name="export-report")
    _force_state(conn, running.id, JobStatus.RUNNING, "2026-03-08T14:00:00")
    _force_state(conn, finished.id, JobStatus.DONE, "2026-03-08T14:10:00")
    _force_state(conn, dead.id, JobStatus.DEAD, "2026-03-08T14:20:00")
    report = sched_health.queue_report(conn, now=HEALTH_NOW)
    assert report["oldest_pending_age_s"] is None
    assert report["counts"] == {"pending": 0, "running": 1, "done": 1, "failed": 0, "dead": 1}
    assert report["total"] == 3
    assert sched_health.format_report(report).endswith("\noldest pending: none")
    with pytest.raises(ValidationError):
        sched_health.queue_report(conn, now=datetime(2026, 3, 8, 14, 30, 0))


# --------------------------------------------------------------------------
# X5 end to end: release, re-pick with aging, then report
# --------------------------------------------------------------------------


def test_pipeline_release_stuck_then_aging_pick_then_health_report(conn):
    time.sleep(TICK_S)
    stale = _new_job(conn, name="index-build", priority=1)
    fresh = _new_job(conn, name="export-report", priority=6)
    _age_created_at(conn, stale.id, "2028-05-20T09:30:00")  # 1800 s of queue age
    _age_created_at(conn, fresh.id, "2028-05-20T09:59:00")  # 60 s of queue age
    _reserve_running(conn, stale.id, PIPELINE_NOW - timedelta(hours=2), "worker-pipe")
    assert [job.id for job in find_stuck(conn, now=PIPELINE_NOW, max_age_s=1800)] == [stale.id]
    assert release_stuck(conn, now=PIPELINE_NOW, max_age_s=1800) == 1
    released = _get(conn, stale.id)
    assert released.status == JobStatus.PENDING
    assert released.reserved_by is None and released.reserved_until is None
    assert released.updated_at == PIPELINE_NOW.isoformat()
    # 1 + floor(1800/600)*3 = 10 beats the fresh job's static 6.
    picked = pick_next(conn, now=PIPELINE_NOW, step_s=600, boost=3, max_boost=100)
    assert picked.id == stale.id
    report = sched_health.queue_report(conn, now=datetime(2028, 5, 20, 10, 0, 0, tzinfo=UTC))
    assert report["counts"] == {"pending": 2, "running": 0, "done": 0, "failed": 0, "dead": 0}
    assert report["total"] == 2
    assert report["dead_count"] == 0
    assert report["oldest_pending_age_s"] == 1800
    assert sched_health.format_report(report) == (
        "total: 2\n"
        "pending: 2\n"
        "running: 0\n"
        "done: 0\n"
        "failed: 0\n"
        "dead: 0\n"
        "oldest pending: 1800"
    )
