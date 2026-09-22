"""Late-fee computation for overdue invoices: grace period, daily rate, cap.

New billing feature; owns no storage and modifies no existing module.
"""

from __future__ import annotations

from jobsched.errors import ValidationError
from jobsched.utils.money import round_half_up


def late_fee_cents(
    invoice_total_cents: int,
    days_overdue: int,
    grace_days: int = 5,
    daily_rate_bp: int = 50,
) -> int:
    """Late fee in cents for an invoice total that is `days_overdue` days late.

    Days within the grace period accrue nothing. The exact fee is
    `total * daily_rate_bp * chargeable_days / 10000`, rounded half-up once,
    then capped at the invoice total.
    """
    _validate(invoice_total_cents, days_overdue, grace_days, daily_rate_bp)
    chargeable_days = max(0, days_overdue - grace_days)
    if chargeable_days == 0:
        return 0
    exact = invoice_total_cents * daily_rate_bp * chargeable_days / 10000
    return min(invoice_total_cents, round_half_up(exact))


def _validate(
    invoice_total_cents: int,
    days_overdue: int,
    grace_days: int,
    daily_rate_bp: int,
) -> None:
    if invoice_total_cents < 0:
        raise ValidationError("invoice total must not be negative")
    if days_overdue < 0:
        raise ValidationError("days_overdue must not be negative")
    if grace_days < 0:
        raise ValidationError("grace_days must not be negative")
    if daily_rate_bp < 0:
        raise ValidationError("daily_rate_bp must not be negative")
