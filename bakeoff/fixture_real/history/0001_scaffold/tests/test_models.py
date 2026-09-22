from __future__ import annotations

from jobsched.models import Customer, Job, JobStatus, Plan


def test_plan_fields():
    plan = Plan(id=1, name="pro", monthly_price_cents=2999, job_quota=100)
    assert plan.monthly_price_cents == 2999


def test_job_defaults():
    job = Job(id=1, customer_id=1, name="render", status=JobStatus.PENDING)
    assert job.retry_count == 0
    assert job.reserved_by is None


def test_customer_defaults():
    c = Customer(id=1, name="Acme", email="ops@acme.test", plan_id=1)
    assert c.created_at == ""
