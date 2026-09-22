"""Tests for budget enforcement in anymodel_subagents.jobs.JobManager.

Follows tests/test_jobs.py's conventions: a fake `run_worker` accepting
**kwargs (as the real engine.run_worker and every other fake do), a
monkeypatched `validate_cwd`, and an injected `Budget` (or a monkeypatched
`ledger.spent_on`) instead of anything real OpenRouter-shaped.

Engine contract under test: `run_worker` receives `on_cost(cost)`, called
after each model response; when it returns a reason string, the job ends with
status "budget_exceeded" and that reason as its error. A queued job whose
swarm's cap is already spent ends "budget_exceeded" without ever reaching
run_worker. Caps come from config.yaml (via Config) or an injected Budget;
nothing in this codebase can change or reset them.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from anymodel_subagents import jobs, ledger, server
from anymodel_subagents.budget import Budget
from anymodel_subagents.config import Config
from anymodel_subagents.types import Usage, WorkerResult

SENTINEL_KEY = "sk-or-v1-supersecretsentinelkeydonotleak0000"


# ---------------------------------------------------------------------------
# Shared fakes / helpers (mirroring tests/test_jobs.py)
# ---------------------------------------------------------------------------


class FakeClient:
    def redaction_secrets(self) -> list[str]:
        return [SENTINEL_KEY]


def make_cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "default_model": "test/default-model",
        "max_concurrency": 2,
        "max_turns": 5,
        "timeout_s": 30.0,
        "max_tasks_per_dispatch": 5,
        "allowed_roots": (),
        "job_retention_days": 7,
    }
    base.update(overrides)
    return Config(**base)


def fake_validate_cwd(cwd: str, cfg: Config) -> Path:
    p = Path(cwd)
    if not p.is_absolute():
        raise ValueError("cwd must be an absolute path")
    return p


class CostingRunWorker:
    """Fake engine.run_worker keyed by task_prompt, reporting spend via `on_cost`.

    Mirrors the engine contract: when `on_cost(cost)` returns a reason, the
    job ends "budget_exceeded" with that reason as its error; otherwise it
    completes normally after reporting all of its costs.
    """

    def __init__(self, costs_by_prompt: dict[str, list[float]] | None = None) -> None:
        self.costs_by_prompt = costs_by_prompt or {}
        self.calls: list[dict[str, Any]] = []
        self.on_cost_results: list[str | None] = []

    async def __call__(self, **kwargs: Any) -> WorkerResult:
        self.calls.append(kwargs)
        on_cost = kwargs.get("on_cost")
        prompt = kwargs["task_prompt"]
        spent = 0.0
        for cost in self.costs_by_prompt.get(prompt, []):
            reason = on_cost(cost) if callable(on_cost) else None
            self.on_cost_results.append(reason)
            if reason is not None:
                return WorkerResult(
                    status="budget_exceeded",
                    final_message="",
                    model=kwargs["model"],
                    turns=len(self.on_cost_results),
                    usage=Usage(),
                    error=reason,
                )
            spent += cost
        return WorkerResult(
            status="completed",
            final_message=f"done: {prompt}",
            model=kwargs["model"],
            turns=1,
            usage=Usage(cost=spent),
        )


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    start = time.monotonic()
    while True:
        if predicate():
            return True
        if time.monotonic() - start > timeout:
            return False
        await asyncio.sleep(interval)


def make_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: Config | None = None,
    budget: Budget | None = None,
    run_worker: Any = None,
) -> jobs.JobManager:
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return jobs.JobManager(
        cfg or make_cfg(),
        state,
        FakeClient,
        run_worker=run_worker if run_worker is not None else CostingRunWorker(),
        budget=budget,
    )


# ---------------------------------------------------------------------------
# Day cap: dispatch refused, nothing queued
# ---------------------------------------------------------------------------


async def test_dispatch_rejected_when_day_cap_already_reached(tmp_path, monkeypatch):
    worker = CostingRunWorker()
    budget = Budget(per_day_usd=1.0, spent_today_usd=2.0)
    mgr = make_manager(tmp_path, monkeypatch, budget=budget, run_worker=worker)

    with pytest.raises(ValueError, match="budget exceeded"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])

    assert mgr._jobs == {}  # nothing queued
    assert worker.calls == []  # and nothing reached the worker


async def test_dispatch_day_cap_rejection_covers_the_whole_call(tmp_path, monkeypatch):
    worker = CostingRunWorker()
    budget = Budget(per_day_usd=1.0, spent_today_usd=1.0)
    mgr = make_manager(tmp_path, monkeypatch, budget=budget, run_worker=worker)

    with pytest.raises(ValueError, match="budget exceeded"):
        await mgr.dispatch([jobs.TaskSpec(prompt=f"p{i}", cwd=str(tmp_path)) for i in range(3)])

    assert mgr._jobs == {}
    assert worker.calls == []


# ---------------------------------------------------------------------------
# Day budget is seeded from the ledger at manager construction
# ---------------------------------------------------------------------------


def test_day_budget_seeded_from_ledger_at_construction(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_spent_on(day, path=None):
        seen["day"] = day
        seen["path"] = path
        return 1.25

    monkeypatch.setattr(ledger, "spent_on", fake_spent_on, raising=False)
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(budget_per_day_usd=2.0))

    assert seen["path"] == mgr.ledger_path
    assert seen["day"] == datetime.now(UTC).astimezone().date()
    assert mgr.budget.snapshot() == {
        "per_swarm_usd": None,
        "per_day_usd": 2.0,
        "spent_today_usd": 1.25,
    }


def test_day_budget_seeding_survives_missing_or_broken_spent_on(tmp_path, monkeypatch):
    monkeypatch.delattr(ledger, "spent_on", raising=False)
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(budget_per_day_usd=2.0))
    assert mgr.budget.snapshot()["spent_today_usd"] == 0.0

    def broken(day, path=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ledger, "spent_on", broken, raising=False)
    mgr2 = make_manager(tmp_path, monkeypatch, cfg=make_cfg(budget_per_day_usd=2.0))
    assert mgr2.budget.snapshot()["spent_today_usd"] == 0.0


def test_injected_budget_is_used_as_is_not_reseeded(tmp_path, monkeypatch):
    budget = Budget(per_swarm_usd=1.0, per_day_usd=5.0, spent_today_usd=3.0)
    mgr = make_manager(tmp_path, monkeypatch, budget=budget)
    assert mgr.budget is budget
    assert mgr.budget.snapshot()["spent_today_usd"] == 3.0


# ---------------------------------------------------------------------------
# Per-swarm cap: a running job ends budget_exceeded via on_cost
# ---------------------------------------------------------------------------


async def test_per_swarm_cap_ends_running_job_budget_exceeded(tmp_path, monkeypatch):
    worker = CostingRunWorker({"p": [0.6, 0.6]})
    mgr = make_manager(tmp_path, monkeypatch, budget=Budget(per_swarm_usd=1.0), run_worker=worker)

    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])
    job_id = created[0].job_id

    assert await _wait_until(lambda: mgr.get(job_id).status == "budget_exceeded")
    job = mgr.get(job_id)
    # 0.6 < 1.0 let the swarm continue; the second 0.6 (total 1.2) returned a reason.
    assert worker.on_cost_results[0] is None
    reason = worker.on_cost_results[1]
    assert isinstance(reason, str)
    assert job.result.error == reason
    assert "budget_per_swarm_usd" in reason

    out = await mgr.wait([job_id], timeout_s=5.0)
    assert out["done"] is True
    assert out["statuses"][job_id] == "budget_exceeded"

    # Status and error surface through the `results` body too.
    results = server._collect_results(mgr, [job_id], include_message=True, full=True)
    entry = results["jobs"][job_id]
    assert entry["status"] == "budget_exceeded"
    assert entry["error"] == reason


async def test_budget_exceeded_job_finalized_like_max_turns(tmp_path, monkeypatch):
    """Terminal bookkeeping: meta.json, report.md, and a ledger entry, done event set."""
    worker = CostingRunWorker({"p": [2.0]})
    mgr = make_manager(tmp_path, monkeypatch, budget=Budget(per_swarm_usd=1.0), run_worker=worker)

    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])
    job_id = created[0].job_id
    assert await _wait_until(lambda: mgr.get(job_id).status == "budget_exceeded")

    started = time.monotonic()
    out = await mgr.wait([job_id], timeout_s=5.0)
    assert out["done"] is True
    assert time.monotonic() - started < 2.0  # done event set, not a timed-out poll

    entries = [
        json.loads(line)
        for line in mgr.ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["status"] for e in entries] == ["budget_exceeded"]
    assert mgr.report_path(job_id) is not None


# ---------------------------------------------------------------------------
# A queued sibling of the same swarm never starts once the cap is spent
# ---------------------------------------------------------------------------


async def test_second_queued_job_same_swarm_ends_budget_exceeded_without_running(
    tmp_path, monkeypatch
):
    worker = CostingRunWorker({"first": [5.0], "second": []})
    cfg = make_cfg(max_concurrency=1)
    mgr = make_manager(
        tmp_path, monkeypatch, cfg=cfg, budget=Budget(per_swarm_usd=1.0), run_worker=worker
    )

    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="first", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="second", cwd=str(tmp_path)),
        ]
    )
    first, second = created
    assert first.swarm_id == second.swarm_id
    assert await _wait_until(lambda: mgr.get(second.job_id).status == "budget_exceeded")

    assert mgr.get(first.job_id).status == "budget_exceeded"
    assert len(worker.calls) == 1  # only the first job ever reached run_worker
    assert worker.calls[0]["task_prompt"] == "first"

    second_job = mgr.get(second.job_id)
    assert second_job.result.turns == 0
    assert second_job.result.error == mgr.get(first.job_id).result.error

    # Terminal on disk too, same as the max_turns path would be.
    meta = json.loads(
        (mgr.ledger_path.parent / "jobs" / second.job_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["status"] == "budget_exceeded"
    assert meta["error"] == second_job.result.error


# ---------------------------------------------------------------------------
# Per-swarm caps are per swarm: a later swarm is not blocked
# ---------------------------------------------------------------------------


async def test_later_swarm_not_blocked_by_first_swarm_per_swarm_cap(tmp_path, monkeypatch):
    worker = CostingRunWorker({"a": [2.0], "b": [0.5]})
    mgr = make_manager(tmp_path, monkeypatch, budget=Budget(per_swarm_usd=1.0), run_worker=worker)

    _swarm1, created1 = await mgr.dispatch([jobs.TaskSpec(prompt="a", cwd=str(tmp_path))])
    assert await _wait_until(lambda: mgr.get(created1[0].job_id).status == "budget_exceeded")

    # A later dispatch is a new swarm id: its own per-swarm cap starts at zero,
    # and no per-day cap is configured here to stop it.
    _swarm2, created2 = await mgr.dispatch([jobs.TaskSpec(prompt="b", cwd=str(tmp_path))])
    assert created2[0].swarm_id != created1[0].swarm_id
    assert await _wait_until(lambda: mgr.get(created2[0].job_id).status == "completed")
    assert mgr.get(created2[0].job_id).result.error is None


# ---------------------------------------------------------------------------
# list_workers: the server block carries the live budget snapshot
# ---------------------------------------------------------------------------


async def test_list_workers_server_block_carries_budget_snapshot(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch, budget=Budget(per_swarm_usd=1.0, per_day_usd=10.0))
    mgr.budget.add("s-whatever", 0.25)

    list_workers = server._make_list_workers(
        {}, [], "m/default", None, {"version": "test"}, budget=mgr.budget
    )
    out = await list_workers()
    assert out["server"]["budget"] == {
        "per_swarm_usd": 1.0,
        "per_day_usd": 10.0,
        "spent_today_usd": 0.25,
    }


async def test_build_server_wires_manager_budget_into_list_workers(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch, budget=Budget(per_swarm_usd=1.0, spent_today_usd=0.5))
    mcp = server.build_server(mgr, server_info={"version": "test"})

    call = await mcp.call_tool("list_workers", {})
    assert call.is_error is False
    assert call.structured_content["server"]["budget"]["spent_today_usd"] == 0.5


def test_dispatch_description_tells_orchestrators_budgets_are_config_only():
    description = server._dispatch_description({})
    assert "config.yaml" in description
    assert "budget_exceeded" in description
