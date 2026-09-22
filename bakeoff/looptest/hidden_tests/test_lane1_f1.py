from __future__ import annotations

import pytest
from jobsched.billing_discounts import apply_discount, discount_amount
from jobsched.errors import ValidationError


def test_no_discount_returns_subtotal_unchanged():
    assert apply_discount(2500) == 2500
    assert discount_amount(2500) == 0


def test_percentage_discount_rounds_half_up_once():
    # 50% of 1001 is 500.5 -> removes 501 cents, net 500 (rule 1).
    assert apply_discount(1001, 5000) == 500
    assert discount_amount(1001, 5000) == 501


def test_fixed_discount_applied_after_percentage():
    # 10% of 20000 = 2000, then 500 fixed -> 17500 (rules 1-2).
    assert apply_discount(20000, 1000, 500) == 17500
    assert discount_amount(20000, 1000, 500) == 2500


def test_result_never_negative_and_amount_is_clamped_total_removed():
    assert apply_discount(100, 10000, 50) == 0
    assert discount_amount(100, 10000, 50) == 100  # clamped, not 105 (rules 2-3)


def test_zero_subtotal_stays_zero():
    assert apply_discount(0, 5000, 25) == 0
    assert discount_amount(0, 5000, 25) == 0


def test_full_percentage_discount_returns_zero():
    assert apply_discount(4321, 10000) == 0


@pytest.mark.parametrize(
    ("subtotal", "bp", "off"),
    [(-1, 0, 0), (1000, 10001, 0), (1000, -1, 0), (1000, 0, -5)],
)
def test_invalid_arguments_raise_validation_error(subtotal, bp, off):
    with pytest.raises(ValidationError):
        apply_discount(subtotal, bp, off)
