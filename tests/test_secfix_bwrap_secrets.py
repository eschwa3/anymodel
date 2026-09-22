"""bwrap-side name-based secret deny: argv-construction unit tests + one real run.

`build_bwrap_argv` masks secret-named regular files under the workspace with
`--ro-bind /dev/null <file>` (bubblewrap cannot match mounts by regex the way
Seatbelt's `_secret_name_denies` does), so the unit tests assert on the
constructed argv and run on any OS. The single real test needs Linux with a
working bwrap and skips everywhere else.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

import pytest

from anymodel_subagents.tools import sandbox


@pytest.fixture
def ws_dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return workspace, tmp


def _null_binds(argv: list[str]) -> list[tuple[str, str]]:
    """(source, target) of every `--ro-bind /dev/null <target>` in the argv."""
    return [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok == "--ro-bind" and argv[i + 1] == "/dev/null"
    ]


def _workspace_bind_index(argv: list[str], workspace: Path) -> int:
    ws_str = str(workspace.resolve())
    for i, tok in enumerate(argv):
        if tok == "--bind" and argv[i + 1] == ws_str and argv[i + 2] == ws_str:
            return i
    raise AssertionError("workspace --bind not found in argv")


def test_bwrap_argv_masks_secret_named_files_after_workspace_bind(
    ws_dirs: tuple[Path, Path],
) -> None:
    workspace, tmp = ws_dirs
    (workspace / ".env").write_text("secret")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "prod.env").write_text("secret")
    (workspace / "k").mkdir()
    (workspace / "k" / "server.pem").write_text("secret")
    (workspace / "id_rsa").write_text("secret")
    (workspace / "main.py").write_text("print('ok')")

    argv = sandbox.build_bwrap_argv(["/bin/true"], workspace=workspace, tmp=tmp, deny_read=[])
    ws_i = _workspace_bind_index(argv, workspace)
    dash_i = argv.index("--")
    masked = {target for _, target in _null_binds(argv)}
    base = workspace.resolve()
    assert masked == {
        str(base / ".env"),
        str(base / "sub" / "prod.env"),
        str(base / "k" / "server.pem"),
        str(base / "id_rsa"),
    }
    for _source, target in _null_binds(argv):
        assert argv[argv.index(target) - 1] == "/dev/null"
    null_is = [i for i, tok in enumerate(argv) if tok == "--ro-bind" and argv[i + 1] == "/dev/null"]
    assert all(ws_i < i < dash_i for i in null_is)


def test_bwrap_argv_leaves_examples_plain_names_symlinks_and_git_alone(
    ws_dirs: tuple[Path, Path],
) -> None:
    workspace, tmp = ws_dirs
    (workspace / ".env.example").write_text("template")
    (workspace / "main.py").write_text("print('ok')")
    (workspace / "real.txt").write_text("plain")
    (workspace / ".env").symlink_to(workspace / "real.txt")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "prod.env").write_text("git-side secret")
    (workspace / ".git" / "credentials").write_text("creds")

    argv = sandbox.build_bwrap_argv(["/bin/true"], workspace=workspace, tmp=tmp, deny_read=[])
    # Nothing here is a plain secret-named regular file in the workspace tree:
    # `.env.example` is an exception, `main.py`/`real.txt` are innocent, `.env`
    # is a symlink, and the walk never enters `.git`.
    assert _null_binds(argv) == []


def test_bwrap_argv_entry_limit_stops_the_walk(
    ws_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, tmp = ws_dirs
    for name in ("a.env", "b.env", "c.env"):
        (workspace / name).write_text("secret")
    monkeypatch.setattr(
        sandbox,
        "_secret_files_under",
        functools.partial(sandbox._secret_files_under, limit_entries=2),
    )
    argv = sandbox.build_bwrap_argv(["/bin/true"], workspace=workspace, tmp=tmp, deny_read=[])
    masked = len(_null_binds(argv))
    assert masked == 2, "walk must stop after `limit_entries` entries, whichever they are"


def test_bwrap_argv_hit_limit_stops_the_walk(
    ws_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, tmp = ws_dirs
    for name in ("a.env", "b.env", "c.env", "d.env"):
        (workspace / name).write_text("secret")
    monkeypatch.setattr(
        sandbox,
        "_secret_files_under",
        functools.partial(sandbox._secret_files_under, limit_hits=2),
    )
    argv = sandbox.build_bwrap_argv(["/bin/true"], workspace=workspace, tmp=tmp, deny_read=[])
    masked = len(_null_binds(argv))
    assert masked == 2, "walk must stop once `limit_hits` secrets have been found"


def _bwrap_usable() -> bool:
    return sys.platform == "linux" and sandbox.detect() == "bwrap"


@pytest.mark.skipif(not _bwrap_usable(), reason="needs Linux with a working bwrap")
def test_real_bwrap_hides_workspace_env_but_reads_main_py(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (workspace / ".env").write_text("DECOY-ENV-SECRET")
    (workspace / "main.py").write_text("print('main-ok')")

    def cat(target: Path) -> subprocess.CompletedProcess[str]:
        wrapped = sandbox.wrap(
            ["/bin/cat", str(target)], workspace=workspace, tmp=tmp, deny_read=[], kind="bwrap"
        )
        return subprocess.run(
            wrapped, capture_output=True, text=True, timeout=60, check=False, cwd=str(workspace)
        )

    secret = cat(workspace / ".env")
    assert "DECOY-ENV-SECRET" not in secret.stdout
    assert "DECOY-ENV-SECRET" not in secret.stderr

    # Positive control: the same sandboxed cat reads a plain workspace file.
    control = cat(workspace / "main.py")
    assert control.returncode == 0, control.stderr
    assert "main-ok" in control.stdout
