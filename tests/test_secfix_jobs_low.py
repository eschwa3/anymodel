"""Regression tests for JobManager.dispatch (fix A).

Fix A: `dispatch`'s task validation must run its blocking git subprocesses
(`config.validate_cwd`, `jobs._git_toplevel`) off the event loop via
`asyncio.to_thread`, and the `max_live_jobs` check-and-register must be
atomic -- no awaits between the check and the registrations, so two
concurrent dispatches can never exceed the cap.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from anymodel_subagents import jobs
from anymodel_subagents.config import Config
from anymodel_subagents.types import Usage, WorkerResult


def fake_validate_cwd(cwd: str, cfg: Config) -> Path:
    p = Path(cwd)
    if not p.is_absolute():
        raise ValueError("cwd must be an absolute path")
    return p


async def immediate_run_worker(**kwargs: Any) -> WorkerResult:
    return WorkerResult(
        status="completed", final_message="done", model=kwargs["model"], turns=1, usage=Usage()
    )


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


def make_manager(tmp_path, monkeypatch, *, cfg=None, run_worker=immediate_run_worker):
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return jobs.JobManager(cfg or make_cfg(), state, lambda: object(), run_worker=run_worker)


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def blocking_run_worker(**kwargs: Any) -> WorkerResult:
    """Blocks until cancelled, so dispatched jobs stay live for the whole test."""
    while not kwargs["cancel_event"].is_set():
        await asyncio.sleep(0.005)
    return WorkerResult(
        status="cancelled", final_message="cancelled", model=kwargs["model"], turns=0, usage=Usage()
    )


async def test_dispatch_validation_runs_off_event_loop_thread(tmp_path, monkeypatch):
    seen: list[threading.Thread] = []

    def recording_validate_cwd(cwd: str, cfg: Config) -> Path:
        seen.append(threading.current_thread())
        return fake_validate_cwd(cwd, cfg)

    def recording_git_toplevel(path: Path):
        seen.append(threading.current_thread())
        return tmp_path

    mgr = make_manager(tmp_path, monkeypatch)
    # Patched after make_manager, which installs its own validate_cwd fake.
    monkeypatch.setattr(jobs, "validate_cwd", recording_validate_cwd)
    monkeypatch.setattr(jobs, "_git_toplevel", recording_git_toplevel)

    _swarm, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="ro", cwd=str(tmp_path)),
            jobs.TaskSpec(prompt="ed", cwd=str(tmp_path), mode="edit", isolation="none"),
        ]
    )

    # validate_cwd once per task + _git_toplevel once for the edit task.
    assert len(seen) == 3
    assert all(t is not threading.main_thread() for t in seen)
    assert await _wait_until(lambda: all(mgr.get(j.job_id).status == "completed" for j in created))


def patch_fake_worktrees(tmp_path, monkeypatch):
    """Replace jobs' worktree functions with fakes that really yield to the loop."""
    from anymodel_subagents.worktree import WorktreeInfo, WorktreeOutcome

    async def create_worktree(cwd: Path, job_id: str, state: Path) -> WorktreeInfo:
        await asyncio.sleep(0)  # a real yield point, like the git-backed original
        wt = tmp_path / "worktrees" / job_id
        wt.mkdir(parents=True, exist_ok=True)
        return WorktreeInfo(
            repo_root=cwd, path=wt, workdir=wt, branch=f"anymodel/{job_id}", base_commit="0" * 40
        )

    async def finalize_worktree(info, message, *, is_write_denied=None, is_sensitive=None):
        await asyncio.sleep(0)
        return WorktreeOutcome()

    monkeypatch.setattr(jobs, "create_worktree", create_worktree)
    monkeypatch.setattr(jobs, "finalize_worktree", finalize_worktree)


async def test_concurrent_dispatches_cannot_exceed_max_live_jobs(tmp_path, monkeypatch):
    mgr = make_manager(
        tmp_path, monkeypatch, cfg=make_cfg(max_live_jobs=2), run_worker=blocking_run_worker
    )
    patch_fake_worktrees(tmp_path, monkeypatch)

    async def dispatch(n, tag):
        return await mgr.dispatch(
            [
                jobs.TaskSpec(
                    prompt=f"{tag}{i}", cwd=str(tmp_path), mode="edit", isolation="worktree"
                )
                for i in range(n)
            ]
        )

    results = await asyncio.gather(dispatch(2, "a"), dispatch(1, "b"), return_exceptions=True)

    created = [j for r in results if not isinstance(r, BaseException) for j in r[1]]
    errors = [r for r in results if isinstance(r, ValueError)]
    assert len(errors) == 1
    assert "too many live jobs" in str(errors[0])
    # Together the two concurrent dispatches never brought more than the cap
    # of live jobs into existence (3 would mean one dispatch slipped past the
    # check while the other was still awaiting between check and registration).
    # Which dispatch wins is scheduler-dependent; the cap is not.
    assert 1 <= len(created) <= 2
    job_ids = [j.job_id for j in created]
    assert await _wait_until(lambda: all(mgr.get(jid).status == "running" for jid in job_ids))
    await mgr.shutdown()  # unblocks the run_worker fakes; jobs wind down cooperatively


