"""Money helpers. All monetary values in this codebase are integer cents."""

from __future__ import annotations

import math


def round_half_up(amount: float) -> int:
    """Round a fractional cent amount to the nearest integer cent, ties up."""
    return math.floor(amount + 0.5)


def cents_to_str(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    whole = abs(cents) // 100
    frac = abs(cents) % 100
    return f"{sign}{whole}.{frac:02d} {currency}"
