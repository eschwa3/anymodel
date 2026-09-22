"""Tests for tools/bash.py: the allowlist parser, env scrubbing, execution
limits (timeout/output cap), and sandboxed vs. unsandboxed dispatch.
"""

from __future__ import annotations

import asyncio
import copy
import os
import subprocess
import sys
import venv as venv_mod
from pathlib import Path

import pytest

from anymodel_subagents.config import DEFAULT_BASH_ALLOW
from anymodel_subagents.tools import sandbox
from anymodel_subagents.tools.bash import (
    Bash,
    BashPolicy,
    _build_env,
    _nproc_ceiling,
    _wrap_with_resource_limits,
    compute_git_extra_read,
    resolve_venv,
    validate_command,
)
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import PolicyError, ToolError

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="written against macOS/Seatbelt")

# The bare `sys.executable` in a `uv run pytest` invocation is this project's
# own dev venv interpreter (e.g. `.venv/bin/python3`), which lives outside
# both the test's temp workspace and the sandbox's read-allowlist -- and
# Python's own venv detection looks for a `pyvenv.cfg` next to wherever it
# was invoked from, so running it via that path fails closed under the
# deny-by-default profile for reasons that have nothing to do with the
# sandbox's real job. Resolving through the venv's symlinks to the real
# base interpreter (under `~/.local/share/uv/python/...`, which *is*
# allowlisted -- see sandbox.py) is what a real worker's own workspace-local
# venv would look like once its symlinks are followed, so it's the
# representative thing to sandbox in these tests.
_REAL_PYTHON = os.path.realpath(sys.executable)


# --------------------------------------------------------------------------- validate_command: allowed

_ALLOWED = [
    "pytest",
    "pytest -k foo",
    "python -m pytest tests/",
    "python3 -m pytest -q",
    "uv run pytest -q",
    "npm test",
    "npm run lint",
    "npx tsc --noEmit",
    "cargo test --all",
    "go test ./...",
    "make test",
    "ruff check .",
    "mypy src",
    "git status",
    "git diff HEAD",
    "git log -n 5",
    "ls -la",
    "cat a.txt",
    "grep -n foo a.txt",
    "rg foo",
    "find . -name '*.py'",
    "echo hello",
    "pwd",
    "true",
    "echo hi 2>&1",
    "echo hi 1>&2",
]


@pytest.mark.parametrize("command", _ALLOWED)
def test_allowed_commands_pass_when_sandboxed(command: str) -> None:
    validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


@pytest.mark.parametrize("command", _ALLOWED)
def test_allowed_simple_commands_pass_when_unsandboxed(command: str) -> None:
    # None of the fixture commands above use chaining, so they're all valid
    # single simple commands too.
    validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=False)


def test_chained_allowed_commands_pass_when_sandboxed() -> None:
    validate_command("git status && npm test", DEFAULT_BASH_ALLOW, sandboxed=True)
    validate_command("pytest -q; echo done", DEFAULT_BASH_ALLOW, sandboxed=True)
    validate_command("ls | grep foo", DEFAULT_BASH_ALLOW, sandboxed=True)
    validate_command("make test || echo failed", DEFAULT_BASH_ALLOW, sandboxed=True)


# --------------------------------------------------------------------------- validate_command: rejected


