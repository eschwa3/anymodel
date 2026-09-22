"""Sandboxed Bash must work when the workspace is a worktree under the state dir.

The state dir is on the sandbox's read-deny list (it holds other jobs' transcripts), while
isolated jobs run in <state>/worktrees/<job>. The workspace grant has to win for that subtree
without re-exposing the rest of the state dir.
"""

import subprocess
from pathlib import Path

import pytest

from anymodel_subagents.tools import LocalWorkspace, sandbox
from anymodel_subagents.tools.bash import Bash, BashPolicy
from anymodel_subagents.types import PolicyError
from anymodel_subagents.worktree import create_worktree

pytestmark = pytest.mark.skipif(sandbox.detect() is None, reason="no OS sandbox available")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def test_bash_in_worktree_under_state_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "hello.txt").write_text("hello from repo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    state = tmp_path / "state"
    (state / "jobs" / "j-other000").mkdir(parents=True)
    (state / "jobs" / "j-other000" / "transcript.jsonl").write_text("OTHER-JOB-SECRET\n")

    info = await create_worktree(repo, "j-abcdef12", state)
    policy = BashPolicy(
        allow_prefixes=("cat", "ls", "git status", "git diff"),
        allow_unsandboxed=False,
        state_dir=state,
    )
    bash, ws = Bash(policy), LocalWorkspace(info.workdir)

    out = await bash.run({"command": "cat hello.txt"}, ws)
    assert "hello from repo" in out

    out = await bash.run({"command": "git status"}, ws)
    assert "exit code: 0" in out

    # The allowlist layer's path-escape check (defense in depth, see
    # bash.py's `_check_file_inspection_args`) now refuses this outright,
    # before it would even reach the OS sandbox.
    other = state / "jobs" / "j-other000" / "transcript.jsonl"
    with pytest.raises(PolicyError):
        await bash.run({"command": f"cat {other}"}, ws)
