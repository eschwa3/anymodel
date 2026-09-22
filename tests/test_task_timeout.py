"""Tests for the per-task `timeout_s` field added to `dispatch`.

Mirrors the existing `max_turns` validation/clamp coverage in test_jobs.py and
the MCP-level argument plumbing coverage in test_server.py. `worktree.py` is
owned by a different workstream, so the same fake-module bootstrap those files
use is repeated here (see their module docstrings for why).
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


def _install_fake_worktree_module() -> types.ModuleType:
    mod = types.ModuleType("anymodel_subagents.worktree")

    class WorktreeError(Exception):
        pass

    @dataclass
    class WorktreeInfo:
        repo_root: Path
        path: Path
        workdir: Path
        branch: str
        base_commit: str
        dirty: bool = False

    @dataclass
    class WorktreeOutcome:
        changed_files: list[str]
        kept: bool
        commit: str | None
        policy_reverted_files: list[str] = None  # type: ignore[assignment]
        policy_notes: list[str] = None  # type: ignore[assignment]
        sensitive_files: list[str] = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            if self.policy_reverted_files is None:
                self.policy_reverted_files = []
            if self.policy_notes is None:
                self.policy_notes = []
            if self.sensitive_files is None:
                self.sensitive_files = []

    async def create_worktree(cwd: Path, job_id: str, state: Path) -> WorktreeInfo:
        return WorktreeInfo(
            repo_root=Path(cwd),
            path=state / "worktrees" / job_id,
            workdir=state / "worktrees" / job_id,
            branch=f"anymodel/{job_id}",
            base_commit="0" * 40,
        )

    async def finalize_worktree(
        info: WorktreeInfo, message: str, *, is_write_denied=None, is_sensitive=None
    ) -> WorktreeOutcome:
        return WorktreeOutcome(changed_files=[], kept=False, commit=None)

    async def remove_worktree(info: WorktreeInfo) -> None:
        return None

    async def sweep(state: Path, retention_days: int) -> None:
        return None

    async def snapshot_in_place(cwd: Path) -> str:
        return "{}"

    async def changed_files_in_place(cwd: Path, before: str) -> list[str]:
        return []

    mod.WorktreeError = WorktreeError
    mod.WorktreeInfo = WorktreeInfo
    mod.WorktreeOutcome = WorktreeOutcome
    mod.create_worktree = create_worktree
    mod.finalize_worktree = finalize_worktree
    mod.remove_worktree = remove_worktree
    mod.sweep = sweep
    mod.snapshot_in_place = snapshot_in_place
    mod.changed_files_in_place = changed_files_in_place
    return mod


def _ensure_worktree_module() -> None:
    if "anymodel_subagents.worktree" in sys.modules:
        return
    try:
        import anymodel_subagents.worktree  # noqa: F401
    except ImportError:
        sys.modules["anymodel_subagents.worktree"] = _install_fake_worktree_module()


_ensure_worktree_module()

from anymodel_subagents import jobs, server
from anymodel_subagents.config import Config
from anymodel_subagents.types import Usage, WorkerResult


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


async def immediate_run_worker(**kwargs: Any) -> WorkerResult:
    return WorkerResult(
        status="completed",
        final_message="done",
        model=kwargs["model"],
        turns=1,
        usage=Usage(cost=0.001),
    )


class RecordingRunWorker:
    """Fake `run_worker` that just captures the kwargs it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> WorkerResult:
        self.calls.append(kwargs)
        return WorkerResult(
            status="completed",
            final_message="done",
            model=kwargs["model"],
            turns=1,
            usage=Usage(cost=0.001),
        )


class RealTimeoutRunWorker:
    """Fake `run_worker` that actually honors the `timeout_s` it's given via
    `asyncio.timeout`, the same mechanism `engine.run_worker` uses -- so a job
    genuinely ends with status "timeout" when its effective timeout is small,
    without needing the real engine/OpenRouter client.
    """

    async def __call__(self, **kwargs: Any) -> WorkerResult:
        timeout_s = kwargs["timeout_s"]
        try:
            async with asyncio.timeout(timeout_s):
                await asyncio.sleep(timeout_s + 5)
        except TimeoutError:
            return WorkerResult(
                status="timeout",
                final_message="Worker timed out before completing.",
                model=kwargs["model"],
                turns=1,
                usage=Usage(),
            )
        return WorkerResult(
            status="completed", final_message="done", model=kwargs["model"], turns=1, usage=Usage()
        )


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
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
    run_worker=immediate_run_worker,
    cfg: Config | None = None,
) -> jobs.JobManager:
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)

    def client_factory():
        class FakeClient:
            def redaction_secrets(self) -> list[str]:
                return []

        return FakeClient()

    return jobs.JobManager(cfg or make_cfg(), state, client_factory, run_worker=run_worker)


