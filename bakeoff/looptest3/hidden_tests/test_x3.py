"""Hidden acceptance tests for X3 (dry-run import report)."""

from __future__ import annotations

import pytest
from jobsched.errors import ValidationError
from jobsched.iface_dryrun import dry_run_import
from jobsched.repository import CustomerRepository, PlanRepository


def _write_csv(tmp_path, text: str):
    path = tmp_path / "customers.csv"
    path.write_text(text)
    return str(path)


def test_empty_file_is_ok_and_writes_nothing(conn, tmp_path):
    result = dry_run_import(conn, _write_csv(tmp_path, ""))
    assert result == {"ok": True, "row_count": 0, "valid_rows": [], "errors": []}
    assert CustomerRepository(conn).list_all() == []


def test_whitespace_only_file_is_ok(conn, tmp_path):
    result = dry_run_import(conn, _write_csv(tmp_path, "  \n\t\n"))
    assert result["ok"] is True
    assert result["row_count"] == 0


def test_valid_rows_reported_with_line_numbers_and_typed_plan_id(conn, tmp_path):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,1\n Blue ,hr@blue.test,1\n"
    result = dry_run_import(conn, _write_csv(tmp_path, csv_text))
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["row_count"] == 2
    assert result["valid_rows"] == [
        {"line": 2, "name": "Acme", "email": "ops@acme.test", "plan_id": 1},
        {"line": 3, "name": "Blue", "email": "hr@blue.test", "plan_id": 1},
    ]


def test_invalid_plan_id_reports_line_and_exact_message(conn, tmp_path):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,not-a-number\n"
    result = dry_run_import(conn, _write_csv(tmp_path, csv_text))
    assert result["ok"] is False
    assert result["errors"] == [{"line": 2, "message": "invalid plan_id: 'not-a-number'"}]
    assert result["valid_rows"] == []


def test_blank_name_and_bad_email_use_service_messages(conn, tmp_path):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\n  ,ops@acme.test,1\nAcme,no-at-sign,1\nOk,ok@acme.test,1\n"
    result = dry_run_import(conn, _write_csv(tmp_path, csv_text))
    assert result["errors"] == [
        {"line": 2, "message": "customer name is required"},
        {"line": 3, "message": "invalid email: 'no-at-sign'"},
    ]
    assert [row["line"] for row in result["valid_rows"]] == [4]


def test_unknown_plan_reported_as_not_found(conn, tmp_path):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,99\n"
    result = dry_run_import(conn, _write_csv(tmp_path, csv_text))
    assert result["errors"] == [{"line": 2, "message": "plan 99 not found"}]


def test_missing_required_column_raises_validation_error(conn, tmp_path):
    with pytest.raises(ValidationError):
        dry_run_import(conn, _write_csv(tmp_path, "name,email\nAcme,ops@acme.test\n"))


def test_dry_run_never_writes_rows(conn, tmp_path):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,1\nBad,ops@bad.test,zzz\nOk,ok@acme.test,1\n"
    result = dry_run_import(conn, _write_csv(tmp_path, csv_text))
    assert result["ok"] is False
    assert result["row_count"] == 3
    assert CustomerRepository(conn).list_all() == []