# ---------------------------------------------------------------------------
# fix B: non-integral max_turns values are rejected with the clean ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), True])
async def test_max_turns_rejects_non_integer_values(tmp_path, monkeypatch, bad):
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="max_turns must be an integer"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path), max_turns=bad)])


async def test_refused_dispatch_removes_the_worktrees_it_prepared(tmp_path, monkeypatch):
    # Worktrees are prepared before the await-free cap re-check; when a concurrent dispatch
    # fills the cap meanwhile, the late refusal must not leak them.
    mgr = make_manager(
        tmp_path, monkeypatch, cfg=make_cfg(max_live_jobs=2), run_worker=blocking_run_worker
    )
    removed: list[Any] = []

    async def create(cwd, job_id, state):
        # Simulates another dispatch registering a live job while this one prepares.
        mgr._jobs["j-0ther001"] = jobs.Job(
            job_id="j-0ther001", swarm_id="s-x", spec=jobs.TaskSpec(prompt="x", cwd=str(cwd))
        )
        return f"info-{job_id}"

    async def fake_remove(info):
        removed.append(info)

    monkeypatch.setattr(jobs, "create_worktree", create)
    monkeypatch.setattr(jobs, "remove_worktree", fake_remove)
    specs = [
        jobs.TaskSpec(prompt=f"p{i}", cwd=str(tmp_path), mode="edit", isolation="worktree")
        for i in range(2)
    ]

    with pytest.raises(ValueError, match="too many live jobs"):
        await mgr.dispatch(specs)

    assert await _wait_until(lambda: len(removed) == 2)


def test_meta_and_report_are_not_written_through_a_symlinked_job_dir(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    jobs_dir = tmp_path / "state" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-0badc0de").symlink_to(outside, target_is_directory=True)
    job = jobs.Job(
        job_id="j-0badc0de",
        swarm_id="s-1",
        spec=jobs.TaskSpec(prompt="p", cwd=str(tmp_path)),
    )

    mgr._write_meta(job)

    assert list(outside.iterdir()) == []


async def test_dispatch_that_cannot_fit_creates_no_worktrees(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(max_live_jobs=1))
    created: list[str] = []

    async def counting_create(cwd, job_id, state):
        created.append(job_id)
        raise AssertionError("no worktree may be created for a dispatch that cannot fit")

    monkeypatch.setattr(jobs, "create_worktree", counting_create)
    specs = [
        jobs.TaskSpec(prompt=f"p{i}", cwd=str(tmp_path), mode="edit", isolation="worktree")
        for i in range(3)
    ]
    with pytest.raises(ValueError, match="too many live jobs"):
        await mgr.dispatch(specs)
    assert created == []


async def test_cancelled_dispatch_removes_worktrees_already_prepared(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(max_live_jobs=5))
    removed: list[Any] = []
    calls = 0

    async def create(cwd, job_id, state):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return f"info-{job_id}"

    async def fake_remove(info):
        removed.append(info)

    monkeypatch.setattr(jobs, "create_worktree", create)
    monkeypatch.setattr(jobs, "remove_worktree", fake_remove)
    specs = [
        jobs.TaskSpec(prompt=f"p{i}", cwd=str(tmp_path), mode="edit", isolation="worktree")
        for i in range(3)
    ]
    with pytest.raises(asyncio.CancelledError):
        await mgr.dispatch(specs)
    await mgr.shutdown()  # drains the background removals
    assert len(removed) == 1


async def test_failed_background_worktree_removal_is_logged(tmp_path, monkeypatch, caplog):
    mgr = make_manager(tmp_path, monkeypatch)

    async def failing_remove(info):
        raise RuntimeError("disk says no")

    monkeypatch.setattr(jobs, "remove_worktree", failing_remove)
    prepared = [
        jobs._PreparedJob(
            job_id="j-00000001",
            spec=jobs.TaskSpec(prompt="p", cwd=str(tmp_path)),
            cwd_path=tmp_path,
            note=None,
            worktree_info="info",
            setup_error=None,
        )
    ]
    with caplog.at_level("WARNING", logger="anymodel_subagents.jobs"):
        mgr._discard_prepared(prepared)
        await mgr.shutdown()
    assert "could not remove an unused worktree" in caplog.text
