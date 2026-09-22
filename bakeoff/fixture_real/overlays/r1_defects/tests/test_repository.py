from __future__ import annotations

import pytest
from jobsched.errors import NotFoundError
from jobsched.models import JobStatus
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository


def test_plan_create_and_get(conn):
    repo = PlanRepository(conn)
    plan = repo.create("starter", 999, job_quota=10)
    fetched = repo.get(plan.id)
    assert fetched.name == "starter"
    assert fetched.monthly_price_cents == 999


def test_plan_get_missing_raises(conn):
    repo = PlanRepository(conn)
    with pytest.raises(NotFoundError):
        repo.get(999)


def test_plan_exists(conn):
    repo = PlanRepository(conn)
    plan = repo.create("starter", 999)
    assert repo.exists(plan.id) is True
    assert repo.exists(999) is False


def test_customer_create_and_update_plan(conn):
    plans = PlanRepository(conn)
    plan_a = plans.create("starter", 999)
    plan_b = plans.create("pro", 2999)
    customers = CustomerRepository(conn)
    cust = customers.create("Acme", "ops@acme.test", plan_a.id)
    assert cust.plan_id == plan_a.id

    customers.update_plan(cust.id, plan_b.id)
    assert customers.get(cust.id).plan_id == plan_b.id


def test_job_create_defaults_to_pending(conn):
    plans = PlanRepository(conn)
    plan = plans.create("starter", 999)
    customers = CustomerRepository(conn)
    cust = customers.create("Acme", "ops@acme.test", plan.id)

    jobs = JobRepository(conn)
    job = jobs.create(cust.id, "render-frame")
    assert job.status == JobStatus.PENDING
    assert job.retry_count == 0


def test_count_active_for_customer(conn):
    plans = PlanRepository(conn)
    plan = plans.create("starter", 999)
    customers = CustomerRepository(conn)
    cust = customers.create("Acme", "ops@acme.test", plan.id)
    jobs = JobRepository(conn)
    jobs.create(cust.id, "a")
    jobs.create(cust.id, "b")
    assert jobs.count_active_for_customer(cust.id) == 2


def test_search_by_name_matches_substring(conn):
    plans = PlanRepository(conn)
    plan = plans.create("starter", 999)
    customers = CustomerRepository(conn)
    cust = customers.create("Acme", "ops@acme.test", plan.id)
    jobs = JobRepository(conn)
    jobs.create(cust.id, "render-frame-1")
    jobs.create(cust.id, "encode-audio")

    results = jobs.search_by_name("render")
    assert len(results) == 1
    assert results[0].name == "render-frame-1"


def test_search_by_name_no_match_returns_empty(conn):
    plans = PlanRepository(conn)
    plan = plans.create("starter", 999)
    customers = CustomerRepository(conn)
    cust = customers.create("Acme", "ops@acme.test", plan.id)
    JobRepository(conn).create(cust.id, "render-frame-1")

    assert JobRepository(conn).search_by_name("nonexistent") == []
