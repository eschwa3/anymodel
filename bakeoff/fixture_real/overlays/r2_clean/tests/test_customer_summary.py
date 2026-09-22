from __future__ import annotations

from jobsched import service
from jobsched.repository import PlanRepository


def test_get_customer_summary_counts_active_jobs(conn):
    plan = PlanRepository(conn).create("starter", 999, job_quota=5)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)
    service.create_job(conn, cust.id, "a")
    job_b = service.create_job(conn, cust.id, "b")

    from jobsched.scheduler import complete_job

    complete_job(conn, job_b.id)  # one done, one still pending

    summary = service.get_customer_summary(conn, cust.id)
    assert summary["total_jobs"] == 2
    assert summary["active_jobs"] == 1
    assert summary["job_quota"] == 5
    assert summary["plan_name"] == "starter"