# ---------------------------------------------------------------------------
# clamping / defaulting
# ---------------------------------------------------------------------------


async def test_default_timeout_is_config_value(tmp_path, monkeypatch):
    cfg = make_cfg(timeout_s=42.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])

    assert created[0].spec.timeout_s == 42.0


async def test_shorter_timeout_is_honored(tmp_path, monkeypatch):
    cfg = make_cfg(timeout_s=900.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=50.0)]
    )

    assert created[0].spec.timeout_s == 50.0


async def test_longer_timeout_is_clamped_to_config(tmp_path, monkeypatch):
    cfg = make_cfg(timeout_s=30.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=99999.0)]
    )

    assert created[0].spec.timeout_s == 30.0


async def test_tiny_timeout_is_clamped_to_the_floor(tmp_path, monkeypatch):
    cfg = make_cfg(timeout_s=900.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=1.0)]
    )

    assert created[0].spec.timeout_s == jobs._MIN_TIMEOUT_S


async def test_config_ceiling_wins_even_below_the_floor(tmp_path, monkeypatch):
    """A pathological config below the floor is still the hard ceiling."""
    cfg = make_cfg(timeout_s=5.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=1.0)]
    )

    assert created[0].spec.timeout_s == 5.0


async def test_huge_int_timeout_clamps_to_config_ceiling(tmp_path, monkeypatch):
    # Regression: float(10**400) raises OverflowError (int too large for a C double), which
    # used to escape `_validate_task` as an unhandled error instead of clamping like any
    # other oversized timeout. Chosen behavior: clamp-to-cap, matching the existing "longer
    # timeout is clamped to config" semantics for ordinary values.
    cfg = make_cfg(timeout_s=30.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=10**400)]
    )

    assert created[0].spec.timeout_s == 30.0
    assert isinstance(created[0].spec.timeout_s, float)


async def test_huge_negative_int_timeout_rejected(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="timeout_s must be a positive number"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=-(10**400))])


class _FloatCoercible:
    """An object that `float()` would happily accept -- must still be rejected:
    fix 2 accepts only real int/float, never anything merely float()-coercible.
    """

    def __float__(self) -> float:
        return 1e9


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        "abc",
        0,
        -5,
        -5.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        # Regression for fix 2: these used to sail through `float(timeout_s)`
        # and become valid (if oversized) timeouts instead of being rejected.
        "50",
        "1e9",
        Decimal(50),
        _FloatCoercible(),
    ],
)
async def test_invalid_timeout_values_rejected(tmp_path, monkeypatch, bad):
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="timeout_s must be a positive number"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=bad)])


async def test_effective_timeout_is_recorded_in_meta(tmp_path, monkeypatch):
    """The effective (clamped) value is what ends up on the persisted job spec,
    following the same pattern as `max_turns`.
    """
    cfg = make_cfg(timeout_s=900.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=77.0)]
    )
    job_id = created[0].job_id
    assert await _wait_until(lambda: mgr.get(job_id).status == "completed")

    import json

    meta = json.loads((tmp_path / "state" / "jobs" / job_id / "meta.json").read_text())
    assert meta["spec"]["timeout_s"] == 77.0


# ---------------------------------------------------------------------------
# wiring: the effective value actually reaches the engine
# ---------------------------------------------------------------------------


async def test_effective_timeout_is_passed_to_the_engine(tmp_path, monkeypatch):
    cfg = make_cfg(timeout_s=900.0)
    recorder = RecordingRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=recorder, cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=15.0)]
    )
    assert await _wait_until(lambda: mgr.get(created[0].job_id).status == "completed")

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["timeout_s"] == 15.0


