"""Append-only JSONL usage ledger."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: no POSIX file locks, no Seatbelt/bwrap sandbox either
    raise SystemExit(
        "anymodel-subagents runs on macOS and Linux. On Windows, run it inside WSL2 "
        "(https://learn.microsoft.com/windows/wsl/) and install bubblewrap there."
    ) from None
import json
import math
import os
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from anymodel_subagents.config import state_dir
from anymodel_subagents.redact import redact_deep
from anymodel_subagents.types import WorkerResult

GroupBy = Literal["day", "model", "role", "swarm"]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def record(
    path: Path,
    *,
    job_id: str,
    swarm_id: str | None,
    role: str,
    model: str,
    result: WorkerResult,
    secrets: list[str] | None = None,
) -> None:
    """Append one job's outcome to the ledger at `path`.

    `secrets` (live secret values, e.g. an API key) are scrubbed from every
    string field before writing, on top of the generic key-shaped patterns
    `redact()` always checks for -- the ledger is not exempt just because it's
    meant to hold structured metadata rather than free-form transcript text.
    """
    _ensure_parent(path)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "job_id": job_id,
        "swarm_id": swarm_id,
        "role": role,
        "model": model,
        "status": result.status,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "invalid_tool_calls": result.invalid_tool_calls,
        "changed_files": result.changed_files,
        "sensitive_changed_files": result.sensitive_changed_files,
        "duration_s": result.duration_s,
        "usage": asdict(result.usage),
        "error": result.error,
    }
    entry = redact_deep(entry, secrets)
    line = json.dumps(entry, default=str) + "\n"

    # Create (if needed) with 0o600 and append -- the ledger can accumulate
    # cost/usage data and error text across many jobs and must not be
    # world/group readable.
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        f = os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def _read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def spent_on(day: date, path: Path | None = None) -> float:
    """Total ledger cost recorded on `day`, as a local-calendar date.

    Sums `usage.cost` of every entry whose `ts` -- converted to the machine's
    LOCAL timezone (naive timestamps are read as UTC, like `summarize`) -- falls
    on `day`. That is the same "local day" the per-day budget cap uses
    (budget.py), which seeds its running total from this at server start.

    Never raises: a missing or unreadable ledger, malformed lines, and entries
    with missing/negative/non-finite costs are skipped, so a broken ledger can
    only make the seeded total too low, never crash the server. `path=None`
    means the canonical ledger (`<state_dir>/ledger.jsonl`).
    """
    if path is None:
        try:
            path = state_dir() / "ledger.jsonl"
        except OSError:
            return 0.0
    try:
        entries = _read_entries(path)
    except OSError:
        return 0.0

    total = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts_raw = entry.get("ts")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts.astimezone().date() != day:
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        cost = usage.get("cost")
        # bool is an int subclass; a `true` cost is garbage, not $1.
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        if not math.isfinite(cost) or cost <= 0:
            continue
        total += cost
    return total


def since_day_start(days: int, *, now: datetime | None = None) -> datetime:
    """Absolute cutoff for a `since_days`-style window, as an aware UTC datetime.

    `days=0` is the start of *today in the machine's local timezone* -- not a
    rolling 24h window and not the UTC day boundary -- which is what the
    `usage` tool documents and what a person asking for "today" means. It also
    matches the per-day budget cap's local-day semantics (budget.py's
    `_today`). `now` is injection for tests.
    """
    now_local = (now or datetime.now(UTC)).astimezone()
    start_local = datetime.combine(
        now_local.date() - timedelta(days=days), time.min, tzinfo=now_local.tzinfo
    )
    return start_local.astimezone(UTC)


def _key_for(entry: dict[str, Any], group_by: GroupBy) -> str:
    if group_by == "day":
        ts = entry.get("ts") or ""
        return ts[:10] if ts else "unknown"
    if group_by == "model":
        return entry.get("model") or "unknown"
    if group_by == "role":
        return entry.get("role") or "unknown"
    if group_by == "swarm":
        return entry.get("swarm_id") or "unknown"
    raise ValueError(f"unknown group_by: {group_by!r}")


def summarize(
    path: Path, *, since: datetime | None = None, group_by: GroupBy = "day"
) -> dict[str, dict[str, Any]]:
    """Roll up ledger entries at `path` into per-group totals."""
    entries = _read_entries(path)

    if since is not None:
        since_cmp = since if since.tzinfo else since.replace(tzinfo=UTC)
        kept = []
        for e in entries:
            try:
                e_dt = datetime.fromisoformat(e.get("ts", ""))
            except ValueError:
                continue
            if e_dt.tzinfo is None:
                e_dt = e_dt.replace(tzinfo=UTC)
            if e_dt >= since_cmp:
                kept.append(e)
        entries = kept

    groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = _key_for(entry, group_by)
        g = groups.setdefault(
            key,
            {
                "jobs": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "cost": 0.0,
                "requests": 0,
                "statuses": {},
            },
        )
        g["jobs"] += 1
        usage = entry.get("usage") or {}
        g["prompt_tokens"] += usage.get("prompt_tokens") or 0
        g["completion_tokens"] += usage.get("completion_tokens") or 0
        g["cached_tokens"] += usage.get("cached_tokens") or 0
        g["reasoning_tokens"] += usage.get("reasoning_tokens") or 0
        g["cost"] += usage.get("cost") or 0.0
        g["requests"] += usage.get("requests") or 0
        status = entry.get("status", "unknown")
        g["statuses"][status] = g["statuses"].get(status, 0) + 1

    return groups
