"""Hidden acceptance tests for LANE 3, feature F2 (filtered job listing)."""

from __future__ import annotations

import pytest
from jobsched import db
from jobsched.errors import NotFoundError, ValidationError
from jobsched.iface_joblist import list_jobs, main
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository


def _seed(conn) -> int:
    plan = PlanRepository(conn).create("pro", 4900)
    return CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id).id


def test_ordering_is_priority_desc_then_id_asc(conn):
    customer_id = _seed(conn)
    JobRepository(conn).create(customer_id, "low", priority=0)  # id 1
    JobRepository(conn).create(customer_id, "high-a", priority=5)  # id 2
    JobRepository(conn).create(customer_id, "high-b", priority=5)  # id 3
    jobs = list_jobs(conn)
    assert [(j.id, j.priority) for j in jobs] == [(2, 5), (3, 5), (1, 0)]


def test_status_filter(conn):
    customer_id = _seed(conn)
    job = JobRepository(conn).create(customer_id, "one")
    JobRepository(conn).create(customer_id, "two")
    JobRepository(conn).mark_done(job.id)
    assert [j.name for j in list_jobs(conn, status="done")] == ["one"]
    assert [j.name for j in list_jobs(conn, status="pending")] == ["two"]


def test_customer_filter_combines_with_limit(conn):
    customer_id = _seed(conn)
    other = CustomerRepository(conn).create("Beta", "ops@beta.test", 1)
    JobRepository(conn).create(customer_id, "a-low", priority=1)
    JobRepository(conn).create(customer_id, "a-high", priority=9)
    JobRepository(conn).create(other.id, "b-high", priority=10)
    page = list_jobs(conn, customer_id=customer_id, limit=1)
    assert [j.name for j in page] == ["a-high"]


def test_invalid_status_raises_validation_error(conn):
    _seed(conn)
    with pytest.raises(ValidationError, match=r"invalid status: 'bogus'"):
        list_jobs(conn, status="bogus")


def test_bad_limit_and_unknown_customer(conn):
    customer_id = _seed(conn)
    with pytest.raises(ValidationError, match="limit must be >= 1"):
        list_jobs(conn, limit=0)
    with pytest.raises(NotFoundError):
        list_jobs(conn, customer_id=customer_id + 999)


def test_main_prints_one_line_per_job(tmp_path, capsys):
    path = str(tmp_path / "jobs.db")
    conn = db.connect(path)
    db.apply_migrations(conn)
    customer_id = _seed(conn)
    JobRepository(conn).create(customer_id, "low", priority=0)
    JobRepository(conn).create(customer_id, "high", priority=5)
    conn.close()
    rc = main(["--db", path])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "job 2 pending high\njob 1 pending low\n"


def test_main_bad_status_exits_one_with_stderr_error(tmp_path, capsys):
    path = str(tmp_path / "jobs.db")
    conn = db.connect(path)
    db.apply_migrations(conn)
    conn.close()
    rc = main(["--db", path, "--status", "bogus"])
    out = capsys.readouterr()
    assert rc == 1
    assert "error:" in out.err
    assert out.out == ""


def test_main_empty_database_prints_nothing(tmp_path, capsys):
    path = str(tmp_path / "jobs.db")
    conn = db.connect(path)
    db.apply_migrations(conn)
    conn.close()
    rc = main(["--db", path])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == ""
