from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jobsched.errors import ValidationError
from jobsched.integ_ops_report import ops_report, render_ops_report
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_job(conn, name="job-0"):
    plan = PlanRepository(conn).create("starter", 999)
    cust = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id)
    return JobRepository(conn).create(cust.id, name)


def _set_job_state(conn, job, status=None, created_at=None, updated_at=None):
    if status is not None:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job.id))
    if created_at is not None:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (created_at, job.id))
    if updated_at is not None:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (updated_at, job.id))
    conn.commit()


def _reserve_at(conn, job_id, reserved_at):
    # reserve_next's post-state with a fixed clock: updated_at is the reservation time.
    lease_end = (reserved_at + timedelta(seconds=300)).isoformat()
    conn.execute(
        "UPDATE jobs SET status = ?, reserved_by = ?, reserved_until = ?, updated_at = ? "
        "WHERE id = ?",
        (JobStatus.RUNNING.value, "worker-1", lease_end, reserved_at.isoformat(), job_id),
    )
    conn.commit()


def _worked_example(conn):
    job0 = _make_job(conn)
    job1 = _make_job(conn, "job-1")
    job2 = _make_job(conn, "job-2")
    worker = _make_job(conn, "worker-job")
    _set_job_state(conn, job0, created_at="2024-06-01T11:58:29.500000")  # 90.5 s -> 90
    _set_job_state(conn, job1, status=JobStatus.DONE)
    _set_job_state(conn, job2, status=JobStatus.DEAD)
    _reserve_at(conn, worker.id, NOW - timedelta(hours=2))


def test_worked_example_combines_health_stuck_and_first_page(conn):
    _worked_example(conn)
    report = ops_report(conn, now=NOW, page_size=2)
    assert set(report) == {"health", "stuck_ids", "jobs", "next_offset"}
    assert report["health"] == {
        "counts": {"pending": 1, "running": 1, "done": 1, "failed": 0, "dead": 1},
        "total": 4,
        "dead_count": 1,
        "oldest_pending_age_s": 90,  # L2-F4 floors 90.5 s, not 91
    }
    assert report["stuck_ids"] == [4]
    assert report["jobs"] == [
        {"job_id": 1, "name": "job-0", "status": "pending", "priority": 0},
        {"job_id": 2, "name": "job-1", "status": "done", "priority": 0},
    ]
    assert report["next_offset"] == 2
    full = ops_report(conn, now=NOW, page_size=5)  # page large enough for every row
    assert [item["job_id"] for item in full["jobs"]] == [1, 2, 3, 4] and full["next_offset"] is None


def test_worked_example_render_is_exact_text(conn):
    _worked_example(conn)
    text = render_ops_report(ops_report(conn, now=NOW, page_size=2))
    assert text == (
        "jobsched ops report\n"
        "total: 4\npending: 1\nrunning: 1\ndone: 1\nfailed: 0\ndead: 1\n"
        "oldest pending: 90\n"
        "stuck reservations: 1 ids: 4\n"
        "job 1 pending job-0\njob 2 done job-1\n"
        "next page offset: 2"
    )


def test_stuck_ids_order_oldest_reservation_first(conn):
    older = _make_job(conn, "older")
    newer = _make_job(conn, "newer")
    _reserve_at(conn, older.id, NOW - timedelta(hours=2))
    _reserve_at(conn, newer.id, NOW - timedelta(hours=1))
    assert ops_report(conn, now=NOW, page_size=5)["stuck_ids"] == [older.id, newer.id]


def test_stuck_ids_ignore_non_running_jobs_however_old(conn):
    done = _make_job(conn, "done-old")
    dead = _make_job(conn, "dead-old")
    _set_job_state(conn, done, status=JobStatus.DONE, updated_at="2020-01-01T00:00:00")
    _set_job_state(conn, dead, status=JobStatus.DEAD, updated_at="2020-01-01T00:00:00")
    assert ops_report(conn, now=NOW, page_size=5)["stuck_ids"] == []  # L2-F3: running only


def test_empty_queue_report_and_render(conn):
    report = ops_report(conn, now=NOW, page_size=5)
    assert report["health"] == {
        "counts": {"pending": 0, "running": 0, "done": 0, "failed": 0, "dead": 0},
        "total": 0,
        "dead_count": 0,
        "oldest_pending_age_s": None,
    }
    assert report["stuck_ids"] == [] and report["jobs"] == [] and report["next_offset"] is None
    assert render_ops_report(report) == (
        "jobsched ops report\n"
        "total: 0\npending: 0\nrunning: 0\ndone: 0\nfailed: 0\ndead: 0\n"
        "oldest pending: none\n"
        "stuck reservations: none\n"
        "first page: none\n"
        "next page offset: none"
    )


def test_render_keeps_zero_age_distinct_from_none(conn):
    job = _make_job(conn)
    _set_job_state(conn, job, created_at="2024-06-01T12:00:05")  # 5 s after NOW
    text = render_ops_report(ops_report(conn, now=NOW, page_size=5))
    assert "oldest pending: 0" in text  # L2-F4 clamps the age to 0, not "none"


def test_bad_page_size_raises_the_handler_message_verbatim(conn):
    cases = [(0, "limit must be >= 1"), (-3, "limit must be >= 1")]
    cases.append(("abc", "offset and limit must be integers"))
    for page_size, message in cases:
        with pytest.raises(ValidationError) as excinfo:
            ops_report(conn, now=NOW, page_size=page_size)
        assert str(excinfo.value) == message  # L3-F3's messages, verbatim


def test_naive_now_raises_validation_error(conn):
    with pytest.raises(ValidationError):
        ops_report(conn, now=datetime(2024, 6, 1, 12, 0, 0), page_size=5)  # no tzinfo


def test_ops_report_is_read_only(conn):
    _worked_example(conn)
    before = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    ops_report(conn, now=NOW, page_size=2)
    after = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
