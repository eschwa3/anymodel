"""Regression test: `sandbox.wrap` must run off the asyncio event loop.

`Bash.run` used to call `sandbox.wrap` synchronously on the loop; on Linux
the bwrap profile's secret-file walk over the workspace stalls the event
loop for the whole walk (measured ~514 ms with 20k files), blocking every
concurrent MCP call and running job.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from anymodel_subagents.config import DEFAULT_BASH_ALLOW
from anymodel_subagents.tools import sandbox
from anymodel_subagents.tools.bash import Bash, BashPolicy
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


def _bash(state_dir: Path) -> Bash:
    return Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )


async def test_sandbox_wrap_runs_off_the_event_loop(
    tmp_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sandbox.wrap` (including its workspace walk) must not run on the loop thread."""
    on_main_thread: list[bool] = []
    seen_kwargs: dict[str, Any] = {}

    def fake_wrap(argv: list[str], **kwargs: Any) -> list[str]:
        on_main_thread.append(threading.current_thread() is threading.main_thread())
        seen_kwargs.update(kwargs)
        return ["/bin/echo", "ok"]

    monkeypatch.setattr(sandbox, "detect", lambda: "seatbelt")
    monkeypatch.setattr(sandbox, "wrap", fake_wrap)

    out = await _bash(state_dir).run({"command": "echo hi"}, ws=LocalWorkspace(root=tmp_path))

    assert "exit code: 0" in out and "ok" in out
    assert seen_kwargs["kind"] == "seatbelt"
    assert seen_kwargs["workspace"] == tmp_path
    assert on_main_thread == [False], "sandbox.wrap ran on the event-loop thread"


async def test_sandbox_detect_runs_off_the_event_loop(
    tmp_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sandbox.detect()` must not run on the loop thread either.

    It spawns a sandbox-exec/bwrap probe subprocess (up to seconds), and it
    shares a thread with the deny/extra-read + venv helpers that feed
    `sandbox.wrap`'s profile.
    """
    detect_on_main_thread: list[bool] = []

    def recording_detect() -> Any:
        detect_on_main_thread.append(threading.current_thread() is threading.main_thread())
        return "seatbelt"

    monkeypatch.setattr(sandbox, "detect", recording_detect)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, **kwargs: ["/bin/echo", "ok"])

    out = await _bash(state_dir).run({"command": "echo hi"}, ws=LocalWorkspace(root=tmp_path))

    assert "exit code: 0" in out and "ok" in out
    assert detect_on_main_thread == [False], "sandbox.detect ran on the event-loop thread"


async def test_wrap_policy_error_still_propagates(
    tmp_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PolicyError raised by `wrap` (now off the loop) still surfaces from run()."""

    def refusing_wrap(argv: list[str], **kwargs: Any) -> list[str]:
        raise PolicyError("no supported sandbox mechanism")

    monkeypatch.setattr(sandbox, "detect", lambda: "seatbelt")
    monkeypatch.setattr(sandbox, "wrap", refusing_wrap)

    with pytest.raises(PolicyError, match="no supported sandbox mechanism"):
        await _bash(state_dir).run({"command": "echo hi"}, ws=LocalWorkspace(root=tmp_path))
