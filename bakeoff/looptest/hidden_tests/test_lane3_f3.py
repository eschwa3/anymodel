"""Hidden acceptance tests for LANE 3, feature F3 (jobs pagination handler)."""

from __future__ import annotations

from jobsched.iface_paging import handle_list_jobs_page
from jobsched.repository import CustomerRepository, JobRepository, PlanRepository


def _seed_jobs(conn, count: int) -> None:
    plan = PlanRepository(conn).create("pro", 4900)
    customer_id = CustomerRepository(conn).create("Acme", "ops@acme.test", plan.id).id
    for i in range(count):
        JobRepository(conn).create(customer_id, f"job-{i}", priority=i)


def test_first_page_returns_items_and_next_offset(conn):
    _seed_jobs(conn, 3)
    page = handle_list_jobs_page(conn, {"offset": 0, "limit": 2})
    assert page["ok"] is True
    assert [item["job_id"] for item in page["items"]] == [1, 2]
    assert page["items"][0] == {"job_id": 1, "name": "job-0", "status": "pending", "priority": 0}
    assert page["next_offset"] == 2


def test_second_page_is_last(conn):
    _seed_jobs(conn, 3)
    page = handle_list_jobs_page(conn, {"offset": 2, "limit": 2})
    assert page["ok"] is True
    assert [item["job_id"] for item in page["items"]] == [3]
    assert page["next_offset"] is None


def test_exact_fit_page_has_no_next_offset(conn):
    _seed_jobs(conn, 4)
    page = handle_list_jobs_page(conn, {"offset": 2, "limit": 2})
    assert [item["job_id"] for item in page["items"]] == [3, 4]
    assert page["next_offset"] is None


def test_empty_table_gives_empty_page(conn):
    page = handle_list_jobs_page(conn, {})
    assert page == {"ok": True, "items": [], "next_offset": None}


def test_offset_past_end_is_empty_and_writes_nothing(conn):
    _seed_jobs(conn, 2)
    page = handle_list_jobs_page(conn, {"offset": 99, "limit": 10})
    assert page == {"ok": True, "items": [], "next_offset": None}
    assert all(job.status.value == "pending" for job in JobRepository(conn).list_for_customer(1))


def test_negative_offset_fails_without_items(conn):
    page = handle_list_jobs_page(conn, {"offset": -1})
    assert page == {"ok": False, "error": "offset must be >= 0"}
    assert "items" not in page and "next_offset" not in page


def test_limit_zero_fails(conn):
    page = handle_list_jobs_page(conn, {"limit": 0})
    assert page == {"ok": False, "error": "limit must be >= 1"}


def test_non_numeric_offset_or_limit_fails(conn):
    bad_offset = handle_list_jobs_page(conn, {"offset": "abc"})
    assert bad_offset == {"ok": False, "error": "offset and limit must be integers"}
    bad_limit = handle_list_jobs_page(conn, {"limit": None})
    assert bad_limit == {"ok": False, "error": "offset and limit must be integers"}
