from __future__ import annotations

from io import StringIO

import pytest
from jobsched.importer import import_customers
from jobsched.repository import CustomerRepository, PlanRepository


def test_import_customers_happy_path(conn):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,1\n"
    result = import_customers(conn, StringIO(csv_text))
    assert result.ok
    assert result.imported == 1
    assert len(CustomerRepository(conn).list_all()) == 1


def test_import_customers_bad_plan_id_recorded_as_error(conn):
    PlanRepository(conn).create("starter", 999)
    csv_text = "name,email,plan_id\nAcme,ops@acme.test,not-a-number\n"
    result = import_customers(conn, StringIO(csv_text))
    assert not result.ok
    assert result.imported == 0
    assert result.errors[0].line_number == 2


def test_import_customers_empty_input(conn):
    result = import_customers(conn, StringIO(""))
    assert result.ok
    assert result.imported == 0


def test_import_customers_missing_column_raises(conn):
    with pytest.raises(ValueError):
        import_customers(conn, StringIO("name,email\nAcme,ops@acme.test\n"))
