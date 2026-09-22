"""Regression tests for the cross-version cancellation hang fixed in
worktree.py's `_run_git` and tools/bash.py's `Bash._exec`.

Root cause: on CPython 3.11 and 3.12, cancelling an asyncio task while it is
inside `asyncio.create_subprocess_exec(...)` (specifically, while the event
loop is still constructing the subprocess transport) hangs forever -- fixed
upstream in 3.13. Both call sites now run a *blocking* `subprocess`
invocation in a worker thread via `asyncio.to_thread` instead: cancelling the
awaiting task there just abandons the thread (which runs to completion, or
until we explicitly kill it) without ever wedging the event loop, on any
Python version.

These tests exercise the fix directly (not by reproducing the original hang,
which would defeat the point -- they'd hang too, on 3.11/3.12, without it).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anymodel_subagents import worktree as worktree_mod
from anymodel_subagents.config import DEFAULT_BASH_ALLOW
from anymodel_subagents.tools import bash as bash_mod
from anymodel_subagents.tools import sandbox
from anymodel_subagents.tools.bash import Bash, BashPolicy
from anymodel_subagents.tools.workspace import LocalWorkspace

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix process-group semantics")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


# --------------------------------------------------------------------------- (a) worktree._run_git


async def test_cancel_mid_run_git_returns_promptly_and_leaves_no_pending_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    real_blocking = worktree_mod._run_git_blocking

    def slow_blocking(argv: list[str], env: dict[str, str], timeout: float):
        # Simulate a git call slow enough that the task is still awaiting the
        # thread (not already finished) at the moment we cancel it below.
        time.sleep(2.0)
        return real_blocking(argv, env, timeout)

    monkeypatch.setattr(worktree_mod, "_run_git_blocking", slow_blocking)

    task = asyncio.create_task(worktree_mod._run_git(["-C", str(repo), "status"]))
    await asyncio.sleep(0.2)  # let the task start and enter the (slowed) blocking call
    assert not task.done()

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"cancellation took {elapsed:.2f}s -- looks hung"

    # No stray asyncio tasks left behind by the cancellation (the abandoned
    # worker thread is not an asyncio task and isn't tracked here).
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert pending == []


async def test_cancel_mid_run_git_worktree_creation_returns_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same hazard, exercised through the real trigger from the task brief:
    `create_worktree` -> `snapshot`-style `_run_git` calls, cancelled while a
    git subprocess is being created/run.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = tmp_path / "state"

    real_blocking = worktree_mod._run_git_blocking

    def slow_blocking(argv: list[str], env: dict[str, str], timeout: float):
        time.sleep(1.5)
        return real_blocking(argv, env, timeout)

    monkeypatch.setattr(worktree_mod, "_run_git_blocking", slow_blocking)

    task = asyncio.create_task(worktree_mod.create_worktree(repo, "job-cancelt", state))
    await asyncio.sleep(0.1)
    assert not task.done()

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - start < 2.0


# --------------------------------------------------------------------------- (b) Bash: cancel mid-command


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


async def test_cancel_mid_bash_command_kills_process_group(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)  # unsandboxed: no seatbelt needed

    created: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(bash_mod.subprocess, "Popen", capturing_popen)

    allow = (*DEFAULT_BASH_ALLOW, "sleep")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))

    task = asyncio.create_task(bash.run({"command": "sleep 30", "timeout": 60}, ws))
    await asyncio.sleep(0.3)  # let the process actually start
    assert created, "the sandboxed command never started"
    assert not task.done()

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"cancellation took {elapsed:.2f}s -- looks hung"

    pid = created[-1].pid
    # start_new_session=True makes the child its own process-group leader
    # (pgid == pid); signalling that pgid must now find nothing alive.
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


async def test_cancel_mid_bash_command_kills_grandchild_too(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, shlex.quote(sys.executable))
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))

    marker = ws.root / "grandchild-alive.txt"
    script = ws.root / "grandchild_spawner.py"
    script.write_text(
        "import subprocess, time\n"
        "subprocess.Popen(['/bin/sh', '-c', "
        f"'while true; do date +%s > {marker} ; sleep 0.1; done'])\n"
        "time.sleep(30)\n"
    )

    task = asyncio.create_task(
        bash.run(
            {"command": f"{shlex.quote(sys.executable)} grandchild_spawner.py", "timeout": 60}, ws
        )
    )
    assert await _wait_until(lambda: marker.exists(), timeout=5.0)

    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - start < 2.0

    await asyncio.sleep(0.5)
    first_seen = marker.read_text()
    await asyncio.sleep(0.5)
    second_seen = marker.read_text()
    assert second_seen == first_seen, "grandchild kept running after cancellation"


# --------------------------------------------------------------------------- (c) timeout still kills grandchildren


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    start = time.monotonic()
    while True:
        if predicate():
            return True
        if time.monotonic() - start > timeout:
            return False
        await asyncio.sleep(interval)


async def test_timeout_still_kills_process_group_and_grandchildren(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, shlex.quote(sys.executable))
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))

    marker = ws.root / "grandchild-alive.txt"
    script = ws.root / "grandchild_spawner.py"
    script.write_text(
        "import subprocess, time\n"
        "subprocess.Popen(['/bin/sh', '-c', "
        f"'while true; do date +%s > {marker} ; sleep 0.1; done'])\n"
        "time.sleep(30)\n"
    )

    out = await bash.run(
        {"command": f"{shlex.quote(sys.executable)} grandchild_spawner.py", "timeout": 1}, ws
    )
    assert "timed out" in out

    await asyncio.sleep(0.5)
    if marker.exists():
        first_seen = marker.read_text()
        await asyncio.sleep(0.5)
        second_seen = marker.read_text() if marker.exists() else None
        assert second_seen == first_seen, "grandchild kept running after timeout"


# --------------------------------------------------------------------------- jobs.JobManager.shutdown()


async def test_shutdown_is_cooperative_then_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shutdown() must never await forever: a job that ignores its
    cancel_event for longer than the grace period gets task.cancel()ed
    instead of hanging shutdown indefinitely.
    """
    from anymodel_subagents import jobs
    from anymodel_subagents.config import Config
    from anymodel_subagents.types import Usage, WorkerResult

    def fake_validate_cwd(cwd: str, cfg: Config) -> Path:
        return Path(cwd)

    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    monkeypatch.setattr(jobs, "_SHUTDOWN_GRACE_S", 0.3)

    async def stubborn_run_worker(**kwargs):
        # Deliberately ignores cancel_event for longer than the grace period;
        # only reacts to an actual task.cancel(), like any real coroutine
        # whose own awaits (not a subprocess call) don't check it every tick.
        for _ in range(100):
            await asyncio.sleep(0.05)
        return WorkerResult(
            status="completed", final_message="", model="test/model", turns=1, usage=Usage()
        )

    class FakeClient:
        def redaction_secrets(self):
            return []

    cfg = Config(
        default_model="test/model",
        max_concurrency=2,
        max_turns=5,
        timeout_s=30.0,
        max_tasks_per_dispatch=5,
        allowed_roots=(),
        job_retention_days=7,
    )
    state = tmp_path / "state"
    state.mkdir()
    mgr = jobs.JobManager(cfg, state, lambda: FakeClient(), run_worker=stubborn_run_worker)

    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path))])
    job_id = created[0].job_id

    async def _wait_running():
        for _ in range(200):
            if mgr.get(job_id).status == "running":
                return True
            await asyncio.sleep(0.01)
        return False

    assert await _wait_running()

    start = time.monotonic()
    # The outer wait_for is a belt-and-braces bound for the test itself; the
    # real assertion is `elapsed` below -- shutdown() must return well inside
    # it on its own, never by being externally cancelled here.
    await asyncio.wait_for(mgr.shutdown(), timeout=5.0)
    elapsed = time.monotonic() - start

    # Bounded by grace period + cancellation unwind, nowhere near "forever":
    # the stubborn job ignores cancel_event for the whole ~5s it would take
    # to finish its loop, so shutdown() only returns this quickly because it
    # escalated to task.cancel() after the (shortened) grace period.
    assert elapsed < 3.0
    # The job's own task has been fully unwound (cancelled or otherwise
    # finished) -- shutdown() never leaves a task behind mid-flight.
    task = mgr._tasks.get(job_id)
    assert task is not None
    assert task.done()
