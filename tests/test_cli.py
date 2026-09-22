"""Tests for cli.py's `run` command, in particular its cwd validation.

`_run_command` previously only checked `--cwd.is_dir()`, which would happily
point a worker at `$HOME` or a non-git directory -- paths the MCP `dispatch`
path (`config.validate_cwd`) would refuse outright. These tests confirm the
CLI now runs the same `validate_cwd` policy, without making any real
OpenRouter network calls (the worker loop itself is monkeypatched out).

Later sections cover the rest of the CLI surface: `_build_parser` (defaults,
`--mode` choices, the mutually exclusive `--prompt`/`--prompt-file` group),
reading the prompt from a file, `--json` output, and `main()`'s `sys.exit`
wiring.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from anymodel_subagents import cli
from anymodel_subagents.types import Usage, WorkerResult


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)


def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "model": "test/model",
        "cwd": None,
        "prompt": "do the thing",
        "prompt_file": None,
        "mode": "read-only",
        "role_prompt": cli.DEFAULT_ROLE_PROMPT,
        "max_turns": 5,
        "timeout": 10.0,
        "transcript": None,
        "json_output": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-key-not-real")


@pytest.fixture(autouse=True)
def _isolated_state_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep validate_cwd's state-dir check, and load_config(), off the real
    filesystem/environment for every test in this module.
    """
    for var in ("ANYMODEL_STATE_DIR", "CLAUDE_PLUGIN_DATA", "XDG_STATE_HOME", "ANYMODEL_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "_state"))
    monkeypatch.setenv("ANYMODEL_CONFIG", str(tmp_path / "_no_such_config.yaml"))


async def test_run_refuses_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rc = await cli._run_command(_args(cwd=fake_home))

    assert rc == 2
    assert "invalid" in capsys.readouterr().err.lower()


async def test_run_refuses_non_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    plain = tmp_path / "plain"
    plain.mkdir()

    rc = await cli._run_command(_args(cwd=plain))

    assert rc == 2
    assert "invalid" in capsys.readouterr().err.lower()


async def test_run_refuses_ancestor_of_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "a" / "b" / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    rc = await cli._run_command(_args(cwd=tmp_path / "a"))

    assert rc == 2


async def test_run_accepts_temp_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors bakeoff/run.py: a temp-dir git repo (which, on macOS, resolves
    under /private/var/folders/...) must be accepted by the same validate_cwd
    policy `dispatch` uses -- this is the scenario item 3 of the review is
    about, confirmed working end to end through the CLI.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    captured_ws_roots: list[Path] = []

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        captured_ws_roots.append(kwargs["ws"].root)
        return WorkerResult(
            status="completed",
            final_message="all good",
            model=kwargs["model"],
            turns=1,
            usage=Usage(),
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    rc = await cli._run_command(_args(cwd=repo))

    assert rc == 0
    assert captured_ws_roots == [repo.resolve()]


async def test_run_accepts_subdirectory_of_temp_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    subdir = repo / "src"
    subdir.mkdir()

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="completed", final_message="ok", model=kwargs["model"], turns=1, usage=Usage()
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    rc = await cli._run_command(_args(cwd=subdir))

    assert rc == 0


async def test_run_refuses_nonexistent_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    rc = await cli._run_command(_args(cwd=tmp_path / "does-not-exist"))
    assert rc == 2


async def test_run_missing_api_key_returns_error_before_touching_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # An invalid cwd (None) would blow up validate_cwd if reached -- the
    # missing-key check must short-circuit before that.
    rc = await cli._run_command(_args(cwd=None))
    assert rc == 2


# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------


def test_build_parser_run_defaults() -> None:
    args = cli._build_parser().parse_args(
        ["run", "--model", "test/model", "--cwd", "/tmp/ws", "--prompt", "hello"]
    )

    assert args.command == "run"
    assert args.model == "test/model"
    assert args.cwd == Path("/tmp/ws")
    assert args.prompt == "hello"
    assert args.prompt_file is None
    assert args.mode == "read-only"
    assert args.role_prompt == cli.DEFAULT_ROLE_PROMPT
    assert args.max_turns == 40
    assert args.timeout == 900.0
    assert args.transcript is None
    assert args.json_output is False


def test_build_parser_accepts_every_option() -> None:
    args = cli._build_parser().parse_args(
        [
            "run",
            "--model",
            "test/model",
            "--cwd",
            "/tmp/ws",
            "--prompt-file",
            "/tmp/task.txt",
            "--mode",
            "edit+bash",
            "--role-prompt",
            "be terse",
            "--max-turns",
            "3",
            "--timeout",
            "1.5",
            "--transcript",
            "/tmp/t.json",
            "--json",
        ]
    )

    assert args.prompt is None
    assert args.prompt_file == Path("/tmp/task.txt")
    assert args.mode == "edit+bash"
    assert args.role_prompt == "be terse"
    assert args.max_turns == 3
    assert args.timeout == 1.5
    assert args.transcript == Path("/tmp/t.json")
    assert args.json_output is True


@pytest.mark.parametrize("mode", ["read-only", "edit", "edit+bash"])
def test_build_parser_accepts_valid_modes(mode: str) -> None:
    args = cli._build_parser().parse_args(
        ["run", "--model", "m", "--cwd", "/tmp/ws", "--prompt", "p", "--mode", mode]
    )
    assert args.mode == mode


def test_build_parser_rejects_unknown_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(
            ["run", "--model", "m", "--cwd", "/tmp/ws", "--prompt", "p", "--mode", "write"]
        )
    assert exc.value.code == 2
    assert "--mode" in capsys.readouterr().err


def test_build_parser_requires_exactly_one_prompt_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli._build_parser()

    # Neither --prompt nor --prompt-file: the group is required.
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(["run", "--model", "m", "--cwd", "/tmp/ws"])
    assert missing.value.code == 2
    assert "one of the arguments" in capsys.readouterr().err

    # Both: mutually exclusive.
    with pytest.raises(SystemExit) as both:
        parser.parse_args(
            [
                "run",
                "--model",
                "m",
                "--cwd",
                "/tmp/ws",
                "--prompt",
                "p",
                "--prompt-file",
                "/tmp/task.txt",
            ]
        )
    assert both.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],  # no subcommand (subparsers are required)
        ["bogus"],  # unknown subcommand
        ["run", "--cwd", "/tmp/ws", "--prompt", "p"],  # missing required --model
        ["run", "--model", "m", "--prompt", "p"],  # missing required --cwd
    ],
)
def test_build_parser_rejects_invalid_invocations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(argv)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# --prompt-file
# ---------------------------------------------------------------------------


