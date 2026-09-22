"""Regression tests for two cancellation/reaping gaps in worktree.py:

1. `create_worktree` cancelled while `git worktree add` is in flight used to
   orphan the worktree dir, the `anymodel/<job_id>` branch and the
   `.git/worktrees` admin entry in the caller's repo: the to_thread git call
   runs to completion despite the cancellation, but `create_worktree` never
   returns and the meta file that lets `sweep()` reap the leftovers is only
   written after the add.

2. `sweep()` only iterated `.*.meta.json` files, so a meta-less orphan
   directory (e.g. from (1) on a killed process) was never reaped.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from anymodel_subagents import worktree as worktree_mod
from anymodel_subagents.worktree import create_worktree, sweep


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# (a) cancel create_worktree while `git worktree add` is in flight
# ---------------------------------------------------------------------------


async def test_cancelled_create_worktree_leaves_no_dir_branch_or_admin_entry(
    repo: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_blocking = worktree_mod._run_git_blocking
    add_started = threading.Event()
    add_done = threading.Event()

    def slow_add_only(argv: list[str], env: dict[str, str], timeout: float):
        # Only the `worktree add` call is slowed and signalled: the task must
        # be parked on it (an abandoned-to-thread git call) when we cancel,
        # and `add_done` must mean the add itself finished, not some earlier
        # git call.
        if "worktree" in argv and "add" in argv:
            add_started.set()
            time.sleep(0.3)
            try:
                return real_blocking(argv, env, timeout)
            finally:
                add_done.set()
        return real_blocking(argv, env, timeout)

    monkeypatch.setattr(worktree_mod, "_run_git_blocking", slow_add_only)

    task = asyncio.create_task(create_worktree(repo, "job-cancel1", state))
    for _ in range(500):
        if add_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert add_started.is_set(), "worktree add never started"
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The abandoned-to-thread add still runs to completion: wait for it so
    # the assertions below observe what it actually created (or what the
    # cleanup removed), not a snapshot taken while git was still sleeping.
    for _ in range(500):
        if add_done.is_set():
            break
        await asyncio.sleep(0.01)
    assert add_done.is_set(), "worktree add never finished"

    # the worktree dir (and anything beside it) must be gone again
    assert not (state / "worktrees" / "job-cancel1").exists()
    # the anymodel/<job_id> branch must be gone
    assert _git("branch", "--list", "anymodel/*", cwd=repo).strip() == ""
    # only the main tree is left -- no stale .git/worktrees admin entry
    listed = [
        line
        for line in _git("worktree", "list", "--porcelain", cwd=repo).splitlines()
        if line.startswith("worktree ")
    ]
    assert len(listed) == 1


# ---------------------------------------------------------------------------
# (b) sweep() reaps meta-less orphan directories
# ---------------------------------------------------------------------------


async def test_sweep_reaps_metaless_orphan_dir_with_zero_retention(repo: Path, state: Path) -> None:
    worktrees = state / "worktrees"
    worktrees.mkdir(parents=True)
    orphan = worktrees / "job-orphan1"
    orphan.mkdir()
    (orphan / "stray.txt").write_text("left behind by a cancelled add\n")

    await sweep(state, retention_days=0)

    assert not orphan.exists()


async def test_sweep_respects_retention_for_metaless_orphan_dirs(repo: Path, state: Path) -> None:
    worktrees = state / "worktrees"
    worktrees.mkdir(parents=True)
    old = worktrees / "job-oldorphan"
    old.mkdir()
    _age(old, 30)
    fresh = worktrees / "job-freshorphan"
    fresh.mkdir()

    await sweep(state, retention_days=7)

    assert not old.exists()
    assert fresh.exists()


async def test_sweep_spares_metaless_entries_that_are_not_job_dirs(
    repo: Path, state: Path, tmp_path: Path
) -> None:
    worktrees = state / "worktrees"
    worktrees.mkdir(parents=True)

    # name fails the job-id pattern -> never touched
    not_a_job = worktrees / "Not_A_Job"
    not_a_job.mkdir()
    (not_a_job / "keep.txt").write_text("x")

    # a symlinked entry must be skipped, never followed or removed
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x")
    link = worktrees / "job-symlink1"
    link.symlink_to(outside)

    await sweep(state, retention_days=0)

    assert not_a_job.exists()
    assert link.is_symlink()
    assert (outside / "precious.txt").exists()
