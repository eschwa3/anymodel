"""Tests for the optional `settle_s` parameter on `JobManager.wait` (jobs.py)
and the `wait` MCP tool (server.py).

`worktree.py` is owned by a different, concurrently-in-progress workstream, so
the same fake-module bootstrap `test_jobs.py`/`test_task_timeout.py` use is
repeated here (see their module docstrings for why): prefer the real module
when it's importable, fall back to a minimal fake only if it isn't yet.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
import types
from dataclasses import dataclass
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

SENTINEL_KEY = "sk-or-v1-supersecretsentinelkeydonotleak0000"


# ---------------------------------------------------------------------------
# Shared fakes / helpers (mirrors test_jobs.py)
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, key: str = SENTINEL_KEY) -> None:
        self._key = key

    def redaction_secrets(self) -> list[str]:
        return [self._key]

    async def aclose(self) -> None:
        pass


def make_client_factory(key: str | None = SENTINEL_KEY):
    def factory():
        env_key = os.environ.get("OPENROUTER_API_KEY")
        return FakeClient(env_key or key)

    return factory


def make_cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "default_model": "test/default-model",
        "max_concurrency": 4,
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


def make_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_worker=None,
    cfg: Config | None = None,
) -> jobs.JobManager:
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return jobs.JobManager(
        cfg or make_cfg(),
        state,
        make_client_factory(),
        run_worker=run_worker or immediate_run_worker,
    )


async def immediate_run_worker(**kwargs: Any) -> WorkerResult:
    return WorkerResult(
        status="completed",
        final_message="done",
        model=kwargs["model"],
        turns=1,
        usage=Usage(cost=0.001),
    )


class ControlledRunWorker:
    """A fake `run_worker` whose calls block until released, for precise timing."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._releases: dict[str, asyncio.Event] = {}

    def release_for(self, task_prompt: str) -> None:
        self._releases.setdefault(task_prompt, asyncio.Event()).set()

    async def __call__(self, **kwargs: Any) -> WorkerResult:
        key = kwargs["task_prompt"]
        self.calls.append(key)
        release = self._releases.setdefault(key, asyncio.Event())
        await release.wait()
        return WorkerResult(
            status="completed",
            final_message="done",
            model=kwargs["model"],
            turns=1,
            usage=Usage(cost=0.01),
        )


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    start = time.monotonic()
    while True:
        if predicate():
            return True
        if time.monotonic() - start > timeout:
            return False
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# JobManager.wait: settle_s semantics
# ---------------------------------------------------------------------------


async def test_settle_none_matches_default_behavior_exactly(tmp_path, monkeypatch):
    """settle_s=None (the default) must be byte-identical to calling wait()
    with no settle_s at all -- same keys, same values, no `settle_s` key."""
    mgr = make_manager(tmp_path, monkeypatch)
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])
    assert await _wait_until(lambda: mgr.get(created[0].job_id).status == "completed")
    job_id = created[0].job_id

    without = await mgr.wait([job_id], timeout_s=1.0, mode="all")
    with_none = await mgr.wait([job_id], timeout_s=1.0, mode="all", settle_s=None)

    assert without == with_none
    assert "settle_s" not in without


async def test_settle_returns_early_with_done_false_for_still_running_job(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]

    crw.release_for("a")
    start = time.monotonic()
    result = await mgr.wait(ids, timeout_s=5.0, mode="all", settle_s=0.1)
    elapsed = time.monotonic() - start

    # Returned once the settle window (~0.1s after "a" finished) elapsed, not
    # anywhere close to the full 5s "all" timeout.
    assert elapsed < 1.0
    assert result["done"] is False
    assert result["statuses"][ids[0]] == "completed"
    assert result["statuses"][ids[1]] == "running"
    assert result["settle_s"] == 0.1

    crw.release_for("b")


async def test_settle_collects_second_job_finishing_inside_window(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]

    async def release_b_soon() -> None:
        await asyncio.sleep(0.05)
        crw.release_for("b")

    crw.release_for("a")
    asyncio.create_task(release_b_soon())

    result = await mgr.wait(ids, timeout_s=5.0, mode="all", settle_s=0.3)

    assert result["done"] is True
    assert result["statuses"][ids[0]] == "completed"
    assert result["statuses"][ids[1]] == "completed"


async def test_settle_all_terminal_at_call_time_returns_immediately(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]
    crw.release_for("a")
    crw.release_for("b")
    assert await _wait_until(lambda: all(mgr.get(jid).status == "completed" for jid in ids))

    start = time.monotonic()
    result = await mgr.wait(ids, timeout_s=5.0, mode="all", settle_s=2.0)
    elapsed = time.monotonic() - start

    assert result["done"] is True
    assert elapsed < 1.0  # never waited out anything close to the 2s settle window


async def test_settle_no_completion_falls_back_to_normal_timeout(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="slow", cwd=str(tmp_path))])

    start = time.monotonic()
    result = await mgr.wait([created[0].job_id], timeout_s=0.15, mode="all", settle_s=0.5)
    elapsed = time.monotonic() - start

    # No job ever finished -- settle_s is clamped to the (smaller) applied
    # timeout, so this behaves exactly like today's plain timeout.
    assert result["done"] is False
    assert result["timeout_s"] == pytest.approx(0.15)
    assert result["settle_s"] == pytest.approx(0.15)
    assert elapsed < 1.0
    crw.release_for("slow")