async def test_shorter_task_timeout_ends_the_job_as_timeout(tmp_path, monkeypatch):
    """A task-level timeout shorter than what the job needs ends the job with
    status "timeout" -- exercised end-to-end through JobManager, with a fake
    engine that honors `timeout_s` via `asyncio.timeout` exactly like the real
    one, kept small so the test stays fast.
    """
    monkeypatch.setattr(jobs, "_MIN_TIMEOUT_S", 0.02)
    cfg = make_cfg(timeout_s=5.0)
    mgr = make_manager(tmp_path, monkeypatch, run_worker=RealTimeoutRunWorker(), cfg=cfg)

    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd=str(tmp_path), timeout_s=0.02)]
    )
    job_id = created[0].job_id

    assert await _wait_until(lambda: mgr.get(job_id).status == "timeout")
    job = mgr.get(job_id)
    assert job.result.status == "timeout"


# ---------------------------------------------------------------------------
# MCP layer: `dispatch` tool accepts the field
# ---------------------------------------------------------------------------


class FakeJobManager:
    """Minimal double for the subset of JobManager's interface `dispatch` uses."""

    def __init__(self) -> None:
        self.dispatch_calls: list[list[jobs.TaskSpec]] = []

    def redaction_secrets(self) -> list[str]:
        return []

    async def dispatch(self, specs: list[jobs.TaskSpec]):
        self.dispatch_calls.append(specs)
        job = jobs.Job(
            job_id="j-aaaaaaaa",
            swarm_id="s-swarm0001",
            spec=specs[0],
        )
        job.status = "queued"
        return "s-swarm0001", [job]


async def test_mcp_dispatch_accepts_timeout_s():
    mgr = FakeJobManager()
    dispatch = server._make_dispatch(mgr)

    out = await dispatch(tasks=[{"prompt": "hello", "cwd": "/tmp/repo", "timeout_s": 123.0}])

    assert "error" not in out
    assert len(mgr.dispatch_calls) == 1
    assert mgr.dispatch_calls[0][0].timeout_s == 123.0


# ---------------------------------------------------------------------------
# `_job_from_meta`: loading meta.json written before `timeout_s` existed
# ---------------------------------------------------------------------------


async def test_job_from_meta_without_timeout_s_key(tmp_path, monkeypatch):
    """An old `meta.json` on disk (written before this field existed) has no
    `timeout_s` key in its `spec` object at all -- `_job_from_meta` must still
    load it, with `TaskSpec.timeout_s` falling back to its own default (None).
    """
    mgr = make_manager(tmp_path, monkeypatch)
    meta = {
        "job_id": "j-aaaaaaaa",
        "swarm_id": "s-swarm0001",
        "status": "completed",
        "spec": {"prompt": "p", "cwd": str(tmp_path)},  # no "timeout_s" key
    }

    job = mgr._job_from_meta(meta)

    assert job is not None
    assert job.spec.timeout_s is None


# ---------------------------------------------------------------------------
# MCP arg-model layer: pydantic coercion of `timeout_s` before `_validate_task`
# ---------------------------------------------------------------------------


def test_mcp_arg_model_rejects_bool_and_numeric_string_timeout_s():
    """Pins fix 2's `server.py` half: pydantic's default (lax) mode would
    coerce a JSON `true` into `1.0` and a numeric string like `"50"` into
    `50.0` before `_validate_task` (which rejects both outright when called
    directly) ever saw them. `TaskArg.timeout_s` is now `StrictFloat |
    StrictInt`, so both are rejected at the MCP arg-model layer instead.
    """
    from mcp.server.mcpserver.utilities.func_metadata import func_metadata
    from pydantic import ValidationError

    meta = func_metadata(server._make_dispatch(object()))

    with pytest.raises(ValidationError):
        meta.arg_model.model_validate(
            {"tasks": [{"prompt": "p", "cwd": "/tmp/repo", "timeout_s": True}]}
        )
    with pytest.raises(ValidationError):
        meta.arg_model.model_validate(
            {"tasks": [{"prompt": "p", "cwd": "/tmp/repo", "timeout_s": "50"}]}
        )

    # A real number of either JSON type still passes through untouched.
    parsed = meta.arg_model.model_validate(
        {"tasks": [{"prompt": "p", "cwd": "/tmp/repo", "timeout_s": 50}]}
    )
    assert parsed.tasks[0]["timeout_s"] == 50
