"""Soak test for X6: slow end-to-end checks. Do not edit."""

from __future__ import annotations

import time

import pytest
from jobsched import db
from jobsched.errors import ValidationError
from jobsched.iface_joblist import list_jobs, main
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository

TICK_S = 55  # simulates scheduler ticks; do not shorten


def _customer(conn) -> int:
    plan = PlanRepository(conn).create("starter", 1999)
    return CustomerRepository(conn).create("Globex", "team@globex.test", plan.id).id


def _tied(conn) -> int:
    """One low-priority job plus two jobs sharing the top priority."""
    plan = PlanRepository(conn).create("pro", 4900)
    customer_id = CustomerRepository(conn).create("Initech", "ops@initech.test", plan.id).id
    JobRepository(conn).create(customer_id, "baseline", priority=1)  # id 1
    JobRepository(conn).create(customer_id, "urgent-first", priority=4)  # id 2
    JobRepository(conn).create(customer_id, "urgent-second", priority=4)  # id 3
    return customer_id


def test_priorities_descend_across_zero_and_negative(conn):
    time.sleep(TICK_S)
    customer_id = _customer(conn)
    JobRepository(conn).create(customer_id, "zero", priority=0)  # id 1
    JobRepository(conn).create(customer_id, "sink", priority=-3)  # id 2
    JobRepository(conn).create(customer_id, "peak", priority=7)  # id 3
    jobs = list_jobs(conn)
    assert [(j.name, j.priority) for j in jobs] == [
        ("peak", 7),
        ("zero", 0),
        ("sink", -3),
    ]


def test_running_and_dead_statuses_are_filterable(conn):
    time.sleep(TICK_S)
    customer_id = _customer(conn)
    JobRepository(conn).create(customer_id, "lease-me")  # id 1
    doomed = JobRepository(conn).create(customer_id, "doom-me")  # id 2
    backlog = JobRepository(conn).create(customer_id, "stay-pending")  # id 3
    leased = JobRepository(conn).reserve_next("worker-7", lease_seconds=600)
    JobRepository(conn).mark_dead(doomed.id, retry_count=3)
    assert [j.name for j in list_jobs(conn, status="running")] == ["lease-me"]
    assert [j.name for j in list_jobs(conn, status="dead")] == ["doom-me"]
    assert [j.name for j in list_jobs(conn, status="pending")] == ["stay-pending"]
    assert leased is not None
    assert (leased.name, leased.status.value, leased.reserved_by) == (
        "lease-me",
        "running",
        "worker-7",
    )
    assert backlog.status.value == "pending"


def test_status_and_customer_filters_combine(conn):
    time.sleep(TICK_S)
    customer_id = _customer(conn)
    other = CustomerRepository(conn).create("Umbrella", "ops@umbrella.test", 1)
    a_done = JobRepository(conn).create(customer_id, "a-done", priority=2)  # id 1
    JobRepository(conn).create(customer_id, "a-pending", priority=9)  # id 2
    b_done = JobRepository(conn).create(other.id, "b-done", priority=8)  # id 3
    JobRepository(conn).mark_done(a_done.id)
    JobRepository(conn).mark_done(b_done.id)
    both = list_jobs(conn, status="done", customer_id=customer_id)
    assert [j.name for j in both] == ["a-done"]
    other_done = list_jobs(conn, status="done", customer_id=other.id)
    assert [j.name for j in other_done] == ["b-done"]
    everything = list_jobs(conn, status="done")
    assert [j.name for j in everything] == ["b-done", "a-done"]


def test_limit_applies_after_ordering(conn):
    time.sleep(TICK_S)
    customer_id = _customer(conn)
    JobRepository(conn).create(customer_id, "p1", priority=1)  # id 1
    JobRepository(conn).create(customer_id, "p5", priority=5)  # id 2
    JobRepository(conn).create(customer_id, "p9", priority=9)  # id 3
    JobRepository(conn).create(customer_id, "p3", priority=3)  # id 4
    page = list_jobs(conn, limit=2)
    assert [j.name for j in page] == ["p9", "p5"]
    full = list_jobs(conn, limit=10)
    assert [j.name for j in full] == ["p9", "p5", "p3", "p1"]


def test_default_limit_is_fifty(conn):
    time.sleep(TICK_S)
    customer_id = _customer(conn)
    for index in range(6):
        JobRepository(conn).create(customer_id, f"bulk-{index}", priority=index)
    jobs = list_jobs(conn)
    assert [j.priority for j in jobs] == [5, 4, 3, 2, 1, 0]
    assert all(j.customer_id == customer_id for j in jobs)


def test_nonpositive_limit_is_rejected(conn):
    time.sleep(TICK_S)
    _customer(conn)
    with pytest.raises(ValidationError, match="limit must be >= 1"):
        list_jobs(conn, limit=0)
    with pytest.raises(ValidationError, match="limit must be >= 1"):
        list_jobs(conn, limit=-4)


def test_main_filters_by_status_flag(tmp_path, capsys):
    time.sleep(TICK_S)
    path = str(tmp_path / "ops.db")
    conn = db.connect(path)
    db.apply_migrations(conn)
    customer_id = _customer(conn)
    JobRepository(conn).create(customer_id, "queued", priority=0)  # id 1
    dead = JobRepository(conn).create(customer_id, "exploded", priority=6)  # id 2
    JobRepository(conn).mark_dead(dead.id, retry_count=5)
    conn.close()
    rc = main(["--db", path, "--status", "dead", "--limit", "5"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "job 2 dead exploded\n"


def test_tied_priorities_keep_insertion_order(conn):
    time.sleep(TICK_S)
    _tied(conn)
    jobs = list_jobs(conn, limit=2)
    assert [j.name for j in jobs] == ["urgent-first", "urgent-second"]


def test_tied_priorities_prefer_most_recent(conn):
    time.sleep(TICK_S)
    _tied(conn)
    jobs = list_jobs(conn, limit=2)
    assert [j.name for j in jobs] == ["urgent-second", "urgent-first"]
