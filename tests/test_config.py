"""Tests for config.py: defaults, validation, clamps, env precedence, and
validate_cwd's refusal rules.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest
import yaml

from anymodel_subagents.config import (
    DEFAULT_BASH_ALLOW,
    Config,
    config_path,
    load_config,
    state_dir,
    validate_cwd,
)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANYMODEL_STATE_DIR",
        "CLAUDE_PLUGIN_DATA",
        "XDG_STATE_HOME",
        "ANYMODEL_CONFIG",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Config defaults / load_config
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    cfg = Config()
    assert cfg.default_model == "deepseek/deepseek-v4.1-flash"
    assert cfg.max_concurrency == 8
    assert cfg.max_turns == 40
    assert cfg.timeout_s == 900.0
    assert cfg.max_tasks_per_dispatch == 20
    assert cfg.allowed_roots == ()
    assert cfg.job_retention_days == 7
    assert cfg.bash_allow == DEFAULT_BASH_ALLOW
    assert cfg.allow_unsandboxed_bash is False
    assert cfg.bash_repo_venv is True
    assert cfg.max_live_jobs == 64
    assert cfg.provider_sort is None


def test_default_bash_allow_covers_common_test_and_git_commands() -> None:
    for expected in ("pytest", "git status", "git diff", "ruff", "make test", "ls", "echo"):
        assert expected in DEFAULT_BASH_ALLOW


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg == Config()


def test_load_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert load_config(path) == Config()


def test_load_config_unknown_key_raises_and_names_it(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bogus_key: 1\nother_bogus: 2\n")
    with pytest.raises(ValueError) as exc_info:
        load_config(path)
    assert "bogus_key" in str(exc_info.value)
    assert "other_bogus" in str(exc_info.value)


def test_load_config_wrong_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("default_model: 5\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_bool_rejected_for_int_field(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_concurrency: true\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_not_a_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- 1\n- 2\n")
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(100, 32), (0, 1), (-5, 1), (32, 32), (1, 1)],
)
def test_load_config_clamps_max_concurrency(tmp_path: Path, value: int, expected: int) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"max_concurrency: {value}\n")
    assert load_config(path).max_concurrency == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(500, 200), (0, 1), (-1, 1), (200, 200), (1, 1)],
)
def test_load_config_clamps_max_turns(tmp_path: Path, value: int, expected: int) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"max_turns: {value}\n")
    assert load_config(path).max_turns == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5000, 1000), (0, 1), (-1, 1), (1000, 1000), (1, 1), (64, 64)],
)
def test_load_config_clamps_max_live_jobs(tmp_path: Path, value: int, expected: int) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"max_live_jobs: {value}\n")
    assert load_config(path).max_live_jobs == expected


def test_load_config_max_live_jobs_wrong_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_live_jobs: true\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_allowed_roots_becomes_tuple_of_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("allowed_roots:\n  - /tmp/one\n  - /tmp/two\n")
    cfg = load_config(path)
    assert cfg.allowed_roots == (Path("/tmp/one"), Path("/tmp/two"))


def test_load_config_bash_allow_becomes_tuple(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bash_allow:\n  - pytest\n  - npm test\n")
    cfg = load_config(path)
    assert cfg.bash_allow == ("pytest", "npm test")


def test_load_config_bash_allow_rejects_non_string_items(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bash_allow:\n  - pytest\n  - 5\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_allow_unsandboxed_bash(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("allow_unsandboxed_bash: true\n")
    cfg = load_config(path)
    assert cfg.allow_unsandboxed_bash is True


def test_load_config_allow_unsandboxed_bash_rejects_non_bool(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("allow_unsandboxed_bash: 1\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_bash_repo_venv_false_disables(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bash_repo_venv: false\n")
    assert load_config(path).bash_repo_venv is False


def test_load_config_bash_repo_venv_rejects_non_bool(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bash_repo_venv: 1\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_rejects_provider_prefs(tmp_path: Path) -> None:
    # ZDR routing is not configurable: the key was never wired to the client, and an
    # always-on guarantee is the point. A stale config must fail loudly, not be ignored.
    path = tmp_path / "config.yaml"
    path.write_text("provider_prefs:\n  zdr: false\n")
    with pytest.raises(ValueError, match="provider_prefs"):
        load_config(path)


def test_max_output_tokens_default() -> None:
    assert Config().max_output_tokens == 16384


def test_load_config_max_output_tokens_custom(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_output_tokens: 4096\n")
    assert load_config(path).max_output_tokens == 4096


def test_load_config_max_output_tokens_null_disables_cap(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_output_tokens: null\n")
    assert load_config(path).max_output_tokens is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 256), (200, 256), (256, 256), (500_000, 200_000), (200_000, 200_000), (100_000, 100_000)],
)
def test_load_config_clamps_max_output_tokens(tmp_path: Path, value: int, expected: int) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"max_output_tokens: {value}\n")
    assert load_config(path).max_output_tokens == expected


def test_load_config_max_output_tokens_bool_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_output_tokens: true\n")
    with pytest.raises(ValueError, match="max_output_tokens.*must be"):
        load_config(path)


def test_load_config_max_output_tokens_str_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_output_tokens: '4096'\n")
    with pytest.raises(ValueError, match="max_output_tokens.*must be"):
        load_config(path)


def test_max_wait_s_default() -> None:
    assert Config().max_wait_s == 45.0


def test_load_config_max_wait_s_custom(tmp_path: Path) -> None:
    # A plain YAML int must load: the field accepts int or float.
    path = tmp_path / "config.yaml"
    path.write_text("max_wait_s: 90\n")
    assert load_config(path).max_wait_s == 90.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 5.0), (0, 5.0), (5, 5.0), (600, 600.0), (601, 600.0), (45, 45.0)],
)
def test_load_config_clamps_max_wait_s(tmp_path: Path, value: float, expected: float) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"max_wait_s: {value}\n")
    assert load_config(path).max_wait_s == expected


def test_load_config_max_wait_s_bool_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_wait_s: true\n")
    with pytest.raises(ValueError, match="max_wait_s.*must be"):
        load_config(path)


def test_load_config_max_wait_s_str_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_wait_s: '90'\n")
    with pytest.raises(ValueError, match="max_wait_s.*must be"):
        load_config(path)


# ---------------------------------------------------------------------------
# timeout_s: non-finite falls back to the default, then clamps to
# [10.0, 86400.0] -- this is the ceiling every task's own `timeout_s` is
# clamped against in jobs.py, so a NaN/negative/zero value here must never
# reach jobs.py raw (see test_task_timeout.py's PoC-derived cap-escape note).
# ---------------------------------------------------------------------------


def test_timeout_s_default() -> None:
    assert Config().timeout_s == 900.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".nan", 900.0),  # non-finite -> falls back to the dataclass default
        (".inf", 900.0),
        ("-.inf", 900.0),
        (-5, 10.0),  # finite but out of range -> clamped, not rejected
        (0, 10.0),
        (5, 10.0),
        (10, 10.0),
        (60, 60.0),
        (86400, 86400.0),
        (100000, 86400.0),
    ],
)
def test_load_config_clamps_timeout_s(tmp_path: Path, raw: float | str, expected: float) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"timeout_s: {raw}\n")
    assert load_config(path).timeout_s == expected


def test_load_config_timeout_s_bool_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("timeout_s: true\n")
    with pytest.raises(ValueError, match="timeout_s.*must be"):
        load_config(path)


def test_load_config_timeout_s_str_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("timeout_s: '90'\n")
    with pytest.raises(ValueError, match="timeout_s.*must be"):
        load_config(path)


def test_load_config_timeout_s_leaves_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("timeout_s: .nan\n")
    before = path.read_text()
    load_config(path)
    assert path.read_text() == before


def test_load_config_full_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_model: openai/gpt-9\n"
        "max_concurrency: 4\n"
        "max_turns: 10\n"
        "timeout_s: 60\n"
        "max_tasks_per_dispatch: 5\n"
        "job_retention_days: 1\n"
    )
    cfg = load_config(path)
    assert cfg.default_model == "openai/gpt-9"
    assert cfg.max_concurrency == 4
    assert cfg.max_turns == 10
    assert cfg.timeout_s == 60
    assert cfg.max_tasks_per_dispatch == 5
    assert cfg.job_retention_days == 1


# ---------------------------------------------------------------------------
# state_dir / config_path precedence
# ---------------------------------------------------------------------------


def test_state_dir_prefers_anymodel_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "b"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "c"))
    assert state_dir() == tmp_path / "a"


def test_state_dir_ignores_ambient_claude_plugin_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "b"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "c"))
    assert state_dir() == tmp_path / "c" / "anymodel-subagents"
    assert not (tmp_path / "b").exists()


def test_state_dir_falls_back_to_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "c"))
    assert state_dir() == tmp_path / "c" / "anymodel-subagents"


def test_state_dir_falls_back_to_home_when_nothing_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    assert state_dir() == fake_home / ".local" / "state" / "anymodel-subagents"


def test_state_dir_is_created_with_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    target = tmp_path / "state"
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(target))
    result = state_dir()
    assert result.is_dir()
    assert (result.stat().st_mode & 0o777) == 0o700


def test_config_path_prefers_anymodel_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANYMODEL_CONFIG", str(tmp_path / "cfg.yaml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgcfg"))
    assert config_path() == tmp_path / "cfg.yaml"


def test_config_path_falls_back_to_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgcfg"))
    assert config_path() == tmp_path / "xdgcfg" / "anymodel-subagents" / "config.yaml"


def test_config_path_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config_path() == tmp_path / "home" / ".config" / "anymodel-subagents" / "config.yaml"


# ---------------------------------------------------------------------------
# validate_cwd
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_cwd's final check calls state_dir(); make sure that never
    touches the real, ambient state dir (e.g. a real CLAUDE_PLUGIN_DATA set
    in the developer's own environment) for any test in this module.
    Tests that specifically exercise the state-dir refusal override this.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "_unused_state"))


@pytest.fixture
def cfg() -> Config:
    return Config()


def test_validate_cwd_refuses_filesystem_root(cfg: Config) -> None:
    with pytest.raises(ValueError):
        validate_cwd("/", cfg)


def test_validate_cwd_refuses_relative_path(cfg: Config) -> None:
    with pytest.raises(ValueError):
        validate_cwd("relative/path", cfg)


def test_validate_cwd_refuses_nonexistent_path(tmp_path: Path, cfg: Config) -> None:
    with pytest.raises(ValueError):
        validate_cwd(str(tmp_path / "nope"), cfg)


def test_validate_cwd_refuses_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(ValueError):
        validate_cwd(str(fake_home), cfg)


def test_validate_cwd_refuses_ancestor_of_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    fake_home = tmp_path / "a" / "b" / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    ancestor = tmp_path / "a"
    with pytest.raises(ValueError):
        validate_cwd(str(ancestor), cfg)


def test_validate_cwd_refuses_symlink_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    link = tmp_path / "link_to_home"
    link.symlink_to(fake_home)
    with pytest.raises(ValueError):
        validate_cwd(str(link), cfg)


def test_validate_cwd_refuses_non_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        validate_cwd(str(plain), cfg)


def test_validate_cwd_accepts_repo_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    subdir = repo / "src"
    subdir.mkdir()
    result = validate_cwd(str(subdir), Config())
    assert result == subdir.resolve()


def test_validate_cwd_refuses_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    other_root = tmp_path / "other"
    other_root.mkdir()
    cfg = Config(allowed_roots=(other_root,))
    with pytest.raises(ValueError):
        validate_cwd(str(repo), cfg)


def test_validate_cwd_accepts_inside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    cfg = Config(allowed_roots=(repo,))
    assert validate_cwd(str(repo), cfg) == repo.resolve()


def test_validate_cwd_refuses_inside_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    state_target = repo / ".state"
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(state_target))
    resolved_state = state_dir()  # creates it, also inside the git work tree
    with pytest.raises(ValueError):
        validate_cwd(str(resolved_state), Config())


def test_validate_cwd_refuses_subdir_of_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    state_target = repo / ".state"
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(state_target))
    resolved_state = state_dir()
    nested = resolved_state / "worktrees"
    nested.mkdir(parents=True)
    with pytest.raises(ValueError):
        validate_cwd(str(nested), Config())


# ---------------------------------------------------------------------------
# budget caps (budget_per_swarm_usd / budget_per_day_usd)
# ---------------------------------------------------------------------------


def test_budget_caps_default_to_no_cap() -> None:
    cfg = Config()
    assert cfg.budget_per_swarm_usd is None
    assert cfg.budget_per_day_usd is None
    # A missing config file must leave both caps unset rather than erroring.
    assert load_config(Path("/nonexistent/config.yaml")).budget_per_day_usd is None


@pytest.mark.parametrize("key", ["budget_per_swarm_usd", "budget_per_day_usd"], ids=str)
@pytest.mark.parametrize(("raw", "expected"), [(5, 5.0), (0.25, 0.25), (None, None), (12.5, 12.5)])
def test_load_config_budget_accepts_null_int_and_float(
    tmp_path: Path, key: str, raw: float | None, expected: float | None
) -> None:
    # Ints must come back as floats: the cap feeds float math in budget.py.
    path = tmp_path / "config.yaml"
    path.write_text(f"{key}: {raw}\n" if raw is not None else f"{key}: null\n")
    assert getattr(load_config(path), key) == expected
    if expected is not None:
        assert isinstance(getattr(load_config(path), key), float)


@pytest.mark.parametrize("key", ["budget_per_swarm_usd", "budget_per_day_usd"], ids=str)
@pytest.mark.parametrize("raw", ["true", "'5.0'", "[]"])
def test_load_config_budget_rejects_bool_and_wrong_types(
    tmp_path: Path, key: str, raw: str
) -> None:
    # bool must be rejected even though it is an int subclass: `true` is not a
    # cap of $1.
    path = tmp_path / "config.yaml"
    path.write_text(f"{key}: {raw}\n")
    with pytest.raises(ValueError, match=key):
        load_config(path)


@pytest.mark.parametrize("key", ["budget_per_swarm_usd", "budget_per_day_usd"], ids=str)
@pytest.mark.parametrize("raw", ["0", "-1", "-0.5", ".nan", ".inf", "-.inf"])
def test_load_config_budget_rejects_non_positive_and_non_finite(
    tmp_path: Path, key: str, raw: str
) -> None:
    # Zero, negative, NaN and infinite caps are rejected outright instead of
    # clamped: a clamped cap could spend past what the user configured.
    path = tmp_path / "config.yaml"
    path.write_text(f"{key}: {raw}\n")
    with pytest.raises(ValueError, match=key):
        load_config(path)


def test_load_config_budget_both_keys_together(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("budget_per_swarm_usd: 2\nbudget_per_day_usd: 10.5\n")
    cfg = load_config(path)
    assert cfg.budget_per_swarm_usd == 2.0
    assert cfg.budget_per_day_usd == 10.5


# ---------------------------------------------------------------------------
# provider_sort
# ---------------------------------------------------------------------------


def test_provider_sort_default_is_none() -> None:
    assert Config().provider_sort is None


def test_load_config_missing_provider_sort_is_none(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("default_model: openai/gpt-9\n")
    assert load_config(path).provider_sort is None


def test_load_config_provider_sort_null_is_none(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: null\n")
    assert load_config(path).provider_sort is None


@pytest.mark.parametrize("value", ["throughput", "latency", "price"])
def test_load_config_provider_sort_accepts_valid_values(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"provider_sort: {value}\n")
    assert load_config(path).provider_sort == value


@pytest.mark.parametrize("value", ["cheapest", "PRICE", "Throughput", " price", "price "])
def test_load_config_provider_sort_rejects_other_strings(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"provider_sort: {value!r}\n")
    with pytest.raises(ValueError, match="provider_sort"):
        load_config(path)


def test_load_config_provider_sort_rejects_empty_string(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: ''\n")
    with pytest.raises(ValueError, match="provider_sort"):
        load_config(path)


def test_load_config_provider_sort_rejects_wrong_type(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: 1\n")
    with pytest.raises(ValueError, match="provider_sort.*must be"):
        load_config(path)


def test_load_config_provider_sort_rejects_bool(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: true\n")
    with pytest.raises(ValueError, match="provider_sort.*must be"):
        load_config(path)


def test_load_config_provider_sort_rejects_list(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort:\n  - price\n")
    with pytest.raises(ValueError, match="provider_sort.*must be"):
        load_config(path)


def test_load_config_provider_sort_leaves_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: price\n")
    before = path.read_text()
    load_config(path)
    assert path.read_text() == before


def test_load_config_provider_sort_rejects_huge_value_with_bounded_message(
    tmp_path: Path,
) -> None:
    # Regression: the rejected value used to be echoed into the ValueError untruncated.
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: " + "a" * 200_000 + "\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    assert len(str(excinfo.value)) < 200


@pytest.mark.parametrize(
    "value",
    [
        "throughput\t",  # tab-padded
        "throughput\0",  # NUL-suffixed
        "thrоughput",  # cyrillic 'o' homoglyph
    ],
)
def test_load_config_provider_sort_rejects_lookalike_values(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"provider_sort: {value!r}\n")
    with pytest.raises(ValueError, match="provider_sort"):
        load_config(path)


def test_load_config_rejects_python_object_apply_tag(tmp_path: Path) -> None:
    # yaml.safe_load must refuse this tag outright -- it never reaches Config construction.
    path = tmp_path / "config.yaml"
    path.write_text("provider_sort: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(yaml.YAMLError):
        load_config(path)


def test_load_config_budget_rejects_huge_value_with_bounded_message(tmp_path: Path) -> None:
    # Regression: math.isfinite(10**400) raises OverflowError (int too large for a C double),
    # which used to crash load_config instead of raising the normal ValueError. Also covers
    # the same echo-bounding pattern as provider_sort, applied to the budget keys.
    path = tmp_path / "config.yaml"
    path.write_text(f"budget_per_day_usd: {10**400}\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    assert len(str(excinfo.value)) < 200


def test_load_config_timeout_s_huge_value_falls_back_to_default(tmp_path: Path) -> None:
    # Regression: same OverflowError as above, but timeout_s's semantics are "fall back to
    # the default when non-finite" (see load_config's docstring), not reject -- a huge int
    # must land there too, not crash load_config, and the result must be a finite float.
    path = tmp_path / "config.yaml"
    path.write_text(f"timeout_s: {10**400}\n")
    cfg = load_config(path)
    assert cfg.timeout_s == Config.timeout_s
    assert math.isfinite(cfg.timeout_s)


def test_load_config_budget_leaves_file_untouched(tmp_path: Path) -> None:
    # Nothing in config.py may write the config file (user-edited only).
    path = tmp_path / "config.yaml"
    path.write_text("budget_per_day_usd: 5\n")
    before = path.read_text()
    load_config(path)
    assert path.read_text() == before