async def test_settle_already_terminal_job_starts_window_at_call_time(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]
    crw.release_for("a")
    assert await _wait_until(lambda: mgr.get(ids[0]).status == "completed")

    start = time.monotonic()
    result = await mgr.wait(ids, timeout_s=5.0, mode="all", settle_s=0.2)
    elapsed = time.monotonic() - start

    # "a" was already terminal before wait() was even called -- the settle
    # window still starts at call time, so this returns around 0.2s, not 5s.
    assert 0.15 <= elapsed < 1.0
    assert result["done"] is False
    crw.release_for("b")


async def test_settle_zero_returns_at_first_completion_like_mode_any(tmp_path, monkeypatch):
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]
    crw.release_for("a")

    start = time.monotonic()
    result = await mgr.wait(ids, timeout_s=5.0, mode="all", settle_s=0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert result["done"] is False
    assert result["settle_s"] == 0
    crw.release_for("b")


async def test_settle_mode_any_still_collects_within_window(tmp_path, monkeypatch):
    """mode="any" already returns at first completion; settle_s documents
    that it behaves the same as mode="all" -- it extends collection, not
    what `done` means (which is already True once any job is terminal)."""
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]

    async def release_b_soon() -> None:
        await asyncio.sleep(0.05)
        crw.release_for("b")

    crw.release_for("a")
    asyncio.create_task(release_b_soon())

    result = await mgr.wait(ids, timeout_s=5.0, mode="any", settle_s=0.3)

    assert result["done"] is True  # true from "a" alone under mode="any"
    assert result["statuses"][ids[0]] == "completed"
    assert result["statuses"][ids[1]] == "completed"  # but "b" was still collected


# ---------------------------------------------------------------------------
# JobManager.wait: settle_s validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_settle",
    [True, False, "5", float("nan"), float("inf"), float("-inf"), -1, -0.5],
)
async def test_settle_rejects_invalid_values(tmp_path, monkeypatch, bad_settle):
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="settle_s"):
        await mgr.wait(["j-doesnotexist"], timeout_s=1.0, mode="all", settle_s=bad_settle)


async def test_settle_zero_is_valid(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch)
    result = await mgr.wait(["j-doesnotexist"], timeout_s=1.0, mode="all", settle_s=0)
    assert result["settle_s"] == 0
    assert result["unknown"] == ["j-doesnotexist"]


@pytest.mark.parametrize("bad_settle", [10**400, -(10**400)])
async def test_settle_huge_int_rejected_without_overflowerror(tmp_path, monkeypatch, bad_settle):
    # Regression: float(10**400) raises OverflowError, not ValueError -- this used to escape
    # as an unhandled "internal error" instead of the normal, short settle_s rejection.
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="settle_s") as excinfo:
        await mgr.wait(["j-doesnotexist"], timeout_s=1.0, mode="all", settle_s=bad_settle)
    assert not isinstance(excinfo.value, OverflowError)


async def test_settle_negative_zero_normalizes_to_positive_zero(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch)
    result = await mgr.wait(["j-doesnotexist"], timeout_s=1.0, mode="all", settle_s=-0.0)
    assert result["settle_s"] == 0.0
    assert math.copysign(1.0, result["settle_s"]) == 1.0


@pytest.mark.parametrize("bad_timeout", [10**400, -(10**400)])
async def test_wait_own_timeout_s_huge_int_clamps_without_overflowerror(
    tmp_path, monkeypatch, bad_timeout
):
    # Regression: `wait`'s own timeout_s had the same float(huge_int) OverflowError. Its
    # existing semantics are "clamp", so a too-large magnitude clamps to the cap/floor
    # instead of raising.
    cfg = make_cfg(max_wait_s=1.0)
    mgr = make_manager(tmp_path, monkeypatch, cfg=cfg)
    result = await mgr.wait(["j-doesnotexist"], timeout_s=bad_timeout, mode="all")
    assert 0.0 <= result["timeout_s"] <= 1.0
    assert math.isfinite(result["timeout_s"])


async def test_settle_s_with_huge_value_stays_within_max_wait_cap(tmp_path, monkeypatch):
    # PoC 1 (poc_settle.py): settle_s=1e6 must never push the wait past max_wait_s.
    cfg = make_cfg(max_wait_s=1.0)
    crw = ControlledRunWorker()
    mgr = make_manager(tmp_path, monkeypatch, run_worker=crw, cfg=cfg)
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="a", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="b", cwd=str(tmp_path)),
        ]
    )
    ids = [j.job_id for j in created]
    crw.release_for("a")
    assert await _wait_until(lambda: mgr.get(ids[0]).status == "completed")

    start = time.monotonic()
    result = await mgr.wait(ids, timeout_s=None, mode="all", settle_s=1_000_000.0)
    elapsed = time.monotonic() - start

    assert elapsed <= result["max_wait_s"] + 0.3
    assert result["done"] is False
    crw.release_for("b")


