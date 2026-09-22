"""Hidden acceptance tests for LANE 4, feature F4 (configured import)."""

from __future__ import annotations

import pytest
from jobsched.errors import ValidationError
from jobsched.integ_bootstrap import bootstrap_import
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository


def _write_csv(tmp_path, text: str):
    path = tmp_path / "customers.csv"
    path.write_text(text)
    return str(path)


def _seed(conn) -> int:
    plan = PlanRepository(conn).create("pro", 4900)
    return CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id).id


def _job_dict(job_id, customer_id, name, status="pending", priority=0, retry_count=0):
    return {"id": job_id, "customer_id": customer_id, "name": name, "status": status,
            "priority": priority, "retry_count": retry_count}


def test_worked_example_full_success(conn, tmp_path):
    customer_id = _seed(conn)
    JobRepository(conn).create(customer_id, "sync", priority=0)  # id 1
    JobRepository(conn).create(customer_id, "render", priority=5)  # id 2
    result = bootstrap_import(
        conn,
        _write_csv(tmp_path, "name,email,plan_id\nAcme,ops@acme.test,1\n"),
        environ={"JOBSCHED_CURRENCY": "EUR", "JOBSCHED_MAX_JOB_RETRIES": "7"},
        overrides={"currency": "GBP"},
    )
    assert set(result) == {"config", "dry_run", "imported", "jobs"}
    assert result["config"] == {
        "currency": "GBP", "tax_rate_bp": 750, "max_job_retries": 7,  # override beats env var
        "reservation_lease_seconds": 300, "db_path": "jobsched.db",
    }
    assert result["dry_run"] == {
        "ok": True, "row_count": 1,
        "valid_rows": [{"line": 2, "name": "Acme", "email": "ops@acme.test", "plan_id": 1}],
        "errors": [],
    }
    assert result["imported"] == 1
    assert result["jobs"] == [_job_dict(2, 1, "render", priority=5), _job_dict(1, 1, "sync")]
    assert CustomerRepository(conn).get(2).email == "ops@acme.test"


def test_clean_csv_imports_every_row_with_env_config(conn, tmp_path):
    PlanRepository(conn).create("pro", 4900)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,1\n Blue ,hr@blue.test,1\nCid,c@c.test,1\n"
    result = bootstrap_import(
        conn,
        _write_csv(tmp_path, csv_text),
        environ={"JOBSCHED_FOO": "ignored", "JOBSCHED_TAX_RATE_BP": "100"},
    )
    assert result["config"] == {  # unknown JOBSCHED_* variables are ignored
        "currency": "USD", "tax_rate_bp": 100, "max_job_retries": 3,
        "reservation_lease_seconds": 300, "db_path": "jobsched.db",
    }
    assert result["imported"] == 3 == result["dry_run"]["row_count"]
    assert [(c.name, c.email, c.plan_id) for c in CustomerRepository(conn).list_all()] == [
        ("Acme", "ops@acme.test", 1),
        ("Blue", "hr@blue.test", 1),
        ("Cid", "c@c.test", 1),
    ]


def test_bad_env_value_raises_naming_the_var_before_any_write(conn, tmp_path):
    _seed(conn)
    source = _write_csv(tmp_path, "name,email,plan_id\nAcme,ops@acme.test,1\n")
    with pytest.raises(ValidationError, match="JOBSCHED_MAX_JOB_RETRIES"):
        bootstrap_import(conn, source, environ={"JOBSCHED_MAX_JOB_RETRIES": "abc"})
    with pytest.raises(ValidationError, match="JOBSCHED_RESERVATION_LEASE_SECONDS"):
        bootstrap_import(conn, source, environ={"JOBSCHED_RESERVATION_LEASE_SECONDS": "0"})
    assert [c.name for c in CustomerRepository(conn).list_all()] == ["Acme"]  # nothing imported


