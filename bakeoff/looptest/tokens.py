"""Token usage of one loop-test arm, from Claude Code session logs.

  uv run python bakeoff/looptest/tokens.py <session.jsonl> [more.jsonl ...]
      [--start ISO] [--end ISO] [--csv series.csv] [--lead-model SUBSTR]

Pass the arm's main session log; its subagent logs (`<session>/subagents/agent-*.jsonl`, arm A)
are found automatically, and extra logs can be listed after it. Every API response is one row:
who made it (lead = not a sidechain, subagent = sidechain or a different file), its model, and its
four usage counters. Output is a JSON summary; `--csv` also writes the cumulative per-turn series
(one row per response, in time order) for plotting lead tokens over time.

Weighted usage is a SENSITIVITY TABLE, never one number: how Anthropic weighs a lead-model token
against a Sonnet token, and a cache read against a fresh token, toward usage limits is not
published. `units = model_weight * (input + cache_write + output + cache_read_x * cache_read)`,
with Sonnet/Haiku-class models at 1 and every other model (Fable, Opus, unknown) at 3 or 5,
cache reads at 0.1 or 0.25.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

COUNTERS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
LEAD_WEIGHTS = (3.0, 5.0)
CACHE_READ_WEIGHTS = (0.1, 0.25)


@dataclass
class Row:
    when: datetime
    who: str  # "lead" | "subagent"
    model: str
    usage: dict[str, int]

    @property
    def fresh(self) -> int:
        u = self.usage
        return u["input_tokens"] + u["cache_creation_input_tokens"] + u["output_tokens"]

    @property
    def context(self) -> int:
        u = self.usage
        return u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def read_rows(
    paths: list[Path], start: datetime | None = None, end: datetime | None = None
) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    main_log = paths[0]
    for path in paths:
        paths = [*paths, *sorted((path.parent / path.stem / "subagents").glob("agent-*.jsonl"))]
    for path in dict.fromkeys(paths):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            if not entry.get("timestamp"):
                continue
            when = _ts(entry["timestamp"])
            if (start and when < start) or (end and when > end):
                continue
            # One API response is logged once per content block: count it once.
            request = entry.get("requestId") or message.get("id") or entry.get("uuid")
            if request in seen:
                continue
            seen.add(request)
            usage = {k: int(message["usage"].get(k) or 0) for k in COUNTERS}
            if not any(usage.values()):
                continue
            who = "subagent" if entry.get("isSidechain") or path != main_log else "lead"
            rows.append(Row(when, who, str(message.get("model") or "unknown"), usage))
    rows.sort(key=lambda r: r.when)
    return rows


def _totals(rows: list[Row]) -> dict[str, int]:
    out = {k: sum(r.usage[k] for r in rows) for k in COUNTERS}
    out["fresh_tokens"] = sum(r.fresh for r in rows)
    out["responses"] = len(rows)
    return out


def sensitivity(rows: list[Row]) -> list[dict[str, float]]:
    table = []
    for lead_x in LEAD_WEIGHTS:
        for cache_x in CACHE_READ_WEIGHTS:
            units = 0.0
            for r in rows:
                cheap = any(name in r.model for name in ("sonnet", "haiku"))
                weight = 1.0 if cheap else lead_x  # Opus/Fable-class and unknown: lead tier
                units += weight * (r.fresh + cache_x * r.usage["cache_read_input_tokens"])
            table.append({"lead_x": lead_x, "cache_read_x": cache_x, "units": round(units)})
    return table


def summarize(rows: list[Row], lead_model: str | None = None) -> dict[str, object]:
    lead = [r for r in rows if r.who == "lead"]
    if lead_model is None:
        # The model the lead used most is "the lead model" for weighting.
        counts: dict[str, int] = {}
        for r in lead:
            counts[r.model] = counts.get(r.model, 0) + 1
        lead_model = max(counts, key=counts.get) if counts else ""
    by_model: dict[str, dict[str, int]] = {}
    for model in sorted({r.model for r in rows}):
        by_model[model] = _totals([r for r in rows if r.model == model])
    minutes = (rows[-1].when - rows[0].when).total_seconds() / 60 if rows else 0.0
    return {
        "lead_model": lead_model,
        "wall_minutes": round(minutes, 1),
        "lead": _totals(lead),
        "subagents": _totals([r for r in rows if r.who == "subagent"]),
        "lead_peak_context": max((r.context for r in lead), default=0),
        "lead_mean_context": round(sum(r.context for r in lead) / len(lead)) if lead else 0,
        "by_model": by_model,
        "weighted_units_sensitivity": sensitivity(rows),
    }


def write_series(rows: list[Row], path: Path) -> None:
    running = dict.fromkeys(("lead_fresh", "lead_cache_read", "sub_fresh", "sub_cache_read"), 0)
    start = rows[0].when if rows else None
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["minute", "who", "model", *COUNTERS, "context", *(f"cum_{k}" for k in running)]
        )
        for r in rows:
            prefix = "lead" if r.who == "lead" else "sub"
            running[f"{prefix}_fresh"] += r.fresh
            running[f"{prefix}_cache_read"] += r.usage["cache_read_input_tokens"]
            minute = round((r.when - start).total_seconds() / 60, 2) if start else 0
            writer.writerow(
                [minute, r.who, r.model, *(r.usage[k] for k in COUNTERS), r.context]
                + list(running.values())
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--start", type=_ts, default=None)
    parser.add_argument("--end", type=_ts, default=None)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--lead-model", default=None, help="substring of the lead's model id")
    args = parser.parse_args(argv)
    rows = read_rows(args.logs, args.start, args.end)
    if args.csv:
        write_series(rows, args.csv)
    print(json.dumps(summarize(rows, args.lead_model), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
