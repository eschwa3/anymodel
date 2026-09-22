"""Regression tests for tools/bash.py security fixes:

1. Every Bash call (success included) must kill the whole process group AND
   any surviving descendants that escaped it (e.g. a child that called
   setsid with its own session), so a detached child cannot outlive the
   command and race later tool calls / the finalize policy scan.
2. git history exposure: `git show` is removed; `git log` is restricted to
   a bounded, patch-free form; `git diff` may only compare the working
   tree/index against HEAD (optionally with paths inside the workspace).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from anymodel_subagents.config import DEFAULT_BASH_ALLOW
from anymodel_subagents.tools import sandbox
from anymodel_subagents.tools.bash import Bash, BashPolicy, validate_command
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError


def _ws(root: Path) -> LocalWorkspace:
    return LocalWorkspace(root=root)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


_GIT_LOG_OK = [
    "git log",
    "git log --oneline",
    "git log -n 5",
    "git log -5",
    "git log --max-count=5",
    "git log --oneline -n 3",
    "git diff",
    "git diff --stat",
    "git diff --cached",
    "git diff --cached --stat",
    "git diff HEAD",
    "git diff HEAD -- a.txt",
    "git diff -- src/pkg.py",
]

_GIT_BAD = [
    "git show",
    "git show HEAD",
    "git show HEAD:secrets.txt",
    "git log -p",
    "git log -u",
    "git log --patch",
    "git log --all",
    "git log --branches",
    "git log --remotes",
    "git log --tags",
    "git log -G secret",
    "git log -S secret",
    "git log --follow",
    "git log --reflog",
    "git log --walk-reflogs",
    "git log --format=%H",
    "git log --pretty=fuller",
    "git log HEAD~3",
    "git log main",
    "git log -- somefile.txt",
    "git diff HEAD~1",
    "git diff main..HEAD",
    "git diff --cached HEAD~2",
    "git diff -p",
    "git diff -u",
    "git diff other-file.txt",
]


@pytest.mark.parametrize("command", _GIT_LOG_OK)
def test_bounded_git_forms_accepted(command: str) -> None:
    validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


@pytest.mark.parametrize("command", _GIT_BAD)
def test_history_exposing_git_forms_rejected(command: str) -> None:
    with pytest.raises(PolicyError):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)
    with pytest.raises(PolicyError):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=False)


def test_git_diff_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        validate_command(
            "git diff -- /etc/passwd", DEFAULT_BASH_ALLOW, sandboxed=True, workspace=tmp_path
        )
    with pytest.raises(PolicyError):
        validate_command(
            "git diff HEAD -- ../secrets.txt",
            DEFAULT_BASH_ALLOW,
            sandboxed=True,
            workspace=tmp_path,
        )


def test_tool_description_states_git_truth(state_dir: Path, tmp_path: Path) -> None:
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )
    desc = bash.schema["function"]["description"]
    assert "git show" in desc  # names the removed form so the model doesn't probe it
    assert "git log" in desc and "--oneline" in desc
    assert "--patch" not in desc or "no" in desc  # patch forms are refused, not offered


# --------------------------------------------------------------------------- fix 1: post-run descendant kill


def _bash(state_dir: Path) -> Bash:
    allow = (*DEFAULT_BASH_ALLOW, "python3")
    return Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))


@pytest.mark.parametrize("sleep_mode", ["short", "forever"])
async def test_bash_kills_detached_child_after_run(
    tmp_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch, sleep_mode: str
) -> None:
    """A command that spawns a setsid-detached child (own session, own fds)
    appending to a beacon file must have that child killed by the time
    `run()` returns -- on success and on timeout alike.
    """
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = _bash(state_dir)
    beacon = tmp_path / "beacon.txt"
    beacon.touch()
    child = tmp_path / "child.py"
    child.write_text(
        "import time\n"
        "while True:\n"
        f"    open({str(beacon)!r}, 'a').write('x')\n"
        "    time.sleep(0.2)\n"
    )
    script = tmp_path / "spawner.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}], start_new_session=True,\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + ("print('parent done')\n" if sleep_mode == "short" else "time.sleep(600)\n")
    )
    args = {"command": f"python3 {script.name}"}
    if sleep_mode == "forever":
        args["timeout"] = 2
    out = await bash.run(args, ws=_ws(tmp_path))
    assert "exit code" in out

    # Give any surviving detached child time to write more beacons.
    await asyncio.sleep(1.0)
    size_then = beacon.stat().st_size if beacon.exists() else 0
    await asyncio.sleep(1.0)
    size_now = beacon.stat().st_size if beacon.exists() else 0
    assert size_now == size_then, "detached child kept writing after the Bash call returned"
