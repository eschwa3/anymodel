"""Clock helpers.

`now()` returns naive local time and predates the rest of the codebase
adopting timezone-aware timestamps; `utcnow()` is the timezone-aware
replacement. New code should call `utcnow()`.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now()  # noqa: DTZ005 -- deliberately naive; see module docstring


def utcnow() -> datetime:
    return datetime.now(UTC)
