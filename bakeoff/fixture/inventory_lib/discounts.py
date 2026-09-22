"""Tiered volume-discount schedule.

The schedule is a list of (threshold, rate) pairs, sorted ascending by
threshold. A quantity qualifies for a tier when it has reached that
tier's threshold; the applicable rate is the rate of the highest
threshold the quantity has reached.

Example: with the default TIERS below, a quantity of 10 should receive
the 5% tier (it has *reached* 10), a quantity of 9 should still get 0%.
"""

from __future__ import annotations

# (minimum quantity to qualify, discount rate)
TIERS: list[tuple[int, float]] = [
    (0, 0.0),
    (10, 0.05),
    (50, 0.10),
    (100, 0.15),
]


def tier_discount_rate(quantity: int) -> float:
    """Return the discount rate that applies to an order of this quantity.

    Walks the tiers from lowest to highest threshold and keeps the rate
    of the last tier the quantity has reached.
    """
    if quantity < 0:
        raise ValueError(f"quantity must be >= 0, got {quantity!r}")

    rate = 0.0
    for threshold, tier_rate in TIERS:
        if quantity > threshold:
            rate = tier_rate
    return rate
