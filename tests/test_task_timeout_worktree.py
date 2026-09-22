"""Regression test: a per-task `timeout_s` (PoC 4 in the timeout-review) firing
mid-Bash on a real `edit+bash` worktree job.

Real `JobManager`, real `engine.run_worker`, real git worktree, real OS
sandbox; only the OpenRouter client is faked (never make real network calls
from tests). The fake worker writes a Makefile (allowlisted `make test`)
whose recipe plants a write-denied file, a symlink out of the workspace, a
legitimate file, and a backgrounded child that would write a marker a few
seconds later -- then blocks, so the job's short wall clock expires mid-run.

Asserts: status == "timeout", worktree finalize still ran the policy scan
(the denied file and the symlink are reverted, the legitimate change is
committed), and no sandboxed process is left alive once finalize is done.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from anymodel_subagents import jobs
from anymodel_subagents.config import DEFAULT_BASH_ALLOW, Config
from anymodel_subagents.openrouter import ChatResult
from anymodel_subagents.tools import sandbox
from anymodel_subagents.types import Usage

pytestmark = pytest.mark.skipif(sandbox.detect() is None, reason="no OS sandbox available")

MARKER = "timeoutwt-orphan-marker"

MAKEFILE = (
    "test:\n"
    "\t@mkdir -p .github/workflows\n"
    "\t@printf 'on: push\\n' > .github/workflows/evil.yml\n"
    "\t@ln -sfn /etc/passwd leaked_link\n"
    "\t@printf 'partial work\\n' > legit.txt\n"
    f"\t@sh -c 'sleep 2; printf orphan > orphan_marker.txt' &  echo started-{MARKER}\n"
    f"\t@sleep 60 # {MARKER}\n"
)


class FakeClient:
    def __init__(self) -> None:
        self.turn = 0

    def redaction_secrets(self) -> list[str]:
        return ["sk-fake-not-a-real-key"]

    async def chat_completions(
        self, *, model: str, messages: list[Any], tools: Any = None, **kw: Any
    ) -> ChatResult:
        self.turn += 1
        if self.turn == 1:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "Write",
                            "arguments": json.dumps({"file_path": "Makefile", "content": MAKEFILE}),
                        },
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": json.dumps({"command": "make test", "timeout": 120}),
                        },
                    },
                ],
            }
            return ChatResult(message=msg, usage=Usage(cost=0.0), finish_reason="tool_calls")
        return ChatResult(
            message={"role": "assistant", "content": "done"},
            usage=Usage(cost=0.0),
            finish_reason="stop",
        )


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    (root / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


def _marker_procs() -> list[str]:
    out = subprocess.run(
        ["pgrep", "-fl", MARKER], capture_output=True, text=True, check=False
    ).stdout
    return [ln for ln in out.splitlines() if "pgrep" not in ln]


async def _wait_terminal(mgr: jobs.JobManager, job_id: str, deadline_s: float = 60.0) -> None:
    start = time.monotonic()
    while mgr.get(job_id).status not in jobs._TERMINAL_STATUSES:
        if time.monotonic() - start > deadline_s:
            pytest.fail("job never reached a terminal status")
        await asyncio.sleep(0.1)


async def test_task_timeout_mid_bash_reverts_policy_and_kills_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    state = tmp_path / "state"
    state.mkdir()

    # A short task timeout is below jobs.py's normal floor -- lower it, the
    # same way test_task_timeout.py's own real-timeout test does, to keep
    # this test fast without touching the floor for anything else.
    monkeypatch.setattr(jobs, "_MIN_TIMEOUT_S", 1.0)

    cfg = Config(
        default_model="test/model",
        max_concurrency=1,
        max_turns=3,
        timeout_s=120.0,  # config ceiling stays high; the TASK shortens it
        max_tasks_per_dispatch=5,
        allowed_roots=(),
        job_retention_days=7,
        bash_allow=(*DEFAULT_BASH_ALLOW, "make test"),
    )
    mgr = jobs.JobManager(cfg, state, FakeClient)

    try:
        _swarm_id, created = await mgr.dispatch(
            [
                jobs.TaskSpec(
                    prompt="do the thing",
                    cwd=str(repo),
                    mode="edit+bash",
                    timeout_s=1.5,
                )
            ]
        )
        job_id = created[0].job_id
        assert created[0].spec.timeout_s == 1.5

        await _wait_terminal(mgr, job_id)
        job = mgr.get(job_id)
        assert job.status == "timeout"
        assert job.result.status == "timeout"

        out = job.worktree_outcome
        wt = Path(job.worktree_info.path)

        # partial work committed, policy-denied path and new symlink reverted
        assert out.commit is not None
        assert "legit.txt" in out.changed_files
        assert any(".github/workflows/evil.yml" in p for p in out.policy_reverted_files)
        assert any("leaked_link" in p for p in out.policy_reverted_files)
        if wt.exists():
            assert not (wt / ".github/workflows/evil.yml").exists()
            assert not (wt / "leaked_link").exists()
            assert (wt / "legit.txt").exists()

        meta = json.loads((state / "jobs" / job_id / "meta.json").read_text())
        assert meta["spec"]["timeout_s"] == 1.5

        # Give the backgrounded child (which would fire ~2s after `make test`
        # started) time to either survive or not, then confirm it's gone --
        # no sandboxed process left alive once finalize is done.
        deadline = time.monotonic() + 10.0
        while _marker_procs() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert _marker_procs() == []
        if wt.exists():
            assert not (wt / "orphan_marker.txt").exists()
    finally:
        await mgr.shutdown()
