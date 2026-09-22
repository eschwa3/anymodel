"""Real escape tests for the Linux bubblewrap sandbox in tools/sandbox.py.

Linux only: this module skips itself entirely anywhere else (including the
macOS dev machine, where `sandbox.detect()` returns "seatbelt"). On Linux, set
`ANYMODEL_REQUIRE_BWRAP=1` (CI) to turn "bwrap is missing or non-working" into
a FAILURE instead of a silent skip -- a green run then really means the
sandbox was exercised.

Every negative test carries its own positive control in the same test (an
`echo` that must work, plus a write inside the workspace that must land on the
host), so a test can never pass merely because the command failed to start or
because the sandbox is broken in the deny direction.

Probes are `/bin/sh -c` plus coreutils on purpose: the interpreter running
pytest may live outside the paths bwrap binds (e.g. a uv-managed Python under
a home directory that is not in `_BWRAP_HOME_RELATIVE_DIRS`), so `sys.executable`
is not a reliable thing to run inside the sandbox.

`build_bwrap_argv` never adds `--clearenv`; env scrubbing is the caller's job
(`bash.py:_build_env` builds the environment from nothing and passes it to
`subprocess`), so there is deliberately no "host env is not inherited" test
here -- it would be testing `bash.py`, not the sandbox.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from anymodel_subagents.tools import sandbox

if sys.platform != "linux":
    pytest.skip("Linux-only: exercises the real bubblewrap sandbox", allow_module_level=True)

_REQUIRE_BWRAP = os.environ.get("ANYMODEL_REQUIRE_BWRAP") == "1"
_HAS_BWRAP = sandbox.detect() == "bwrap"


@pytest.fixture
def bwrap() -> None:
    """Fail (not skip) when the environment promised a working bwrap and it is gone."""
    if _HAS_BWRAP:
        return
    if _REQUIRE_BWRAP:
        pytest.fail(
            "ANYMODEL_REQUIRE_BWRAP=1 but sandbox.detect() != 'bwrap': bwrap is missing "
            "or non-working on this machine"
        )
    pytest.skip("no working bwrap on this machine")


@pytest.fixture
def sandboxed_dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return workspace, tmp


def _run(
    argv: list[str],
    *,
    workspace: Path,
    tmp: Path,
    deny_read: list[Path] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    wrapped = sandbox.wrap(
        list(argv),
        workspace=workspace,
        tmp=tmp,
        deny_read=list(deny_read or []),
        kind="bwrap",
    )
    return subprocess.run(
        wrapped, capture_output=True, text=True, timeout=timeout, check=False, cwd=str(workspace)
    )


def _sh(
    script: str,
    *,
    workspace: Path,
    tmp: Path,
    deny_read: list[Path] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["/bin/sh", "-c", script],
        workspace=workspace,
        tmp=tmp,
        deny_read=deny_read,
        timeout=timeout,
    )


def _assert_control(*, workspace: Path, tmp: Path) -> None:
    """The positive control every negative test needs: the sandboxed shell runs,
    and a write inside the workspace (and the scratch tmp) really lands on the host."""
    echo = _sh("echo ok", workspace=workspace, tmp=tmp)
    assert echo.returncode == 0, echo.stderr
    assert "ok" in echo.stdout

    ws_file = workspace / "control-ws.txt"
    written = _sh(f"echo ws > {shlex.quote(str(ws_file))}", workspace=workspace, tmp=tmp)
    assert written.returncode == 0, written.stderr
    assert ws_file.read_text().strip() == "ws"

    tmp_file = tmp / "control-tmp.txt"
    written = _sh(f"echo tm > {shlex.quote(str(tmp_file))}", workspace=workspace, tmp=tmp)
    assert written.returncode == 0, written.stderr
    assert tmp_file.read_text().strip() == "tm"


def _interfaces(proc_net_dev: str) -> set[str]:
    names = set()
    for line in proc_net_dev.splitlines():
        if ":" not in line or line.startswith(("Inter-", " face")):
            continue
        names.add(line.partition(":")[0].strip())
    return names


def test_bwrap_is_available_when_required() -> None:
    if not _REQUIRE_BWRAP:
        pytest.skip("set ANYMODEL_REQUIRE_BWRAP=1 to require a working bwrap")
    assert sandbox.detect() == "bwrap", (
        "ANYMODEL_REQUIRE_BWRAP=1 but sandbox.detect() != 'bwrap': bwrap is missing or non-working"
    )


def test_control_echo_and_writes_inside_workspace_and_tmp(
    sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    workspace, tmp = sandboxed_dirs
    _assert_control(workspace=workspace, tmp=tmp)


def test_write_outside_workspace_is_denied(
    tmp_path: Path, sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    workspace, tmp = sandboxed_dirs
    _assert_control(workspace=workspace, tmp=tmp)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "pwned.txt"
    result = _sh(f"echo pwned > {shlex.quote(str(outside))}", workspace=workspace, tmp=tmp)
    assert result.returncode != 0
    assert not outside.exists()


def test_read_of_deny_read_path_is_denied(
    tmp_path: Path, sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    workspace, tmp = sandboxed_dirs
    secret_dir = tmp_path / "secretdir"
    secret_dir.mkdir()
    secret = secret_dir / "id_rsa"
    secret.write_text("SUPER-SECRET-DECOY")
    _assert_control(workspace=workspace, tmp=tmp)

    result = _sh(
        f"cat {shlex.quote(str(secret))}", workspace=workspace, tmp=tmp, deny_read=[secret_dir]
    )
    assert result.returncode != 0
    assert "SUPER-SECRET-DECOY" not in result.stdout


def test_file_outside_workspace_and_tmp_is_not_visible(
    tmp_path: Path, sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    workspace, tmp = sandboxed_dirs
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    decoy = elsewhere / "notes.txt"
    decoy.write_text("ELSEWHERE-DECOY")
    _assert_control(workspace=workspace, tmp=tmp)

    result = _sh(f"cat {shlex.quote(str(decoy))}", workspace=workspace, tmp=tmp)
    assert result.returncode != 0
    assert "ELSEWHERE-DECOY" not in result.stdout


def test_write_to_git_hook_is_denied_in_a_real_repo(
    sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    workspace, tmp = sandboxed_dirs
    init = subprocess.run(
        ["git", "init", "-q", str(workspace)], capture_output=True, text=True, check=False
    )
    assert init.returncode == 0, init.stderr
    _assert_control(workspace=workspace, tmp=tmp)

    hook = workspace / ".git" / "hooks" / "pre-commit"
    result = _sh(f"echo evil > {shlex.quote(str(hook))}", workspace=workspace, tmp=tmp)
    assert result.returncode != 0
    assert not hook.exists()


def test_network_namespace_is_unshared(sandboxed_dirs: tuple[Path, Path], bwrap: None) -> None:
    # `--unshare-all` includes --unshare-net, so /proc/net/dev inside the sandbox
    # must list loopback only. (`cat < /dev/tcp/...` is a bash builtin and /bin/sh
    # here is dash, so /proc/net/dev is the portable probe.)
    workspace, tmp = sandboxed_dirs
    _assert_control(workspace=workspace, tmp=tmp)

    result = _sh("cat /proc/net/dev", workspace=workspace, tmp=tmp)
    assert result.returncode == 0, result.stderr
    inside = _interfaces(result.stdout)
    assert inside == {"lo"}, result.stdout

    # Only meaningful if this host actually has a non-loopback interface to hide.
    host_only = _interfaces(Path("/proc/net/dev").read_text()) - {"lo"}
    if host_only:
        assert not (host_only & inside), result.stdout


def test_pid_namespace_is_unshared(sandboxed_dirs: tuple[Path, Path], bwrap: None) -> None:
    workspace, tmp = sandboxed_dirs
    _assert_control(workspace=workspace, tmp=tmp)

    result = _sh("echo $$", workspace=workspace, tmp=tmp)
    assert result.returncode == 0, result.stderr
    inner_pid = int(result.stdout.strip())
    assert inner_pid < 100, f"pid {inner_pid} looks like a host pid, not a fresh PID namespace"
    if os.getpid() > 100:
        assert inner_pid != os.getpid()

    listing = _sh("ls /proc", workspace=workspace, tmp=tmp)
    assert listing.returncode == 0, listing.stderr
    ns_pids = {name for name in listing.stdout.split() if name.isdigit()}
    assert ns_pids, listing.stdout
    if os.getpid() > 100:
        assert str(os.getpid()) not in ns_pids, "the host test process is visible inside"


def test_symlink_inside_workspace_cannot_reach_denied_path(
    tmp_path: Path, sandboxed_dirs: tuple[Path, Path], bwrap: None
) -> None:
    workspace, tmp = sandboxed_dirs
    secret_dir = tmp_path / "secretdir"
    secret_dir.mkdir()
    secret = secret_dir / "id_rsa"
    secret.write_text("SYMLINK-DECOY")
    _assert_control(workspace=workspace, tmp=tmp)

    # Positive control for symlinks: one pointing at a workspace file resolves fine.
    normal = workspace / "normal.txt"
    normal.write_text("hello")
    good_link = workspace / "good-link"
    good_link.symlink_to(normal)
    good = _sh(f"cat {shlex.quote(str(good_link))}", workspace=workspace, tmp=tmp)
    assert good.returncode == 0, good.stderr
    assert "hello" in good.stdout

    bad_link = workspace / "bad-link"
    bad_link.symlink_to(secret)
    result = _sh(
        f"cat {shlex.quote(str(bad_link))}", workspace=workspace, tmp=tmp, deny_read=[secret_dir]
    )
    assert result.returncode != 0
    assert "SYMLINK-DECOY" not in result.stdout
