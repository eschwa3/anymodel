"""Regression tests for the pre-public review's sandbox findings (H3, M6, bwrap probe)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anymodel_subagents.tools import sandbox

requires_seatbelt = pytest.mark.skipif(
    sandbox.detect() != "seatbelt",
    reason="requires a working Seatbelt (sandbox-exec) on this machine",
)


def _cat(path: Path, *, workspace: Path, tmp: Path) -> subprocess.CompletedProcess:
    argv = sandbox.wrap(
        ["/bin/cat", str(path)], workspace=workspace, tmp=tmp, deny_read=[], kind="seatbelt"
    )
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=10, check=False, cwd=str(workspace)
    )


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "work-space"  # a '-' exercises the path escaping in the regex
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return workspace, tmp


@requires_seatbelt
@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".env.local",
        "sub/prod.env",
        "sub/deep/server.pem",
        "deploy.key",
        "id_rsa",
        "k/id_ed25519",
        ".git-credentials",
        ".netrc",
        "credentials.json",
        "conf/credentials",
        "keys/authorized_keys",
    ],
)
def test_secret_shaped_files_in_the_workspace_are_unreadable(dirs, name: str) -> None:
    workspace, tmp = dirs
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DECOY-SECRET-VALUE")

    proc = _cat(target, workspace=workspace, tmp=tmp)

    assert "DECOY-SECRET-VALUE" not in proc.stdout
    assert proc.returncode != 0


@requires_seatbelt
@pytest.mark.parametrize(
    "name", ["main.py", ".env.example", "sub/.env.sample", "environment.py", "docs/credentials.md"]
)
def test_ordinary_files_stay_readable(dirs, name: str) -> None:
    # Positive control: the denies above must not pass because nothing is readable.
    workspace, tmp = dirs
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ORDINARY-CONTENT")

    proc = _cat(target, workspace=workspace, tmp=tmp)

    assert proc.returncode == 0, proc.stderr
    assert "ORDINARY-CONTENT" in proc.stdout


def test_library_preferences_is_not_read_allowed() -> None:
    assert "/Library/Preferences" not in sandbox._READ_ALLOW_SYSTEM_DIRS


@requires_seatbelt
def test_library_preferences_is_unreadable_and_git_still_works(dirs) -> None:
    workspace, tmp = dirs
    plist = Path("/Library/Preferences/SystemConfiguration/preferences.plist")
    if plist.exists():
        assert _cat(plist, workspace=workspace, tmp=tmp).returncode != 0
    git = Path(sandbox.real_toolchain_bin()) / "git"
    argv = sandbox.wrap(
        [str(git), "--version"], workspace=workspace, tmp=tmp, deny_read=[], kind="seatbelt"
    )
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=20, check=False, cwd=str(workspace)
    )
    assert proc.returncode == 0, proc.stderr
    assert "git version" in proc.stdout


def test_bwrap_probe_uses_the_real_isolation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox._probe_bwrap("/usr/bin/bwrap") is True
    assert "--unshare-all" in seen[0]
    assert seen[0][seen[0].index("--cap-drop") + 1] == "ALL"


def test_bwrap_binds_private_tmp_before_the_workspace(tmp_path: Path) -> None:
    # Later mounts win: a workspace under /tmp must not be hidden by the private /tmp bind.
    ws, tmp = tmp_path / "ws", tmp_path / "tmp"
    ws.mkdir()
    tmp.mkdir()
    argv = sandbox.build_bwrap_argv(["true"], workspace=ws, tmp=tmp, deny_read=[])
    pairs = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--bind"]
    targets = [dst for _, dst in pairs]
    assert targets.index("/tmp") < targets.index(str(ws.resolve()))