async def test_non_terminal_disk_only_job_does_not_start_settle_window(tmp_path, monkeypatch):
    # Regression (poc_settle_disk.py): `first_done = bool(disk_statuses) or ...` treated ANY
    # on-disk job as a completion, including a non-terminal one known only via another
    # process's meta.json (two servers sharing one state dir). That started the settle
    # window immediately, so `wait` returned after `settle_s` with nothing finished.
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    state = tmp_path / "state"
    (state / "jobs").mkdir(parents=True, exist_ok=True)
    crw = ControlledRunWorker()
    mgr = jobs.JobManager(make_cfg(max_wait_s=45.0), state, make_client_factory(), run_worker=crw)

    other = "j-deadbeef"
    d = state / "jobs" / other
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "job_id": other,
                "swarm_id": "s-00000000",
                "spec": {"prompt": "p", "cwd": str(tmp_path)},
                "status": "running",
                "created_at": time.time(),
            }
        )
    )

    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="mine", cwd=str(tmp_path))])
    mine = created[0].job_id  # never released -- stays running

    start = time.monotonic()
    result = await mgr.wait([mine, other], timeout_s=0.6, mode="all", settle_s=0.1)
    elapsed = time.monotonic() - start

    # Nothing is terminal, so this must fall through to the full ~0.6s timeout budget, not
    # return early after the 0.1s settle window.
    assert elapsed >= 0.5
    assert result["done"] is False
    crw.release_for("mine")


# ---------------------------------------------------------------------------
# MCP `wait` tool: plumbing + strict typing
# ---------------------------------------------------------------------------


class FakeJobManagerForServer:
    """Minimal double for the MCP-layer tests: records how `wait` was called."""

    def __init__(self, wait_result: dict[str, Any] | Exception) -> None:
        self.wait_result = wait_result
        self.wait_calls: list[dict[str, Any]] = []

    def redaction_secrets(self) -> list[str]:
        return []

    async def wait(self, job_ids: list[str], timeout_s=None, mode="all", **kwargs: Any):
        self.wait_calls.append({"job_ids": job_ids, "timeout_s": timeout_s, "mode": mode, **kwargs})
        if isinstance(self.wait_result, Exception):
            raise self.wait_result
        return self.wait_result


async def test_mcp_wait_accepts_settle_s_and_forwards_it():
    mgr = FakeJobManagerForServer({"statuses": {}, "unknown": [], "done": True, "settle_s": 0.2})
    wait = server._make_wait(mgr)

    out = await wait(job_ids=["j-aaaaaaaa"], settle_s=0.2)

    assert "error" not in out
    assert mgr.wait_calls == [
        {"job_ids": ["j-aaaaaaaa"], "timeout_s": None, "mode": "all", "settle_s": 0.2}
    ]


async def test_mcp_wait_omits_settle_s_from_manager_call_when_unset():
    """When the caller doesn't pass settle_s, the tool must call manager.wait
    exactly as it did before this feature existed (no settle_s kwarg at all),
    so older JobManager-like doubles without the parameter keep working."""
    mgr = FakeJobManagerForServer({"statuses": {}, "unknown": [], "done": True})
    wait = server._make_wait(mgr)

    out = await wait(job_ids=["j-aaaaaaaa"])

    assert "error" not in out
    assert mgr.wait_calls == [{"job_ids": ["j-aaaaaaaa"], "timeout_s": None, "mode": "all"}]


async def test_mcp_wait_propagates_manager_value_error():
    mgr = FakeJobManagerForServer(ValueError("settle_s must be a non-negative number"))
    wait = server._make_wait(mgr)

    out = await wait(job_ids=["j-aaaaaaaa"], settle_s=1.0)

    assert out == {"error": "settle_s must be a non-negative number"}


def test_mcp_arg_model_rejects_bool_and_numeric_string_settle_s():
    """Mirrors test_task_timeout.py's pin for `timeout_s`: `settle_s` is typed
    `StrictFloat | StrictInt | None`, so pydantic's default lax coercion of a
    JSON `true` into `1.0`, or `"5"` into `5.0`, is rejected at the MCP
    arg-model layer before `JobManager.wait` is ever called.
    """
    from mcp.server.mcpserver.utilities.func_metadata import func_metadata
    from pydantic import ValidationError

    meta = func_metadata(server._make_wait(FakeJobManagerForServer({})))

    with pytest.raises(ValidationError):
        meta.arg_model.model_validate({"job_ids": ["j-aaaaaaaa"], "settle_s": True})
    with pytest.raises(ValidationError):
        meta.arg_model.model_validate({"job_ids": ["j-aaaaaaaa"], "settle_s": "5"})

    parsed = meta.arg_model.model_validate({"job_ids": ["j-aaaaaaaa"], "settle_s": 5})
    assert parsed.settle_s == 5

    # Omitting it entirely still validates, defaulting to None.
    parsed_default = meta.arg_model.model_validate({"job_ids": ["j-aaaaaaaa"]})
    assert parsed_default.settle_s is None
