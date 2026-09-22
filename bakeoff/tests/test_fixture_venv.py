"""Verifies `fixture.ensure_fixture_venv` -- the real `.venv` (pytest
included) built inside a fixture repo when `--bash` gives the worker a
sandboxed Bash tool. All subprocess calls are faked (nothing is installed,
no network), checking command order, the uv offline -> online fallback, the
pip fallback without uv, the `.git/info/exclude` entry, and the failure
paths. Also guards the harness contract that `--dry-run` never builds one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from real import fixture, runner

FAKE_UV = "/usr/local/bin/uv"


def _fake_run(
    monkeypatch,
    *,
    uv_path: str | None,
    create_rc: int = 0,
    offline_install_rc: int = 0,
    install_rc: int = 0,
    verify_rc: int = 0,
) -> list[list[str]]:
    """Replace subprocess.run inside `real.fixture` with a fake that records
    argv and simulates the venv-creation step by actually creating
    `<venv>/bin/python` and `pyvenv.cfg`. Returns the recorded argv lists.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        calls.append(argv)
        is_create = (len(argv) > 1 and argv[0] == "uv" and argv[1] == "venv") or (
            len(argv) > 2 and argv[1] == "-m" and argv[2] == "venv"
        )
        if is_create:
            venv_dir = Path(argv[-1])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!/bin/sh\n")
            (venv_dir / "pyvenv.cfg").write_text("home = /fake\n")
            rc = create_rc
        elif "import pytest" in argv:
            rc = verify_rc
        elif "--offline" in argv:
            rc = offline_install_rc
        else:
            rc = install_rc
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(fixture.subprocess, "run", fake_run)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: uv_path if name == "uv" else None)
    return calls


def test_uv_builds_venv_offline_then_verifies(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, uv_path=FAKE_UV)
    venv_dir = tmp_path / ".venv"
    venv_python = venv_dir / "bin" / "python"

    assert fixture.ensure_fixture_venv(tmp_path) is None

    # Order: create, offline install, verify.
    assert calls == [
        ["uv", "venv", "--python", sys.executable, str(venv_dir)],
        ["uv", "pip", "install", "--offline", "--python", str(venv_python), "pytest"],
        [str(venv_python), "-c", "import pytest"],
    ]
    assert venv_python.exists()
    assert (venv_dir / "pyvenv.cfg").exists()
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".venv/") == 1


def test_uv_offline_install_failure_falls_back_online(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, uv_path=FAKE_UV, offline_install_rc=1)
    venv_python = tmp_path / ".venv" / "bin" / "python"

    assert fixture.ensure_fixture_venv(tmp_path) is None

    assert "--offline" in calls[1]
    assert calls[2] == ["uv", "pip", "install", "--python", str(venv_python), "pytest"]
    assert calls[3] == [str(venv_python), "-c", "import pytest"]
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".venv/") == 1


def test_without_uv_uses_venv_module_and_pip(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, uv_path=None)
    venv_dir = tmp_path / ".venv"
    venv_python = venv_dir / "bin" / "python"

    assert fixture.ensure_fixture_venv(tmp_path) is None

    assert calls == [
        [sys.executable, "-m", "venv", str(venv_dir)],
        [str(venv_python), "-m", "pip", "install", "pytest"],
        [str(venv_python), "-c", "import pytest"],
    ]


def test_exclude_gains_exactly_one_venv_line_when_called_twice(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, uv_path=FAKE_UV)

    assert fixture.ensure_fixture_venv(tmp_path) is None
    # Second call: `.venv/bin/python` now exists, so it must short-circuit.
    assert fixture.ensure_fixture_venv(tmp_path) is None

    assert len(calls) == 3  # the second call made no subprocess calls
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".venv/") == 1


def test_existing_venv_python_short_circuits_without_subprocess(tmp_path, monkeypatch):
    venv_dir = tmp_path / ".venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python").write_text("")
    calls = _fake_run(monkeypatch, uv_path=FAKE_UV)

    assert fixture.ensure_fixture_venv(tmp_path) is None

    assert calls == []
    assert not (tmp_path / ".git" / "info" / "exclude").exists()


def test_failed_verify_returns_warning_and_does_not_raise(tmp_path, monkeypatch):
    _fake_run(monkeypatch, uv_path=FAKE_UV, verify_rc=1)

    warning = fixture.ensure_fixture_venv(tmp_path)

    assert isinstance(warning, str)
    assert "pytest" in warning


@pytest.mark.asyncio
async def test_dry_run_never_builds_fixture_venv(tmp_path, monkeypatch):
    """`--dry-run` fabricates results without any worker, so it must not
    build a venv either. The sentinel raises into `_run_one_cli_task`'s
    harness-error catch-all if the contract is ever broken, which would show
    up as a non-completed record -- caught by the assertions below.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("ensure_fixture_venv must not run in a --dry-run")

    monkeypatch.setattr(fixture, "ensure_fixture_venv", _boom)

    args = argparse.Namespace(
        dry_run=True,
        repeats=1,
        max_turns=5,
        timeout=30,
        parallel=1,
        bash=True,
        yes=True,
        orchestrator_price_per_mtok=5.0,
        judge_model=None,
    )
    out_dir = tmp_path / "run"
    out_dir.mkdir()

    rc = await runner.run_real_suite(args, out_dir, ["fake-a"], ["R1"])

    assert rc == 0
    records = [
        json.loads(line) for line in (out_dir / "results_real.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["status"] == "completed"
    assert "ensure_fixture_venv" not in (records[0].get("error") or "")