_REJECTED = [
    # command substitution / backticks
    "echo $(whoami)",
    "echo `whoami`",
    "pytest $(rm -rf /)",
    # redirection (except 2>&1 / 1>&2)
    "echo hi > out.txt",
    "cat file < in.txt",
    "echo hi >> out.txt",
    "echo hi 2> err.txt",
    # backgrounding
    "pytest &",
    "npm test &",
    # env-assignment prefix
    "FOO=bar echo hi",
    "OPENROUTER_API_KEY=x pytest",
    # eval/exec/source/.
    "eval echo hi",
    "exec ls",
    "source setup.sh",
    ". setup.sh",
    # path-qualified command word
    "/bin/echo hi",
    "./script.sh",
    "bin/foo",
    "../escape/tool",
    # find: -exec/-execdir/-delete/-ok
    "find . -exec rm {} ;",
    "find . -execdir rm {} ;",
    "find . -delete",
    "find . -ok rm {} ;",
    # git: -c / --exec-path / --upload-pack / -C, and unlisted subcommands
    "git -c core.pager=cat status",
    "git --exec-path=/tmp status",
    "git --upload-pack=/tmp/evil status",
    "git -C /tmp status",
    "git commit -m evil",
    "git push origin main",
    # git: --no-index / -O / --ext-diff / --textconv / --output (all read or
    # execute something other than plain tracked repo content)
    "git diff --no-index /etc/passwd /etc/hosts",
    "git diff -O/tmp/orderfile",
    "git diff --ext-diff",
    "git diff --textconv",
    "git diff --output=/tmp/out.diff",
    "git diff --output /tmp/out.diff",
    # rg --pre
    "rg --pre=cat pattern",
    "rg --pre cat pattern",
    # not in allowlist at all
    "curl http://example.com",
    "rm -rf /",
    "sudo ls",
    "nc -l 1234",
    # empty / whitespace-only
    "",
    "   ",
    # newline-separated commands (must not smuggle a second command past the
    # allowlist by hiding behind whitespace-splitting)
    "ls\nrm -rf /",
    "pytest\ncurl http://evil",
    # NUL / control characters
    "echo hi\x00; rm -rf /",
    "echo hi\x07",
    # unicode bidi/invisible-formatting tricks
    "echo hi" + chr(0x202E),
    "ls" + chr(0x200B) + " -la",
]


@pytest.mark.parametrize("command", _REJECTED)
def test_rejected_commands_fail_when_sandboxed(command: str) -> None:
    with pytest.raises((PolicyError, ToolError)):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


@pytest.mark.parametrize("command", _REJECTED)
def test_rejected_commands_fail_when_unsandboxed(command: str) -> None:
    with pytest.raises((PolicyError, ToolError)):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=False)


def test_chained_commands_rejected_when_unsandboxed() -> None:
    # Individually-allowed commands chained together require a shell, which
    # unsandboxed mode refuses to use at all.
    with pytest.raises(PolicyError):
        validate_command("git status && npm test", DEFAULT_BASH_ALLOW, sandboxed=False)
    with pytest.raises(PolicyError):
        validate_command("ls | grep foo", DEFAULT_BASH_ALLOW, sandboxed=False)


def test_pipe_to_disallowed_command_rejected_even_when_sandboxed() -> None:
    with pytest.raises(PolicyError):
        validate_command("cat a.txt | sh", DEFAULT_BASH_ALLOW, sandboxed=True)


def test_explicitly_allowlisted_path_qualified_binary_is_allowed() -> None:
    allow = (*DEFAULT_BASH_ALLOW, "/usr/local/bin/mytool")
    validate_command("/usr/local/bin/mytool --check", allow, sandboxed=True)


def test_unicode_homoglyph_leader_does_not_match_allowlist() -> None:
    # Cyrillic 'а' (U+0430) instead of Latin 'a' -- must not be treated as "cat".
    command = "cаt file.txt"
    with pytest.raises(PolicyError):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


def test_malformed_shell_quoting_raises_policy_error() -> None:
    with pytest.raises(PolicyError):
        validate_command("echo 'unterminated", DEFAULT_BASH_ALLOW, sandboxed=True)


def test_non_string_command_raises_tool_error() -> None:
    with pytest.raises(ToolError):
        validate_command(None, DEFAULT_BASH_ALLOW, sandboxed=True)  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        validate_command(123, DEFAULT_BASH_ALLOW, sandboxed=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Bash tool: fixtures


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


@pytest.fixture
def sandboxed_bash(state_dir: Path) -> Bash:
    return Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=False, state_dir=state_dir)
    )


# --------------------------------------------------------------------------- Bash tool: instance schema description


def test_instance_schema_describes_rules_and_prefixes(state_dir: Path) -> None:
    allow = ("pytest -q", "ruff check .", "git status")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=False, state_dir=state_dir))
    desc = bash.schema["function"]["description"]
    for prefix in allow:
        assert prefix in desc
    assert desc.endswith("Allowed command prefixes: pytest -q, ruff check ., git status")
    assert "No output redirection" in desc
    assert "relative paths" in desc


