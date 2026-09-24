"""User-facing configuration: state/config paths, defaults, and cwd validation.

The config file (`config.yaml`) is user-edited only: nothing in this codebase
writes it, per SPEC.md's "no config-mutating tools of any kind" rule. Workers
never see this module; it is orchestrator/server-side plumbing.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import yaml

_APP_NAME = "anymodel-subagents"

_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 32
_MIN_TURNS = 1
_MAX_TURNS = 200
_MIN_LIVE_JOBS = 1
_MAX_LIVE_JOBS = 1000
_MIN_MAX_OUTPUT_TOKENS = 256
_MAX_MAX_OUTPUT_TOKENS = 200_000
_MIN_WAIT_S = 5.0
_MAX_WAIT_S = 600.0
_MIN_WEB_CALLS = 1
_MAX_WEB_CALLS = 200
_MIN_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 86400.0

# Command-prefix allowlist for the sandboxed Bash worker tool (see
# tools/bash.py). This is only a second layer behind the OS sandbox -- see
# that module's docstring for the threat model -- but it keeps a worker from
# even attempting anything outside ordinary test/lint/build/inspection
# commands. User-editable via config.yaml's `bash_allow` key; nothing in
# this codebase writes that file (SPEC.md: "no config-mutating tools of any
# kind").
DEFAULT_BASH_ALLOW: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "uv run pytest",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run build",
    "npm run typecheck",
    "npx tsc",
    "npx eslint",
    "npx vitest",
    "npx jest",
    "pnpm test",
    "pnpm run",
    "yarn test",
    "cargo test",
    "cargo build",
    "cargo check",
    "cargo clippy",
    "cargo fmt",
    "go test",
    "go build",
    "go vet",
    "gofmt",
    "make test",
    "make check",
    "make lint",
    "ruff",
    "mypy",
    "pyright",
    "tsc",
    "eslint",
    "git status",
    "git diff",
    "git log",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "rg",
    "find",
    "echo",
    "pwd",
    "true",
)


@dataclass(frozen=True)
class Config:
    default_model: str = "deepseek/deepseek-v4.1-flash"
    max_concurrency: int = 8
    max_turns: int = 40
    timeout_s: float = 900.0
    # Cap on how long one `wait` long-poll may block. The 45 s default exists
    # because Codex CLI kills an MCP tool call after 60 s; clients without such
    # a limit (Claude Code) can raise it to block until jobs finish in one call
    # instead of paying a turn per extra poll.
    max_wait_s: float = 45.0
    max_tasks_per_dispatch: int = 20
    # Per-request output-token cap passed to OpenRouter as `max_tokens` (see openrouter.py).
    # `null` disables the cap entirely.
    max_output_tokens: int | None = 16384
    # Global cap on queued+running jobs at once; `dispatch` rejects a call that
    # would push the total (existing live jobs + this call's tasks) past it.
    # Finished jobs never count against this -- see jobs.py's own, separate
    # in-memory pruning bound for those.
    max_live_jobs: int = 64
    # Spend caps in USD (OpenRouter cost); `null` = no cap. Only this file sets them: no MCP
    # tool can raise or reset a budget. See budget.py.
    budget_per_swarm_usd: float | None = None
    budget_per_day_usd: float | None = None
    allowed_roots: tuple[Path, ...] = ()  # empty = any git work tree (see validate_cwd)
    job_retention_days: int = 7
    bash_allow: tuple[str, ...] = DEFAULT_BASH_ALLOW
    allow_unsandboxed_bash: bool = False
    # For edit+bash jobs, make the source repository's .venv readable (read-only)
    # inside the sandbox with its bin first on PATH, so a worker can run the
    # project's own tests. The path is derived server-side from the job's
    # worktree metadata (the repo the worktree was created from), never from
    # worker or orchestrator input; the Bash tool re-validates it at call time.
    bash_repo_venv: bool = True
    # Role files in <repo>/.workers/ come from the repo being worked on, which may be untrusted.
    allow_project_roles: bool = False
    # Opt-in OpenRouter provider sort (see openrouter.py's `_VALID_PROVIDER_SORT`). `None`
    # (default) sends today's request bodies unchanged. Set turns off OpenRouter's default
    # price-weighted load balancing among ZDR-eligible providers, so it can pick a pricier one.
    provider_sort: str | None = None
    # Web access for `web` mode workers (docs/adr/0001-worker-web-access.md). Off by default;
    # also needs BRAVE_API_KEY in the server env. Only this file can turn it on.
    web_enabled: bool = False
    web_max_calls_per_job: int = 30
    # Extra domains for the web denylist, merged with the bundled and user denylist files.
    web_denylist_extra: tuple[str, ...] = ()


# Field name -> expected type(s), for validating user-supplied config.yaml values.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "default_model": (str,),
    "max_concurrency": (int,),
    "max_turns": (int,),
    "timeout_s": (int, float),
    "max_wait_s": (int, float),
    "max_tasks_per_dispatch": (int,),
    "max_output_tokens": (int, type(None)),
    "max_live_jobs": (int,),
    "budget_per_swarm_usd": (int, float, type(None)),
    "budget_per_day_usd": (int, float, type(None)),
    "allowed_roots": (list,),
    "job_retention_days": (int,),
    "bash_allow": (list,),
    "allow_unsandboxed_bash": (bool,),
    "bash_repo_venv": (bool,),
    "allow_project_roles": (bool,),
    "provider_sort": (str, type(None)),
    "web_enabled": (bool,),
    "web_max_calls_per_job": (int,),
    "web_denylist_extra": (list,),
}

_KNOWN_FIELDS = {f.name for f in fields(Config)}

# Kept in sync with openrouter.py's own `_VALID_PROVIDER_SORT` (defined there too, so that
# module's constructor validates independently of load_config -- defense in depth, not just
# a single choke point).
_VALID_PROVIDER_SORT = ("throughput", "latency", "price")

# Spend caps: same null/int/float typing as above, but out-of-range values are
# rejected rather than clamped -- a silently-raised cap could spend past what
# the user configured. Enforcement lives in budget.py; only this file may set
# the values (no MCP tool can change or reset them).
_BUDGET_KEYS = ("budget_per_swarm_usd", "budget_per_day_usd")

# Global git options applied to every git invocation this module or
# worktree.py makes: hooks and fsmonitor disabled, GPG signing forced off
# (we're an automated committer, not the user), and file:// transport
# restricted to the invoking user's own repos. Defined here because
# validate_cwd runs git too (and must not inherit the server's env), and
# worktree.py imports from this module (not vice versa).
GIT_SAFETY_ARGS = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "protocol.file.allow=user",
]


def git_env() -> dict[str, str]:
    """A scrubbed environment for git: the server process env may hold
    OPENROUTER_API_KEY (and anything else), which must never reach a
    subprocess. Only PATH/HOME/LANG plus git's own no-prompt switches pass.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def state_dir() -> Path:
    """Directory for jobs/transcripts/worktrees; created 0o700 if missing.

    Precedence: $ANYMODEL_STATE_DIR > XDG state dir. An ambient $CLAUDE_PLUGIN_DATA is
    deliberately ignored: it may belong to another plugin. The Claude Code plugin manifest
    passes ANYMODEL_STATE_DIR=${CLAUDE_PLUGIN_DATA} explicitly instead.
    """
    override = os.environ.get("ANYMODEL_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
        path = base / _APP_NAME

    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    return path


def config_path() -> Path:
    """Path to the (user-edited) config.yaml.

    Precedence: $ANYMODEL_CONFIG > XDG config dir.
    """
    override = os.environ.get("ANYMODEL_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / _APP_NAME / "config.yaml"


def _clamp(value: float, lo: float, hi: float) -> float:
    # Typed float for `max_wait_s`; int args (the other clamped fields) still satisfy it.
    return max(lo, min(hi, value))


def _is_finite_number(value: float) -> bool:
    """`math.isfinite`, but safe for an out-of-float-range int (e.g. YAML's `10**400`).

    `math.isfinite`/`float()` raise OverflowError for an int too large to represent as a
    C double; such a value is, for our purposes, exactly as unusable as NaN/inf, so it is
    treated as non-finite rather than left to crash `load_config` with an unhandled error.
    """
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def load_config(path: Path | None = None) -> Config:
    """Load config from `path` (default: `config_path()`).

    Missing file -> defaults. Unknown keys -> ValueError naming them. Values
    are type-checked against the dataclass fields. `max_concurrency` and
    `max_turns` are clamped into their valid ranges rather than rejected.
    `timeout_s` falls back to the dataclass default when non-finite (NaN/inf),
    then is clamped into [10.0, 86400.0]. Budget caps (`budget_per_swarm_usd`,
    `budget_per_day_usd`) accept null, int, or float, and reject anything <= 0
    or non-finite; ints become floats.
    """
    cfg_path = path if path is not None else config_path()

    if not cfg_path.exists():
        return Config()

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if raw is None:
        return Config()
    if not isinstance(raw, dict):
        msg = f"config file must contain a mapping, got {type(raw).__name__}"
        raise ValueError(msg)  # noqa: TRY004 -- ValueError is this module's public contract

    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if unknown:
        raise ValueError(f"unknown config key(s): {', '.join(unknown)}")

    values: dict[str, Any] = {}
    for key, value in raw.items():
        expected = _FIELD_TYPES[key]
        # bool is an int subclass; don't silently accept booleans for int/float fields.
        if isinstance(value, bool) and bool not in expected:
            msg = f"config key {key!r} must be of type {expected}, got bool"
            raise ValueError(msg)
        if not isinstance(value, expected):
            type_names = "/".join(t.__name__ for t in expected)
            msg = f"config key {key!r} must be of type {type_names}, got {type(value).__name__}"
            raise ValueError(msg)  # noqa: TRY004 -- ValueError is this module's public contract
        if key == "allowed_roots":
            roots = []
            for item in value:
                if not isinstance(item, str):
                    msg = "config key 'allowed_roots' must be a list of strings"
                    raise ValueError(msg)  # noqa: TRY004
                roots.append(Path(item).expanduser())
            values[key] = tuple(roots)
        elif key == "bash_allow":
            if not all(isinstance(item, str) for item in value):
                msg = "config key 'bash_allow' must be a list of strings"
                raise ValueError(msg)
            values[key] = tuple(value)
        elif key == "web_denylist_extra":
            if not all(isinstance(item, str) for item in value):
                msg = "config key 'web_denylist_extra' must be a list of strings"
                raise ValueError(msg)
            values[key] = tuple(value)
        elif key == "provider_sort":
            if value is not None and value not in _VALID_PROVIDER_SORT:
                # Bound the echoed value: it's attacker/user-controlled config.yaml text and
                # must never blow up the exception message (e.g. a 200 KB string).
                msg = (
                    f"config key 'provider_sort' must be one of {_VALID_PROVIDER_SORT} "
                    f"or null, got {str(value)[:40]!r}"
                )
                raise ValueError(msg)
            values[key] = value
        elif key in _BUDGET_KEYS:
            # `null` = no cap; ints normalize to float. Rejected (not clamped)
            # when <= 0 or non-finite. Bools never reach here (rejected above).
            if value is not None and (not _is_finite_number(value) or value <= 0):
                # Same bounding as provider_sort above: an int like 10**400 has a
                # multi-hundred-digit repr.
                msg = f"config key {key!r} must be a positive number, got {str(value)[:40]!r}"
                raise ValueError(msg)
            values[key] = None if value is None else float(value)
        else:
            values[key] = value

    cfg = Config(**values)

    max_output_tokens = cfg.max_output_tokens
    if max_output_tokens is not None:
        max_output_tokens = _clamp(
            max_output_tokens, _MIN_MAX_OUTPUT_TOKENS, _MAX_MAX_OUTPUT_TOKENS
        )

    # `timeout_s` is the ceiling every task's own `timeout_s` is clamped against
    # (see jobs.py's `_validate_task`): `min(task_value, cfg.timeout_s)` silently
    # returns `task_value` unclamped when `cfg.timeout_s` is NaN (NaN comparisons
    # are always false), so a non-finite value here would be a cap escape, not
    # just a broken default. Fall back to the dataclass default rather than
    # reject, matching how out-of-range values are handled for the other
    # clamped fields above.
    timeout_s = cfg.timeout_s
    if not _is_finite_number(timeout_s):
        timeout_s = Config.timeout_s
    timeout_s = _clamp(timeout_s, _MIN_TIMEOUT_S, _MAX_TIMEOUT_S)

    return replace(
        cfg,
        max_concurrency=_clamp(cfg.max_concurrency, _MIN_CONCURRENCY, _MAX_CONCURRENCY),
        max_turns=_clamp(cfg.max_turns, _MIN_TURNS, _MAX_TURNS),
        max_live_jobs=_clamp(cfg.max_live_jobs, _MIN_LIVE_JOBS, _MAX_LIVE_JOBS),
        web_max_calls_per_job=_clamp(cfg.web_max_calls_per_job, _MIN_WEB_CALLS, _MAX_WEB_CALLS),
        max_output_tokens=max_output_tokens,
        max_wait_s=_clamp(cfg.max_wait_s, _MIN_WAIT_S, _MAX_WAIT_S),
        timeout_s=timeout_s,
    )


def _is_ancestor(candidate: Path, of: Path) -> bool:
    """True if `candidate` is `of` or a strict ancestor of it."""
    try:
        of.relative_to(candidate)
        return True
    except ValueError:
        return False


def validate_cwd(cwd: str, cfg: Config) -> Path:
    """Validate and resolve a caller-supplied cwd for a worker task.

    Raises ValueError with a short message on any refusal.
    """
    raw = Path(cwd)
    if not raw.is_absolute():
        raise ValueError("cwd must be an absolute path")
    if not raw.exists():
        raise ValueError("cwd does not exist")
    if not raw.is_dir():
        raise ValueError("cwd is not a directory")

    resolved = raw.resolve()

    # A path with a `.git` component points at (or into) git's own metadata
    # for some repo -- an adversarial or mistaken cwd, never a workspace.
    if any(part == ".git" for part in resolved.parts):
        raise ValueError("cwd must not be inside a .git directory")

    if resolved == resolved.anchor or resolved.parent == resolved:
        raise ValueError("cwd must not be the filesystem root")

    home = Path.home().resolve()
    if resolved == home:
        raise ValueError("cwd must not be the user's home directory")
    if _is_ancestor(resolved, home) and resolved != home:
        raise ValueError("cwd must not be an ancestor of the user's home directory")

    try:
        proc = subprocess.run(
            ["git", *GIT_SAFETY_ARGS, "-C", str(resolved), "rev-parse", "--show-toplevel"],
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not check git work tree: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError("cwd is not inside a git work tree")

    if cfg.allowed_roots and not any(
        resolved == root or _is_ancestor(root, resolved)
        for root in (r.resolve() for r in cfg.allowed_roots)
    ):
        raise ValueError("cwd is outside the configured allowed_roots")

    state = state_dir().resolve()
    if resolved == state or _is_ancestor(state, resolved):
        raise ValueError("cwd must not be inside the plugin state dir")

    return resolved
