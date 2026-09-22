from __future__ import annotations

import pytest
from jobsched.billing_latefees import late_fee_cents
from jobsched.errors import ValidationError


def test_within_grace_period_fee_is_zero():
    # Default grace is 5 days.
    assert late_fee_cents(20000, 0) == 0
    assert late_fee_cents(20000, 3) == 0
    assert late_fee_cents(20000, 5) == 0


def test_first_day_past_grace_accrues_exactly_one_day():
    # 1% of 1000 for one chargeable day.
    assert late_fee_cents(1000, 6, grace_days=5, daily_rate_bp=100) == 10


def test_fee_is_rounded_half_up_once_not_per_day():
    # Total 333 at 50 bp: per-day would give round(1.665)=2 three times = 6;
    # rounding once: 333 * 50 * 3 / 10000 = 4.995 -> 5.
    assert late_fee_cents(333, 3, grace_days=0) == 5


def test_fee_is_capped_at_invoice_total():
    # 50% per day for 10 days on a 100-cent total would be 500 uncapped.
    assert late_fee_cents(100, 10, grace_days=0, daily_rate_bp=5000) == 100


def test_zero_total_means_zero_fee():
    assert late_fee_cents(0, 400, grace_days=0) == 0


@pytest.mark.parametrize(
    ("total", "days", "grace", "rate"),
    [(-1, 0, 0, 50), (1000, -1, 5, 50), (1000, 3, -1, 50), (1000, 3, 0, -1)],
)
def test_invalid_arguments_raise_validation_error(total, days, grace, rate):
    with pytest.raises(ValidationError):
        late_fee_cents(total, days, grace_days=grace, daily_rate_bp=rate)


def test_default_arguments_match_documented_values():
    # 6 days overdue, 5-day grace, 50 bp daily rate on 20000 cents:
    # one chargeable day -> 20000 * 50 / 10000 = 100 cents.
    assert late_fee_cents(20000, 6) == 100