def test_instance_schemas_differ_and_class_schema_is_unchanged(state_dir: Path) -> None:
    class_schema_before = copy.deepcopy(Bash.schema)
    a = Bash(BashPolicy(allow_prefixes=("pytest",), allow_unsandboxed=False, state_dir=state_dir))
    b = Bash(
        BashPolicy(allow_prefixes=("cargo test",), allow_unsandboxed=False, state_dir=state_dir)
    )
    desc_a = a.schema["function"]["description"]
    desc_b = b.schema["function"]["description"]
    assert desc_a != desc_b
    assert "cargo test" in desc_b
    assert "cargo test" not in desc_a
    # Deep-copied per instance: building b must not have mutated the class-level
    # fallback (or a's schema) in place.
    assert Bash.schema == class_schema_before


def test_schema_description_truncates_a_100_prefix_allowlist(state_dir: Path) -> None:
    allow = tuple(f"tool{i}" for i in range(100))
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=False, state_dir=state_dir))
    desc = bash.schema["function"]["description"]
    assert desc.endswith("tool59, ... (40 more)")
    assert "tool60" not in desc


def test_default_allowlist_description_is_compact(state_dir: Path) -> None:
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=False, state_dir=state_dir)
    )
    desc = bash.schema["function"]["description"]
    assert len(desc) < 1500
    assert "pytest" in desc
    assert "true" in desc  # the final default prefix made it in untruncated


# --------------------------------------------------------------------------- Bash tool: no-sandbox refusal


async def test_bash_refuses_when_no_sandbox_and_not_allowed(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=False, state_dir=state_dir)
    )
    with pytest.raises(PolicyError, match="sandbox"):
        await bash.run({"command": "echo hi"}, ws)


async def test_bash_runs_unsandboxed_single_command_when_allowed(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )
    out = await bash.run({"command": "echo hello-unsandboxed"}, ws)
    assert "exit code: 0" in out
    assert "hello-unsandboxed" in out


async def test_bash_unsandboxed_rejects_chained_command(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )
    with pytest.raises(PolicyError):
        await bash.run({"command": "echo a && echo b"}, ws)


async def test_bash_unsandboxed_does_not_use_a_shell(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If this ever ran through a shell, the embedded '$HOME' would be
    # expanded; exec'd as raw argv to /bin/echo, it must come back literal.
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, "echo")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    out = await bash.run({"command": "echo $HOME"}, ws)
    assert "$HOME" in out


# --------------------------------------------------------------------------- Bash tool: sandboxed execution


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_bash_sandboxed_runs_allowed_command(
    ws: LocalWorkspace, sandboxed_bash: Bash
) -> None:
    out = await sandboxed_bash.run({"command": "echo hello-sandboxed"}, ws)
    assert "exit code: 0" in out
    assert "hello-sandboxed" in out


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_bash_sandboxed_rejects_disallowed_command(
    ws: LocalWorkspace, sandboxed_bash: Bash
) -> None:
    with pytest.raises(PolicyError):
        await sandboxed_bash.run({"command": "curl http://example.com"}, ws)


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_bash_sandboxed_nonzero_exit_code_reported(
    ws: LocalWorkspace, state_dir: Path
) -> None:
    allow = (*DEFAULT_BASH_ALLOW, _REAL_PYTHON)
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=False, state_dir=state_dir))
    out = await bash.run({"command": f"{_REAL_PYTHON} -c 'import sys; sys.exit(7)'"}, ws)
    assert "exit code: 7" in out


# --------------------------------------------------------------------------- Env scrubbing


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_bash_env_scrubbed_of_secrets(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-should-not-leak")
    monkeypatch.setenv("FOO_TOKEN", "also-should-not-leak")
    monkeypatch.setenv("MY_SECRET", "also-should-not-leak")
    monkeypatch.setenv("SOME_PASSWORD", "also-should-not-leak")
    allow = (*DEFAULT_BASH_ALLOW, _REAL_PYTHON)
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=False, state_dir=state_dir))
    code = "import os; print(sorted(os.environ.keys()))"
    out = await bash.run({"command": f'{_REAL_PYTHON} -c "{code}"'}, ws)
    assert "OPENROUTER_API_KEY" not in out
    assert "FOO_TOKEN" not in out
    assert "MY_SECRET" not in out
    assert "SOME_PASSWORD" not in out
    for expected in ("PATH", "TMPDIR", "TERM", "CI", "NO_COLOR", "PYTHONDONTWRITEBYTECODE"):
        assert expected in out


