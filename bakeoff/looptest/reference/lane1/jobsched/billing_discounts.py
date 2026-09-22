"""Discount application for invoice subtotals: percentage then fixed, before tax.

New billing feature; owns no storage and modifies no existing module.
"""

from __future__ import annotations

from jobsched.errors import ValidationError
from jobsched.utils.money import round_half_up


def apply_discount(subtotal_cents: int, percent_off_bp: int = 0, off_cents: int = 0) -> int:
    """Return `subtotal_cents` less a percentage and then a fixed discount.

    The percentage discount is rounded half-up once; the fixed discount is
    subtracted afterwards. The result is clamped at zero cents.
    """
    _validate(subtotal_cents, percent_off_bp, off_cents)
    pct_discount = round_half_up(subtotal_cents * percent_off_bp / 10000)
    return max(0, subtotal_cents - pct_discount - off_cents)


def discount_amount(subtotal_cents: int, percent_off_bp: int = 0, off_cents: int = 0) -> int:
    """Total cents removed by `apply_discount` for the same arguments."""
    return subtotal_cents - apply_discount(subtotal_cents, percent_off_bp, off_cents)


def _validate(subtotal_cents: int, percent_off_bp: int, off_cents: int) -> None:
    if subtotal_cents < 0:
        raise ValidationError("subtotal must not be negative")
    if not 0 <= percent_off_bp <= 10000:
        raise ValidationError("percent_off_bp must be between 0 and 10000")
    if off_cents < 0:
        raise ValidationError("off_cents must not be negative")