def test_failed_dry_run_imports_nothing_and_keeps_the_listing(conn, tmp_path):
    customer_id = _seed(conn)
    JobRepository(conn).create(customer_id, "solo", priority=2)  # id 1
    csv_text = "name,email,plan_id\nOk,ok@acme.test,1\nBad,ops@bad.test,later\nGhost,g@x.test,99\n"
    result = bootstrap_import(conn, _write_csv(tmp_path, csv_text), environ={})
    assert result["dry_run"]["ok"] is False
    assert result["dry_run"]["row_count"] == 3
    assert result["dry_run"]["valid_rows"] == [
        {"line": 2, "name": "Ok", "email": "ok@acme.test", "plan_id": 1}
    ]
    assert result["dry_run"]["errors"] == [
        {"line": 3, "message": "invalid plan_id: 'later'"},
        {"line": 4, "message": "plan 99 not found"},
    ]
    assert result["imported"] == 0
    assert [c.name for c in CustomerRepository(conn).list_all()] == ["Acme"]  # db unchanged
    assert result["jobs"] == [_job_dict(1, 1, "solo", priority=2)]


def test_dry_run_row_messages_and_line_order_inherited(conn, tmp_path):
    _seed(conn)
    csv_text = "name,email,plan_id\n  ,a@b.test,1\nAcme,no-at-sign,1\nAcme,x@y.test,zz\nOk,ok@acme.test,1\n"
    result = bootstrap_import(conn, _write_csv(tmp_path, csv_text), environ={})
    assert result["dry_run"]["errors"] == [
        {"line": 2, "message": "customer name is required"},
        {"line": 3, "message": "invalid email: 'no-at-sign'"},
        {"line": 4, "message": "invalid plan_id: 'zz'"},
    ]
    assert result["dry_run"]["valid_rows"] == [
        {"line": 5, "name": "Ok", "email": "ok@acme.test", "plan_id": 1}
    ]
    assert result["imported"] == 0  # a valid row exists, but the dry run failed


def test_missing_header_column_raises_validation_error(conn, tmp_path):
    _seed(conn)
    source = _write_csv(tmp_path, "name,email\nAcme,ops@acme.test\n")
    with pytest.raises(ValidationError, match="missing required columns"):
        bootstrap_import(conn, source, environ={})
    assert [c.name for c in CustomerRepository(conn).list_all()] == ["Acme"]


def test_empty_csv_is_ok_with_zero_imports(conn, tmp_path):
    result = bootstrap_import(conn, _write_csv(tmp_path, ""), environ={})
    assert result["dry_run"] == {"ok": True, "row_count": 0, "valid_rows": [], "errors": []}
    assert result["imported"] == 0
    assert result["jobs"] == []


def test_job_listing_defaults_and_order_inherited(conn, tmp_path):
    customer_id = _seed(conn)
    other = CustomerRepository(conn).create("Beta", "ops@beta.test", 1)
    JobRepository(conn).create(customer_id, "a-low", priority=0)  # id 1
    JobRepository(conn).create(customer_id, "a-high", priority=5)  # id 2
    JobRepository(conn).create(other.id, "b-high", priority=5)  # id 3
    JobRepository(conn).create(other.id, "b-mid", priority=1)  # id 4
    JobRepository(conn).mark_done(1)
    result = bootstrap_import(conn, _write_csv(tmp_path, ""), environ={})
    assert result["config"]["tax_rate_bp"] == 750  # defaults: no JOBSCHED_* variable set
    assert result["imported"] == 0
    assert [j["id"] for j in result["jobs"]] == [2, 3, 4, 1]  # priority DESC, id ASC
    assert [j["customer_id"] for j in result["jobs"][:2]] == [customer_id, other.id]  # no filter
    assert [(j["id"], j["name"], j["status"]) for j in result["jobs"]] == [
        (2, "a-high", "pending"), (3, "b-high", "pending"),
        (4, "b-mid", "pending"), (1, "a-low", "done"),
    ]
    assert set(result["jobs"][0]) == {
        "id", "customer_id", "name", "status", "priority", "retry_count"}