async def test_bash_env_scrubbed_unsandboxed(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-should-not-leak")
    monkeypatch.setenv("SOME_SECRET", "also-should-not-leak")
    allow = (*DEFAULT_BASH_ALLOW, "env")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    out = await bash.run({"command": "env"}, ws)
    assert "OPENROUTER_API_KEY" not in out
    assert "SOME_SECRET" not in out


# --------------------------------------------------------------------------- Timeout


async def test_bash_timeout_kills_process_group_and_grandchildren(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, "python3")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    marker = ws.root / "grandchild-alive.txt"
    # A raw newline inside `command` is refused by the allowlist parser (see
    # test_rejected_commands_fail_when_sandboxed), so multi-line Python goes
    # into a script file on disk instead -- exactly how a real worker would
    # do it (Write the script, then Bash `python3 script.py`).
    script = ws.root / "grandchild_spawner.py"
    script.write_text(
        "import subprocess, time\n"
        "subprocess.Popen(['/bin/sh', '-c', "
        f"'while true; do date +%s > {marker} ; sleep 0.1; done'])\n"
        "time.sleep(30)\n"
    )
    out = await bash.run({"command": "python3 grandchild_spawner.py", "timeout": 1}, ws)
    assert "timed out" in out

    # Give any surviving grandchild a moment to have written another
    # heartbeat if it's still alive, then confirm it stopped updating.
    await asyncio.sleep(0.5)
    if marker.exists():
        first_seen = marker.read_text()
        await asyncio.sleep(0.5)
        second_seen = marker.read_text() if marker.exists() else None
        assert second_seen == first_seen, "grandchild kept running after timeout"


# --------------------------------------------------------------------------- Output cap


async def test_bash_output_capped_with_elision_marker(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, "python3")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    code = "print('A' * 50000)"
    out = await bash.run({"command": f'python3 -c "{code}"'}, ws)
    assert len(out) < 50000
    assert "omitted" in out
    assert out.startswith("exit code: 0")
    assert out.rstrip().endswith("A" * 100)  # tail preserved


async def test_bash_short_output_not_truncated(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )
    out = await bash.run({"command": "echo short"}, ws)
    assert "omitted" not in out


# --------------------------------------------------------------------------- timeout arg clamping


async def test_bash_timeout_arg_clamped(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    bash = Bash(
        BashPolicy(allow_prefixes=DEFAULT_BASH_ALLOW, allow_unsandboxed=True, state_dir=state_dir)
    )
    # Non-integer -> ToolError; out-of-range -> clamped rather than rejected.
    with pytest.raises(ToolError):
        await bash.run({"command": "echo hi", "timeout": "lots"}, ws)
    out = await bash.run({"command": "echo hi", "timeout": 10_000}, ws)
    assert "exit code: 0" in out


# --------------------------------------------------------------------------- tmp dir hygiene


async def test_bash_creates_and_cleans_up_job_tmp_dir(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, "python3")
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    bash_tmp_root = state_dir / "bash-tmp"
    await bash.run({"command": 'python3 -c "print(1)"'}, ws)
    assert bash_tmp_root.is_dir()
    assert list(bash_tmp_root.iterdir()) == []  # per-call tmp dir removed after use


# --------------------------------------------------------------------------- file-inspection path-escape check


def test_check_file_inspection_rejects_absolute_and_dotdot_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for command in [
        "cat /etc/passwd",
        "cat ~/.ssh/id_rsa",
        "ls /Users",
        "grep -r secret /Users",
        "find ../escape -name x",
        "head -n5 /etc/hosts",
        "tail /var/log/system.log",
        "wc -l /etc/passwd",
        "rg foo /Users/someone",
    ]:
        with pytest.raises(PolicyError):
            validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True, workspace=workspace)


def test_check_file_inspection_allows_relative_and_in_workspace_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    for command in [
        "cat a.txt",
        "cat sub/b.txt",
        "grep -n foo a.txt",
        "ls sub",
        "find . -name '*.py'",
        f"cat {workspace}/sub/b.txt",  # absolute, but resolves inside workspace
    ]:
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True, workspace=workspace)


