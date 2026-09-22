"""Reference/gold test suite for `jobsched/notifications.py`, used offline to
verify that bakeoff's R6 (write-tests) mutants are actually killable and that
the original module passes. Not shown to workers; not the grading bar for a
worker's own test file (workers are graded by mutation score against
`bakeoff/hidden_real/mutants/r6/*.py`, using whatever test file the worker
wrote).
"""

from __future__ import annotations

import pytest
from jobsched.errors import ValidationError
from jobsched.models import Customer, Job, JobStatus
from jobsched.notifications import (
    build_reminder_batch,
    format_reminder_digest,
    validate_customer_for_notification,
)


def _customer(id_=1, email="a@b.test") -> Customer:
    return Customer(id=id_, name=f"Customer{id_}", email=email, plan_id=1)


def _job(id_, status) -> Job:
    return Job(id=id_, customer_id=1, name=f"job{id_}", status=status)


# -- validate_customer_for_notification --------------------------------


def test_validate_customer_none_raises():
    with pytest.raises(ValidationError):
        validate_customer_for_notification(None)


def test_validate_customer_missing_email_raises():
    with pytest.raises(ValidationError):
        validate_customer_for_notification(_customer(email=""))


def test_validate_customer_email_without_at_raises():
    with pytest.raises(ValidationError):
        validate_customer_for_notification(_customer(email="not-an-email"))


def test_validate_customer_valid_returns_true():
    assert validate_customer_for_notification(_customer()) is True


# -- build_reminder_batch: email gating ---------------------------------


def test_skips_customer_without_email():
    cust = _customer(email="")
    reminders = build_reminder_batch([cust], {}, {cust.id}, {})
    assert reminders == []


def test_skips_customer_with_invalid_email():
    cust = _customer(email="no-at-sign")
    reminders = build_reminder_batch([cust], {}, {cust.id}, {})
    assert reminders == []


# -- build_reminder_batch: overdue invoices ------------------------------


def test_overdue_customer_gets_reminder():
    cust = _customer()
    reminders = build_reminder_batch([cust], {}, {cust.id}, {})
    assert len(reminders) == 1
    assert reminders[0].kind == "overdue_invoice"


def test_non_overdue_customer_gets_no_overdue_reminder():
    cust = _customer()
    reminders = build_reminder_batch([cust], {}, set(), {})
    assert all(r.kind != "overdue_invoice" for r in reminders)


# -- build_reminder_batch: quota ------------------------------------------


def test_quota_reminder_when_active_jobs_reach_quota():
    cust = _customer()
    jobs = [_job(1, JobStatus.PENDING), _job(2, JobStatus.RUNNING)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, set(), {cust.id: 2})
    kinds = [r.kind for r in reminders]
    assert "quota_exceeded" in kinds


def test_no_quota_reminder_when_under_quota():
    cust = _customer()
    jobs = [_job(1, JobStatus.PENDING)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, set(), {cust.id: 5})
    assert all(r.kind != "quota_exceeded" for r in reminders)


def test_zero_quota_means_unlimited_never_triggers():
    cust = _customer()
    jobs = [_job(i, JobStatus.PENDING) for i in range(10)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, set(), {cust.id: 0})
    assert all(r.kind != "quota_exceeded" for r in reminders)


def test_done_and_dead_jobs_are_not_counted_active():
    cust = _customer()
    jobs = [_job(1, JobStatus.DONE), _job(2, JobStatus.DEAD)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, set(), {cust.id: 1})
    assert all(r.kind != "quota_exceeded" for r in reminders)


# -- build_reminder_batch: dedup -------------------------------------------


def test_each_customer_kind_pair_reminded_at_most_once():
    cust = _customer()
    jobs = [_job(1, JobStatus.PENDING)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, {cust.id}, {cust.id: 1})
    overdue = [r for r in reminders if r.kind == "overdue_invoice"]
    quota = [r for r in reminders if r.kind == "quota_exceeded"]
    assert len(overdue) == 1
    assert len(quota) == 1


def test_multiple_customers_each_get_their_own_reminders():
    c1, c2 = _customer(1), _customer(2)
    reminders = build_reminder_batch([c1, c2], {}, {c1.id, c2.id}, {})
    assert {r.customer_id for r in reminders} == {c1.id, c2.id}


def test_duplicate_customer_entries_still_deduped():
    cust = _customer()
    reminders = build_reminder_batch([cust, cust], {}, {cust.id}, {})
    overdue = [r for r in reminders if r.kind == "overdue_invoice"]
    assert len(overdue) == 1


def test_overdue_and_quota_both_reported_for_same_customer():
    cust = _customer()
    jobs = [_job(1, JobStatus.PENDING)]
    reminders = build_reminder_batch([cust], {cust.id: jobs}, {cust.id}, {cust.id: 1})
    kinds = sorted(r.kind for r in reminders)
    assert kinds == ["overdue_invoice", "quota_exceeded"]


# -- format_reminder_digest -------------------------------------------------


def test_format_empty_digest():
    assert format_reminder_digest([]) == "No reminders."


def test_format_digest_lists_kind_and_message():
    cust = _customer()
    reminders = build_reminder_batch([cust], {}, {cust.id}, {})
    text = format_reminder_digest(reminders)
    assert "[overdue_invoice]" in text
    assert cust.name in text
