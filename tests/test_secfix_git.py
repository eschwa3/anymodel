"""Regression tests for the git hardening fixes: scrubbed env + safety args
on config.validate_cwd's git call, refusal of `.git`-component cwds, and the
local-git-config audit create_worktree runs before checkout.

Config-related tests here live next to the worktree tests because both fixes
share one theme (git invoked on behalf of a caller) and one test file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from anymodel_subagents.config import Config, validate_cwd
from anymodel_subagents.worktree import WorktreeError, create_worktree


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(root: Path, *, commit: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    if commit:
        (root / "README.md").write_text("hello\n")
        _git("add", "-A", cwd=root)
        _git("commit", "-q", "-m", "init", cwd=root)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# Fix A: validate_cwd's git invocation
# ---------------------------------------------------------------------------


def _capture_run(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(argv), kwargs.get("env")))
        return real_run(
            argv,
            env=kwargs.get("env"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    monkeypatch.setattr("anymodel_subagents.config.subprocess.run", fake_run)
    return calls


def test_validate_cwd_git_call_is_scrubbed_and_hardened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The env passed to git must not carry the server's OPENROUTER_API_KEY,
    and argv must include the hooks/fsmonitor safety overrides."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-decoy-never-leak")
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "state"))

    calls = _capture_run(monkeypatch)
    resolved = validate_cwd(str(repo), Config())

    assert resolved == repo.resolve()
    argv, env = calls[0]
    assert env is not None
    assert "OPENROUTER_API_KEY" not in env
    assert "sk-decoy-never-leak" not in str(env)
    assert "core.hooksPath=/dev/null" in argv
    assert "core.fsmonitor=false" in argv


def test_validate_cwd_refuses_dot_git_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "state"))

    with pytest.raises(ValueError, match=r"\.git"):
        validate_cwd(str(repo / ".git"), Config())


# ---------------------------------------------------------------------------
# Fix B: local git config audit before creating a worktree
# ---------------------------------------------------------------------------


async def test_create_worktree_refuses_filter_clean_config(
    repo: Path, state: Path, tmp_path: Path
) -> None:
    """A repo whose local config wires a clean filter to an arbitrary command
    must be refused before anything is created; the command must never run."""
    (repo / ".gitattributes").write_text("*.txt filter=pwn\n")
    beacon = tmp_path / "beacon"
    _git("config", "filter.pwn.clean", f"touch {beacon}", cwd=repo)

    with pytest.raises(WorktreeError) as excinfo:
        await create_worktree(repo, "job-pwn000", state)

    # The error names the config key only -- never the value.
    assert "filter.pwn.clean" in str(excinfo.value)
    assert "beacon" not in str(excinfo.value)
    assert not beacon.exists()
    assert not (state / "worktrees" / "job-pwn000").exists()


async def test_create_worktree_allows_stock_git_lfs_filters(repo: Path, state: Path) -> None:
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    _git("config", "filter.lfs.clean", "git-lfs clean -- %f", cwd=repo)
    _git("config", "filter.lfs.smudge", "git-lfs smudge -- %f", cwd=repo)
    _git("config", "filter.lfs.process", "git-lfs filter-process", cwd=repo)

    info = await create_worktree(repo, "job-lfs001", state)
    assert info.repo_root == repo.resolve()


@pytest.mark.parametrize(
    ("config_args", "expected_key"),
    [
        (("core.fsmonitor", "true"), "core.fsmonitor"),
        (("core.sshcommand", "ssh -i /etc/passwd"), "core.sshcommand"),
        (("core.hookspath", "/tmp/hooks"), "core.hookspath"),
        (("diff.pwn.textconv", "cat %f"), "diff.pwn.textconv"),
        (("diff.pwn.command", "cat %f"), "diff.pwn.command"),
        (("merge.pwn.driver", "cat %O %A %B"), "merge.pwn.driver"),
        (("core.fsmonitor", "0"), None),
        (("core.fsmonitor", "false"), None),
        (("diff.safe.textconv", None), None),  # unset -> absent, not refused
    ],
)
async def test_create_worktree_local_config_keys(
    repo: Path, state: Path, config_args: tuple[str, str | None], expected_key: str | None
) -> None:
    key, value = config_args
    if value is not None:
        _git("config", key, value, cwd=repo)

    if expected_key is None:
        info = await create_worktree(repo, "job-cfg-ok1", state)
        assert info.repo_root == repo.resolve()
    else:
        with pytest.raises(WorktreeError, match=expected_key.replace(".", r"\.")):
            await create_worktree(repo, "job-cfg-no2", state)
        assert not (state / "worktrees" / "job-cfg-no2").exists()


async def test_create_worktree_refuses_local_config_include(tmp_path):
    # An included file could define filter.*.clean without showing up in `--list --local`.
    import subprocess

    from anymodel_subagents import worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q"],
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        ["git", "config", "include.path", "../evil.gitconfig"],
    ):
        subprocess.run(argv, cwd=repo, check=True)  # noqa: ASYNC221
    with pytest.raises(worktree.WorktreeError, match="include.path"):
        await worktree._refuse_unsafe_local_config(repo)