def test_check_file_inspection_skipped_when_no_workspace_given() -> None:
    # workspace=None (the default) means the check is skipped -- callers
    # that don't yet know a workspace root still get the rest of the
    # allowlist checks; the OS sandbox is the real boundary either way.
    validate_command("cat /etc/passwd", DEFAULT_BASH_ALLOW, sandboxed=True)


# --------------------------------------------------------------------------- git worktree extra-read computation


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_compute_git_extra_read_normal_repo_workspace_at_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # The workspace IS the repo root: its own .git is already inside
    # WORKSPACE, nothing extra needed.
    assert compute_git_extra_read(repo) == ()


def test_compute_git_extra_read_workspace_is_repo_subdirectory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git(repo, "init", "-q")
    extra = compute_git_extra_read(repo / "sub")
    assert extra == (repo / ".git",)


def test_compute_git_extra_read_worktree_adds_gitdir_and_commondir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "wtbranch", str(worktree))

    extra = compute_git_extra_read(worktree)
    assert len(extra) == 2
    gitdir, commondir = extra
    assert gitdir == (repo / ".git" / "worktrees" / "wt").resolve()
    assert commondir == (repo / ".git").resolve()


def test_compute_git_extra_read_no_git_returns_empty(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert compute_git_extra_read(plain) == ()


# --------------------------------------------------------------------------- resource limits


def test_wrap_with_resource_limits_preserves_argv_without_shell_reinterpretation() -> None:
    wrapped = _wrap_with_resource_limits(["/bin/echo", "$HOME", "a b", "*"], nproc_ceiling=999999)
    assert wrapped[0] == "/bin/sh"
    assert wrapped[1] == "-c"
    assert wrapped[3:] == ["/bin/echo", "$HOME", "a b", "*"]
    result = subprocess.run(wrapped, capture_output=True, text=True, check=False)
    # Confirms empirically: no re-expansion of $HOME, no glob, no word
    # splitting of "a b" -- argv boundaries are preserved exactly.
    assert result.stdout == "$HOME a b *\n"


async def test_nproc_ceiling_is_above_current_process_count() -> None:
    ceiling = await _nproc_ceiling(margin=256)
    assert ceiling >= 256


async def test_bash_fork_bomb_is_capped_and_process_group_fully_killed(
    ws: LocalWorkspace, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded fork-bomb (not an unbounded one -- see task instructions)
    must hit EAGAIN well before completing 3000 attempted forks.

    Children exit quickly (rather than sleeping past the command's own
    timeout) so the command finishes -- and `communicate()` returns full
    output -- on its own; a *separate*, pre-existing test
    (`test_bash_timeout_kills_process_group_and_grandchildren`) already
    covers the "the overall command times out, killpg reaps every
    descendant" path on its own terms.
    """
    monkeypatch.setattr(sandbox, "detect", lambda: None)
    allow = (*DEFAULT_BASH_ALLOW, _REAL_PYTHON)
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=True, state_dir=state_dir))
    script = ws.root / "forkbomb.py"
    script.write_text(
        "import os, sys, time\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(3000):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(0.2)\n"
        "            os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    print(f'CAPPED after {n} forks')\n"
        "    sys.stdout.flush()\n"
        "    for _ in range(n):\n"
        "        try:\n"
        "            os.wait()\n"
        "        except ChildProcessError:\n"
        "            break\n"
    )
    out = await bash.run({"command": f"{_REAL_PYTHON} forkbomb.py", "timeout": 15}, ws)
    assert "exit code: 0" in out
    assert "CAPPED after" in out
    forks = int(out.split("CAPPED after")[1].split("forks")[0].strip())
    assert 0 < forks < 3000  # the ceiling actually stopped it well short of the attempted count

    survivor_check = await asyncio.to_thread(
        subprocess.run,
        ["/bin/sh", "-c", "pgrep -f forkbomb.py || true"],
        capture_output=True,
        text=True,
        check=False,
    )
    survivors = survivor_check.stdout.strip()
    assert survivors == "", f"fork-bomb descendants survived: {survivors!r}"


# --------------------------------------------------------------------------- pytest run in a real workspace venv


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_bash_runs_pytest_in_a_real_workspace_venv(tmp_path: Path, state_dir: Path) -> None:
    """The way a real user repo would: a project-local `.venv` inside the
    workspace, `python3 -m pytest` run exactly as the default allowlist
    permits, with the interpreter and pytest both living under WORKSPACE.
    """
    root = tmp_path / "repo"
    root.mkdir()
    venv_dir = root / ".venv"
    created = await asyncio.to_thread(
        subprocess.run,
        ["uv", "venv", "-p", "3.12", "--seed", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"could not create a scratch venv here: {created.stderr}")
    venv_python = venv_dir / "bin" / "python3"
    installed = await asyncio.to_thread(
        subprocess.run,
        ["uv", "pip", "install", "--python", str(venv_python), "-q", "pytest"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if installed.returncode != 0:
        pytest.skip(f"could not install pytest into the scratch venv: {installed.stderr}")

    (root / "test_trivial.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")

    allow = (*DEFAULT_BASH_ALLOW, str(venv_python))
    bash = Bash(BashPolicy(allow_prefixes=allow, allow_unsandboxed=False, state_dir=state_dir))
    ws = LocalWorkspace(root=root)
    out = await bash.run({"command": f"{venv_python} -m pytest -q"}, ws)
    assert "exit code: 0" in out
    assert "1 passed" in out


# --------------------------------------------------------------------------- Project venv


def _fake_venv(path: Path) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (path / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def test_resolve_venv_accepts_a_real_directory(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path / "repo" / ".venv")
    assert resolve_venv(venv, []) == Path(os.path.realpath(venv))
    assert resolve_venv(None, []) is None


def test_resolve_venv_refuses_links_overlaps_and_incomplete_dirs(tmp_path: Path) -> None:
    good = _fake_venv(tmp_path / "repo" / ".venv")
    link = tmp_path / "linked-venv"
    link.symlink_to(good)
    assert resolve_venv(link, []) is None
    assert resolve_venv(tmp_path / "missing", []) is None

    via_linked_parent = tmp_path / "parent-link"
    via_linked_parent.symlink_to(tmp_path / "repo")
    assert resolve_venv(via_linked_parent / ".venv", []) is not None  # parent links are fine

    cfg_link = _fake_venv(tmp_path / "cfglink" / ".venv")
    (cfg_link / "pyvenv.cfg").unlink()
    (cfg_link / "pyvenv.cfg").symlink_to(good / "pyvenv.cfg")
    assert resolve_venv(cfg_link, []) is None

    no_python = _fake_venv(tmp_path / "nopy" / ".venv")
    (no_python / "bin" / "python").unlink()
    assert resolve_venv(no_python, []) is None

    assert resolve_venv(good, [tmp_path / "repo"]) is None  # inside a denied path
    assert resolve_venv(good, [good / "lib" / "secrets"]) is None  # contains a denied path
    assert resolve_venv(Path.home(), []) is None
    assert resolve_venv(Path("/"), []) is None


def test_build_env_path_is_built_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/evil/bin:/usr/bin")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-should-not-leak")
    env = _build_env(tmp_path)
    dirs = env["PATH"].split(os.pathsep)
    assert "/evil/bin" not in dirs
    assert "/usr/bin" in dirs and "/bin" in dirs
    assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    assert "OPENROUTER_API_KEY" not in env


def test_build_env_with_venv_points_python_at_the_workspace(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path / "repo" / ".venv")
    workspace = tmp_path / "wt"
    (workspace / "src").mkdir(parents=True)
    env = _build_env(tmp_path, venv=venv, workspace=workspace)
    assert env["PATH"].split(os.pathsep)[0] == str(venv / "bin")
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PYTHONPATH"] == f"{workspace / 'src'}{os.pathsep}{workspace}"


@pytest.mark.skipif(sandbox.detect() != "seatbelt", reason="requires a working Seatbelt")
async def test_sandboxed_python_uses_repo_venv_and_workspace_code(
    tmp_path: Path, state_dir: Path
) -> None:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "secret.txt").write_text("source-repo-secret", encoding="utf-8")
    venv_mod.create(repo / ".venv", with_pip=False, symlinks=True)
    (repo / ".venv" / "pip.conf").write_text("index-url = https://u:pw@x/\n", encoding="utf-8")
    wt = tmp_path / "worktree"
    (wt / "src" / "pkg").mkdir(parents=True)
    (wt / "src" / "pkg" / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    ws = LocalWorkspace(wt)
    policy = BashPolicy(
        allow_prefixes=("python -c", "cat"),
        allow_unsandboxed=False,
        state_dir=state_dir,
        repo_venv=repo / ".venv",
    )
    bash = Bash(policy)

    out = await bash.run(
        {"command": "python -c 'import pkg, sys; print(pkg.VALUE, sys.prefix)'"}, ws
    )
    assert "exit code: 0" in out
    assert "42" in out
    assert os.path.realpath(repo / ".venv") in out

    # The venv is readable; the rest of the source repo and the venv's pip.conf are not,
    # and nothing in the venv is writable.
    script = (
        "import sys; "
        f"p = {str(repo / 'secret.txt')!r}; c = {str(repo / '.venv' / 'pip.conf')!r}; "
        f"w = {str(repo / '.venv' / 'planted')!r}\n"
        "for label, fn in (('secret', lambda: open(p).read()), ('pipconf', lambda: open(c).read()), "
        "('write', lambda: open(w, 'w').write('x'))):\n"
        "    try:\n        fn(); print(label, 'ALLOWED')\n"
        "    except OSError: print(label, 'denied')\n"
    )
    (wt / "probe.py").write_text(script, encoding="utf-8")
    policy2 = BashPolicy(
        allow_prefixes=("python probe.py",),
        allow_unsandboxed=False,
        state_dir=state_dir,
        repo_venv=repo / ".venv",
    )
    out2 = await Bash(policy2).run({"command": "python probe.py"}, ws)
    assert "secret denied" in out2 and "pipconf denied" in out2 and "write denied" in out2
    assert "ALLOWED" not in out2
    assert not (repo / ".venv" / "planted").exists()


# --------------------------------------------------------------------------- /dev/null redirects


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -5 2>/dev/null",
        "git status 2> /dev/null",
        "pytest -q >/dev/null",
        "git diff --stat 2>/dev/null; git status 2>/dev/null | head -5",
    ],
)
def test_redirect_to_dev_null_is_allowed_when_sandboxed(command: str) -> None:
    validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


@pytest.mark.parametrize(
    "command",
    [
        "git log 2>/dev/null/x",
        "git log 2>/dev/nullx",
        "git log >/dev/null.txt",
        "git log > out.txt",
        "git log 2>/dev/null > out.txt",
        "git log >> /dev/null",
        "git log </dev/null",
        "git log 2>/dev/../etc/passwd",
    ],
)
def test_other_redirects_are_still_rejected_when_sandboxed(command: str) -> None:
    with pytest.raises(PolicyError):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=True)


def test_redirect_to_dev_null_is_rejected_unsandboxed() -> None:
    """Unsandboxed commands run as a bare argv: `2>/dev/null` would become arguments."""
    with pytest.raises(PolicyError, match="redirection"):
        validate_command("git log 2>/dev/null", DEFAULT_BASH_ALLOW, sandboxed=False)


def test_dev_null_redirect_cannot_smuggle_a_disallowed_command() -> None:
    with pytest.raises(PolicyError):
        validate_command("2>/dev/null curl http://x", DEFAULT_BASH_ALLOW, sandboxed=True)
    with pytest.raises(PolicyError):
        validate_command("git log 2>/dev/null && curl http://x", DEFAULT_BASH_ALLOW, sandboxed=True)


def test_dev_null_redirect_never_hides_an_argument_from_validation() -> None:
    assert validate_command("head -n 2 >/dev/null", DEFAULT_BASH_ALLOW, sandboxed=True) == [
        ["head", "-n", "2"]
    ]
    assert validate_command("git log 2>/dev/null", DEFAULT_BASH_ALLOW, sandboxed=True) == [
        ["git", "log", "2"]
    ]


@pytest.mark.parametrize(
    "command",
    ["git log >> x", "git log >| x", "git log &> x", "git log >& x", "git log <> x"],
)
@pytest.mark.parametrize("sandboxed", [True, False])
def test_redirect_operator_variants_are_rejected(command: str, sandboxed: bool) -> None:
    with pytest.raises(PolicyError):
        validate_command(command, DEFAULT_BASH_ALLOW, sandboxed=sandboxed)
