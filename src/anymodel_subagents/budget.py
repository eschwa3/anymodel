"""Spend budget: per-swarm and per-day caps on OpenRouter cost.

The caps come from config.yaml only (user-edited; no MCP tool can change or
reset them). `Budget` is in-memory bookkeeping: the day total is seeded from
the ledger at server start, then every model response's cost is added live,
so a running job can be stopped between requests rather than after it ends.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime


def _today() -> date:
    # The user's local day: that is what "per day" means to the person setting the cap.
    return datetime.now(UTC).astimezone().date()


class Budget:
    def __init__(
        self,
        per_swarm_usd: float | None = None,
        per_day_usd: float | None = None,
        spent_today_usd: float = 0.0,
        today: Callable[[], date] = _today,
    ) -> None:
        self.per_swarm_usd = per_swarm_usd
        self.per_day_usd = per_day_usd
        self._today = today
        self._day = today()
        self._spent_day = max(0.0, spent_today_usd)
        self._spent_swarm: dict[str, float] = {}

    def _roll(self) -> None:
        now = self._today()
        if now != self._day:
            self._day = now
            self._spent_day = 0.0

    def add(self, swarm_id: str, cost_usd: float) -> None:
        if not isinstance(cost_usd, (int, float)) or not math.isfinite(cost_usd) or cost_usd <= 0:
            return
        self._roll()
        self._spent_day += cost_usd
        self._spent_swarm[swarm_id] = self._spent_swarm.get(swarm_id, 0.0) + cost_usd

    def exceeded(self, swarm_id: str | None = None) -> str | None:
        """A short reason when a cap is reached, else None. Day cap is checked first."""
        self._roll()
        if self.per_day_usd is not None and self._spent_day >= self.per_day_usd:
            return (
                f"budget exceeded: budget_per_day_usd ${self.per_day_usd:.2f} reached "
                f"(${self._spent_day:.2f} spent today); raise it in config.yaml and restart"
            )
        if swarm_id is not None and self.per_swarm_usd is not None:
            spent = self._spent_swarm.get(swarm_id, 0.0)
            if spent >= self.per_swarm_usd:
                return (
                    f"budget exceeded: budget_per_swarm_usd ${self.per_swarm_usd:.2f} reached "
                    f"(${spent:.2f} spent by this swarm)"
                )
        return None

    def snapshot(self) -> dict[str, float | None]:
        self._roll()
        return {
            "per_swarm_usd": self.per_swarm_usd,
            "per_day_usd": self.per_day_usd,
            "spent_today_usd": round(self._spent_day, 6),
        }
