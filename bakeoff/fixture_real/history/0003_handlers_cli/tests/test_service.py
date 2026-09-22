from __future__ import annotations

import pytest
from jobsched import service
from jobsched.errors import ValidationError
from jobsched.repository import PlanRepository


def test_create_customer_rejects_bad_email(conn):
    plan = PlanRepository(conn).create("starter", 999)
    with pytest.raises(ValidationError):
        service.create_customer(conn, "Acme", "not-an-email", plan.id)


def test_create_job_enforces_quota(conn):
    plan = PlanRepository(conn).create("starter", 999, job_quota=1)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)
    service.create_job(conn, cust.id, "first")
    with pytest.raises(ValidationError):
        service.create_job(conn, cust.id, "second")


def test_create_job_unlimited_quota_when_zero(conn):
    plan = PlanRepository(conn).create("starter", 999, job_quota=0)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)
    service.create_job(conn, cust.id, "first")
    service.create_job(conn, cust.id, "second")  # should not raise


def test_change_plan_records_plan_change(conn):
    plan_a = PlanRepository(conn).create("starter", 999)
    plan_b = PlanRepository(conn).create("pro", 2999)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan_a.id)
    updated = service.change_plan(conn, cust.id, plan_b.id)
    assert updated.plan_id == plan_b.id


def test_change_plan_same_plan_is_noop(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)
    updated = service.change_plan(conn, cust.id, plan.id)
    assert updated.plan_id == plan.id