async def test_run_reads_prompt_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("task from a file\n")

    seen: dict[str, Any] = {}

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        seen.update(kwargs)
        return WorkerResult(
            status="completed", final_message="ok", model=kwargs["model"], turns=1, usage=Usage()
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    rc = await cli._run_command(_args(cwd=repo, prompt=None, prompt_file=prompt_file))

    assert rc == 0
    assert seen["task_prompt"] == "task from a file\n"


async def test_run_unreadable_prompt_file_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))

    def boom(**kwargs: Any) -> WorkerResult:
        raise AssertionError("run_worker must not run when the prompt file is unreadable")

    monkeypatch.setattr(cli, "run_worker", boom)

    # cwd is deliberately not a valid git repo: the prompt-file read must fail
    # before validate_cwd is ever consulted.
    rc = await cli._run_command(
        _args(cwd=tmp_path / "not-a-repo", prompt=None, prompt_file=tmp_path / "nope.txt")
    )

    assert rc == 2
    assert "could not read --prompt-file" in capsys.readouterr().err


async def test_run_prompt_file_that_is_a_directory_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    a_directory = tmp_path / "a-directory"
    a_directory.mkdir()

    rc = await cli._run_command(_args(cwd=tmp_path, prompt=None, prompt_file=a_directory))

    assert rc == 2
    assert "could not read --prompt-file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --json output and exit code
# ---------------------------------------------------------------------------


async def test_run_json_output_prints_dataclass_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="completed",
            final_message="the final answer",
            model=kwargs["model"],
            turns=2,
            usage=Usage(prompt_tokens=10, completion_tokens=5, cost=0.001, requests=1),
            tool_calls=3,
            changed_files=["a.txt"],
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    rc = await cli._run_command(_args(cwd=repo, json_output=True))

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "completed"
    assert payload["final_message"] == "the final answer"
    assert payload["model"] == "test/model"
    assert payload["turns"] == 2
    assert payload["tool_calls"] == 3
    assert payload["changed_files"] == ["a.txt"]
    assert payload["usage"]["prompt_tokens"] == 10
    # --json is the only output channel; no human-readable summary on stderr.
    assert captured.err == ""


async def test_run_returns_1_for_non_completed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="max_turns",
            final_message="ran out of turns",
            model=kwargs["model"],
            turns=5,
            usage=Usage(),
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    rc = await cli._run_command(_args(cwd=repo))

    assert rc == 1


# ---------------------------------------------------------------------------
# main() and the __main__ entry point
# ---------------------------------------------------------------------------


def test_main_runs_command_and_exits_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="completed", final_message="done", model=kwargs["model"], turns=1, usage=Usage()
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)
    monkeypatch.setattr(
        sys,
        "argv",
        ["anymodel-worker", "run", "--model", "test/model", "--cwd", str(repo), "--prompt", "hi"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "done" in capsys.readouterr().out


def test_main_exits_1_when_worker_does_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="error",
            final_message="boom",
            model=kwargs["model"],
            turns=1,
            usage=Usage(),
            error="boom",
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)
    monkeypatch.setattr(
        sys,
        "argv",
        ["anymodel-worker", "run", "--model", "m", "--cwd", str(repo), "--prompt", "hi"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_main_exits_2_on_invalid_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["anymodel-worker"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_exits_2_when_api_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["anymodel-worker", "run", "--model", "m", "--cwd", str(tmp_path), "--prompt", "hi"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_module_entry_point_exits_2_without_a_subcommand() -> None:
    """Runs `python -m anymodel_subagents.cli`, exercising the
    `if __name__ == "__main__": main()` guard. No subcommand
    means argparse exits before any API key or network is needed.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "anymodel_subagents.cli"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "usage:" in proc.stderr


async def test_run_passes_configured_max_output_tokens_to_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    config = tmp_path / "config.yaml"
    config.write_text("max_output_tokens: 4096\n")
    monkeypatch.setenv("ANYMODEL_CONFIG", str(config))
    caps: list[int | None] = []

    async def fake_run_worker(**kwargs: Any) -> WorkerResult:
        caps.append(kwargs["client"]._max_output_tokens)
        return WorkerResult(
            status="completed", final_message="ok", model=kwargs["model"], turns=1, usage=Usage()
        )

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)

    assert await cli._run_command(_args(cwd=repo)) == 0
    assert caps == [4096]
