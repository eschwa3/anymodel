"""Hidden acceptance tests for LANE 4, feature F2 (next jobs to run)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from jobsched.errors import ValidationError
from jobsched.integ_next_jobs import next_jobs
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository

NOW = datetime(2024, 6, 1, 12, 0, 0)  # injected clock; no wall-clock reads

PARAMS = {"base_s": 60, "cap_s": 300, "step_s": 3600, "boost": 1, "max_boost": 5}


def _customer_id(conn):
    plan = PlanRepository(conn).create("starter", 999)
    return CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id).id


def _add(conn, customer_id, *, status=JobStatus.PENDING, priority=0, retry_count=0,
         created_at, updated_at, reserved_by=None):
    # The stored state a repository write would leave, with a fixed clock.
    job = JobRepository(conn).create(customer_id, "render-frame", priority=priority)
    conn.execute(
        "UPDATE jobs SET status = ?, priority = ?, retry_count = ?, created_at = ?, "
        "updated_at = ?, reserved_by = ?, reserved_until = NULL WHERE id = ?",
        (status.value, priority, retry_count, created_at, updated_at, reserved_by, job.id),
    )
    conn.commit()
    return job.id


def _run(conn, limit=10, **overrides):
    return next_jobs(conn, now=NOW, limit=limit, **{**PARAMS, **overrides})


def _rows(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()]


def _worked_example(conn):
    cid = _customer_id(conn)
    j1 = _add(conn, cid, priority=1, retry_count=2, created_at="2024-05-30T00:00:00",
              updated_at="2024-06-01T11:57:00")  # due 11:59:00; aged 1+min(5,60)=6
    j2 = _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
              updated_at="2024-06-01T11:59:00")  # due exactly now; aged 0+min(5,12)=5
    _add(conn, cid, priority=3, retry_count=3, created_at="2024-05-25T00:00:00",
         updated_at="2024-06-01T11:59:30")  # due 12:03:30, not yet
    _add(conn, cid, priority=5, created_at="2024-06-01T11:00:00",
         updated_at="2024-06-01T11:30:00")  # fresh pending
    j5 = _add(conn, cid, status=JobStatus.RUNNING, created_at="2024-06-01T09:00:00",
              updated_at="2024-06-01T10:00:00", reserved_by="worker-1")  # stuck
    return j1, j2, j5


def test_worked_example_ranks_by_aged_priority(conn):
    j1, j2, j5 = _worked_example(conn)
    assert _run(conn, limit=10) == [
        {"id": j1, "priority": 1, "aged_priority": 6},
        {"id": j2, "priority": 0, "aged_priority": 5},
    ]
    assert j5 not in [entry["id"] for entry in _run(conn, limit=10)]


def test_limit_truncates_from_the_front(conn):
    j1, j2, _ = _worked_example(conn)
    assert _run(conn, limit=1) == [{"id": j1, "priority": 1, "aged_priority": 6}]
    assert [entry["id"] for entry in _run(conn, limit=2)] == [j1, j2]
    assert [entry["id"] for entry in _run(conn, limit=99)] == [j1, j2]


def test_stuck_job_is_dropped_and_never_released(conn):
    j1, j2, j5 = _worked_example(conn)
    assert [entry["id"] for entry in _run(conn)] == [j1, j2]
    row = conn.execute(
        "SELECT status, reserved_by, updated_at FROM jobs WHERE id = ?", (j5,)
    ).fetchone()
    assert row["status"] == "running"
    assert row["reserved_by"] == "worker-1"
    assert row["updated_at"] == "2024-06-01T10:00:00"


def test_call_is_read_only_and_repeatable(conn):
    j1, j2, _ = _worked_example(conn)
    before = _rows(conn)
    first = _run(conn)
    assert _run(conn) == first == [
        {"id": j1, "priority": 1, "aged_priority": 6},
        {"id": j2, "priority": 0, "aged_priority": 5},
    ]
    assert _rows(conn) == before


def test_ties_break_by_job_id_ascending(conn):
    cid = _customer_id(conn)
    first = _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
                 updated_at="2024-06-01T11:59:00")
    second = _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
                  updated_at="2024-06-01T11:59:00")
    assert [entry["id"] for entry in _run(conn)] == [first, second]


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_below_one_raises_validation_error(conn, limit):
    cid = _customer_id(conn)
    _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
         updated_at="2024-06-01T11:59:00")
    with pytest.raises(ValidationError):
        _run(conn, limit=limit)


@pytest.mark.parametrize(
    "bad",
    [{"base_s": 0}, {"cap_s": 59}, {"step_s": 0}, {"boost": -1},
     {"max_boost": -1}, {"stuck_max_age_s": 0}],
)
def test_dependency_parameter_errors_surface(conn, bad):
    cid = _customer_id(conn)
    _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
         updated_at="2024-06-01T11:59:00")
    with pytest.raises(ValidationError):
        _run(conn, **bad)


def test_backoff_boundary_is_inherited(conn):
    cid = _customer_id(conn)
    due = _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
               updated_at="2024-06-01T11:59:00")  # base_s=60 elapsed exactly
    _add(conn, cid, retry_count=1, created_at="2024-06-01T00:00:00",
         updated_at="2024-06-01T11:59:01")  # due 12:00:01, one second past now
    assert [entry["id"] for entry in _run(conn)] == [due]


def test_backoff_cap_and_doubling_are_inherited(conn):
    cid = _customer_id(conn)
    capped = _add(conn, cid, retry_count=3, created_at="2024-06-01T00:00:00",
                  updated_at="2024-06-01T11:57:30")  # cap_s=100 caps 240 to 100
    doubled = _add(conn, cid, retry_count=2, created_at="2024-06-01T00:00:00",
                   updated_at="2024-06-01T11:58:30")  # capped 100 s: due 12:00:10, not yet
    assert [entry["id"] for entry in _run(conn, base_s=60, cap_s=100)] == [capped]


def test_max_boost_cap_flips_ranking(conn):
    cid = _customer_id(conn)
    aged = _add(conn, cid, priority=0, retry_count=1, created_at="2024-05-22T00:00:00",
                updated_at="2024-06-01T11:59:00")
    fresh = _add(conn, cid, priority=3, retry_count=1, created_at="2024-06-01T11:00:00",
                 updated_at="2024-06-01T11:58:00")
    assert _run(conn, max_boost=2) == [
        {"id": fresh, "priority": 3, "aged_priority": 4},  # 3 + min(2, 1 step)
        {"id": aged, "priority": 0, "aged_priority": 2},  # 0 + min(2, 252 steps)
    ]


def test_aging_floor_and_negative_age_clamp_are_inherited(conn):
    cid = _customer_id(conn)
    future = _add(conn, cid, priority=7, retry_count=1, created_at="2024-06-01T13:00:00",
                  updated_at="2024-06-01T11:59:00")
    exact = _add(conn, cid, retry_count=1, created_at="2024-06-01T11:00:00",
                 updated_at="2024-06-01T11:59:00")
    one_us_short = (datetime.fromisoformat("2024-06-01T11:00:00") + timedelta(microseconds=1))
    short = _add(conn, cid, retry_count=1, created_at=one_us_short.isoformat(),
                 updated_at="2024-06-01T11:58:00")
    assert _run(conn, max_boost=10) == [
        {"id": future, "priority": 7, "aged_priority": 7},  # age < 0 adds nothing
        {"id": exact, "priority": 0, "aged_priority": 1},  # exactly one step
        {"id": short, "priority": 0, "aged_priority": 0},  # 1 us short of a step
    ]
