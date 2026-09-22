"""The `Bash` worker tool: a command-prefix allowlist layered on an OS sandbox.

Threat model (see also sandbox.py): the worker LLM is untrusted. A command
allowlist alone is not containment -- a worker can write an arbitrary file
with `Write` and then run an allowlisted command like `pytest` or `make
test` against it, so the allowlist here is only a *second* layer. Real
containment is the OS sandbox: no network, writes confined to the workspace
+ a scratch tmp dir, reads denied for credential directories. Without a
working sandbox, Bash refuses outright unless the operator has explicitly
opted into `allow_unsandboxed_bash`, in which case only a single simple
command (no shell, no chaining) is ever exec'd directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from anymodel_subagents.tools import sandbox
from anymodel_subagents.types import PolicyError, ToolError, Workspace

# ---------------------------------------------------------------------------
# Command validation (the second layer). Everything here operates on the raw
# command string / its tokens; it never executes anything itself.
# ---------------------------------------------------------------------------

_CHAIN_OPS = frozenset({"&&", "||", ";", "|"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DISALLOWED_LEADERS = frozenset({"eval", "exec", "source", "."})
_FIND_DENIED_FLAGS = frozenset({"-exec", "-execdir", "-delete", "-ok"})
# `--no-index` lets `git diff` compare two arbitrary filesystem paths outside
# any repo; `-O`/`--ext-diff`/`--textconv` all read or execute something
# other than the plain tracked content (an orderfile, a configured external
# diff driver, a configured textconv filter) -- all path/command escapes
# even though `git` itself is allowlisted.
_GIT_DENIED_FLAGS = frozenset(
    {"-c", "--exec-path", "--upload-pack", "-C", "--no-index", "-O", "--ext-diff", "--textconv"}
)
# git subcommand restriction. `git show <rev>:<path>` and `git log -p --all`
# read any revision's content -- including secrets removed from HEAD or
# committed only on another branch -- so `git show` is refused outright and
# `git log` is restricted to a bounded, patch-free form.
_GIT_LOG_FLAGS = frozenset({"--oneline"})
_GIT_LOG_COUNT_RE = re.compile(r"^(?:-\d+|--max-count=\d+)$")
_GIT_DIFF_FLAGS = frozenset({"--stat", "--cached"})
# Commands where an argument that isn't a flag is (or can be) a file path.
# Defense in depth on top of the OS sandbox: even sandboxed, there's no
# reason one of these ever needs to name a path outside the workspace.
_FILE_INSPECTION_LEADERS = frozenset({"cat", "head", "tail", "wc", "grep", "rg", "find", "ls"})

# Bidi-control and invisible-formatting code points used in "Trojan Source"
# -style tricks: they don't change what a real shell executes (POSIX sh only
# treats ASCII characters specially), but they can make a command look
# different than it is wherever it's displayed/logged, so we refuse them
# outright rather than trying to reason about them.
_UNICODE_TRICK_CODEPOINTS = frozenset(
    {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF}
)

DEFAULT_OUTPUT_CAP = 30_000
DEFAULT_TIMEOUT_S = 120
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 600


def _has_disallowed_chars(command: str) -> str | None:
    for ch in command:
        if ch in (" ", "\t"):
            continue
        if ord(ch) < 32 or ord(ch) == 127:
            return f"command contains a control character (0x{ord(ch):02x})"
        if ord(ch) in _UNICODE_TRICK_CODEPOINTS:
            return "command contains disallowed unicode formatting characters"
    return None


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:
        raise PolicyError(f"could not parse command: {exc}") from None


def _split_segments(tokens: list[str], *, sandboxed: bool) -> list[list[str]]:
    """Split `tokens` on chain/pipe operators into per-command segments.

    Rejects backgrounding (`&`) and redirection (`>`, `<`, and their
    variants) unconditionally, except for the `N>&M` fd-duplication form
    (e.g. `2>&1`) and, when sandboxed, a redirect to exactly `/dev/null`:
    both are common in test/lint invocations and never touch a file. Chain/pipe operators (`&&`, `||`, `;`,
    `|`) are only permitted when a sandbox will contain the result; in
    unsandboxed mode the caller must submit exactly one simple command.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        if (
            tok == ">&"
            and current
            and current[-1].isdigit()
            and i + 1 < n
            and tokens[i + 1].isdigit()
        ):
            current.append(tok)
            i += 1
            continue
        if sandboxed and tok == ">" and i + 1 < n and tokens[i + 1] == "/dev/null":
            # `2>/dev/null` / `>/dev/null`: discards output, touches no file. Sandboxed only:
            # there a shell interprets it; unsandboxed commands run as a bare argv.
            # A preceding fd digit (`2>`) stays in the segment as a harmless argument:
            # validation should never see fewer words than the shell does.
            i += 2
            continue
        if tok in _CHAIN_OPS:
            if not sandboxed:
                raise PolicyError(f"command chaining ({tok!r}) requires a sandboxed environment")
            if not current:
                raise PolicyError("empty command segment")
            segments.append(current)
            current = []
            i += 1
            continue
        if tok == "&":
            raise PolicyError("backgrounding a command with '&' is not allowed")
        if "<" in tok or ">" in tok:
            raise PolicyError(f"redirection is not allowed: {tok!r}")
        current.append(tok)
        i += 1

    if current:
        segments.append(current)
    elif segments:
        raise PolicyError("trailing operator with no following command")
    else:
        raise PolicyError("empty command")
    return segments


def _prefix_tokens(prefix: str) -> tuple[str, ...]:
    return tuple(shlex.split(prefix))


def _matches_prefix(segment: list[str], prefix: tuple[str, ...]) -> bool:
    return len(segment) >= len(prefix) and segment[: len(prefix)] == list(prefix)


def _leader_explicitly_allowed_with_slash(leader: str, allow_prefixes: tuple[str, ...]) -> bool:
    return any(_prefix_tokens(p)[0] == leader for p in allow_prefixes if _prefix_tokens(p))


def _looks_like_path_escape(token: str) -> bool:
    """True if `token` looks like it names a path outside the current directory.

    Absolute paths, `~`-relative paths, and any path containing a `..`
    component are all suspect; a plain relative path like `tests/` or
    `a.txt` is not.
    """
    if token.startswith(("/", "~")):
        return True
    return ".." in Path(token).parts


def _resolves_inside_workspace(token: str, workspace: Path) -> bool:
    candidate = Path(token).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        ws_resolved = workspace.resolve()
    except OSError:
        return False
    return resolved == ws_resolved or ws_resolved in resolved.parents


def _check_file_inspection_args(segment: list[str], workspace: Path | None) -> None:
    """Reject a file-inspection command argument that escapes the workspace.

    Defense in depth on top of the OS sandbox (see module docstring): `cat`,
    `head`, `tail`, `wc`, `grep`, `rg`, `find`, and `ls` never need to name a
    path outside the workspace, so an absolute path, a `~`-relative path, or
    a `..` component is refused outright unless it happens to still resolve
    inside the workspace (e.g. `find "$PWD/../repo"` when `$PWD` already is
    the workspace root -- resolved, not just refused on sight).
    """
    if workspace is None:
        return
    for tok in segment[1:]:
        if tok.startswith("-"):
            continue
        if _looks_like_path_escape(tok) and not _resolves_inside_workspace(tok, workspace):
            raise PolicyError(f"path argument escapes the workspace: {tok!r}")


def _validate_git_log(args: list[str]) -> None:
    """`git log` is allowed only as `git log [--oneline] [-n <N> | -<N> | --max-count=<N>]`."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-n":
            if i + 1 < len(args) and args[i + 1].isdigit():
                i += 2
                continue
            raise PolicyError("git log: -n must be followed by a count")
        if tok in _GIT_LOG_FLAGS or tok.isdigit() or _GIT_LOG_COUNT_RE.match(tok):
            i += 1
            continue
        raise PolicyError(
            "git log: only --oneline and a commit count are allowed (no "
            "--format/--pretty, -p/-u/--patch, -G/-S, --follow, --reflog, "
            "--all/--branches/--remotes/--tags, or revision/path arguments)"
        )


def _strip_fd_redirect_words(args: list[str]) -> list[str]:
    """Drop what `_split_segments` leaves of a trailing `2>/dev/null` (`2`) or `2>&1` (`2 >& 1`).

    Those words never reach git as revisions: the shell consumes them as the redirect.
    """
    out = list(args)
    while True:
        if len(out) >= 3 and out[-2] == ">&" and out[-1].isdigit() and out[-3].isdigit():
            del out[-3:]
        elif (
            out
            and out[-1].isdigit()
            and len(out[-1]) == 1
            and (len(out) < 2 or out[-2] not in ("-n", "--max-count"))
        ):
            del out[-1:]
        else:
            return out


def _validate_git_diff(args: list[str], workspace: Path | None) -> None:
    """Only working-tree/index-vs-HEAD diffs: no other revisions, no patch flags."""
    saw_head = False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in _GIT_DIFF_FLAGS:
            i += 1
            continue
        if tok == "HEAD" and not saw_head:
            saw_head = True
            i += 1
            continue
        if tok == "--":
            for path_tok in args[i + 1 :]:
                inside = (
                    _resolves_inside_workspace(path_tok, workspace)
                    if workspace is not None
                    else not _looks_like_path_escape(path_tok)
                )
                if not inside:
                    raise PolicyError(f"git diff: path escapes the workspace: {path_tok!r}")
            return
        raise PolicyError(
            "git diff: only the working tree/index against HEAD is allowed "
            "(optional --stat/--cached, then `-- <paths inside the workspace>`)"
        )


def _validate_segment(
    segment: list[str], allow_prefixes: tuple[str, ...], *, workspace: Path | None
) -> None:
    if not segment:
        raise PolicyError("empty command")
    leader = segment[0]

    if _ENV_ASSIGN_RE.match(leader):
        raise PolicyError(f"environment-variable assignment prefix is not allowed: {leader!r}")
    if leader in _DISALLOWED_LEADERS:
        raise PolicyError(f"{leader!r} is not allowed")
    if "/" in leader and not _leader_explicitly_allowed_with_slash(leader, allow_prefixes):
        raise PolicyError(f"path-qualified command is not allowed: {leader!r}")

    if not any(_matches_prefix(segment, _prefix_tokens(p)) for p in allow_prefixes):
        shown = " ".join(segment[:4])
        raise PolicyError(f"command is not in the allowlist: {shown!r}")

    if leader == "find" and any(tok in _FIND_DENIED_FLAGS for tok in segment):
        raise PolicyError("find: -exec/-execdir/-delete/-ok are not allowed")
    if leader == "git" and any(
        tok in _GIT_DENIED_FLAGS or tok.startswith("-O") for tok in segment[1:]
    ):
        raise PolicyError(
            "git: -c/-C/--exec-path/--upload-pack/--no-index/-O/--ext-diff/--textconv "
            "are not allowed"
        )
    if leader == "git" and any(tok == "--output" or tok.startswith("--output=") for tok in segment):
        raise PolicyError("git: --output is not allowed")
    if leader == "git" and len(segment) > 1:
        subcommand = segment[1]
        if subcommand == "show":
            raise PolicyError("git show is not allowed (it can read any revision of any file)")
        if subcommand == "log":
            _validate_git_log(_strip_fd_redirect_words(segment[2:]))
        elif subcommand == "diff":
            _validate_git_diff(_strip_fd_redirect_words(segment[2:]), workspace)
    if leader == "rg" and any(tok == "--pre" or tok.startswith("--pre=") for tok in segment):
        raise PolicyError("rg --pre is not allowed")
    if leader in _FILE_INSPECTION_LEADERS:
        _check_file_inspection_args(segment, workspace)


def validate_command(
    command: Any,
    allow_prefixes: tuple[str, ...],
    *,
    sandboxed: bool,
    workspace: Path | None = None,
) -> list[list[str]]:
    """Validate `command` against the allowlist, returning its parsed segments.

    `workspace`, when given, enables the file-inspection path-escape check
    (see `_check_file_inspection_args`); it's optional because this function
    is also used to validate commands before a workspace root is known (and
    the OS sandbox is the real boundary regardless).

    Raises PolicyError on any refusal. Never executes anything.
    """
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")

    bad = _has_disallowed_chars(command)
    if bad:
        raise PolicyError(bad)
    if "$(" in command or "`" in command:
        raise PolicyError("command substitution is not allowed")

    tokens = _tokenize(command)
    segments = _split_segments(tokens, sandboxed=sandboxed)
    for segment in segments:
        _validate_segment(segment, allow_prefixes, workspace=workspace)
    return segments


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BashPolicy:
    """Operator-controlled policy for the Bash tool. Never mutated by workers."""

    allow_prefixes: tuple[str, ...]
    allow_unsandboxed: bool
    state_dir: Path
    # Extra read-only directories the sandbox should allow beyond the
    # workspace/tmp and the built-in system/toolchain allowlist (see
    # sandbox.py). Not currently wired up to config.yaml (that file is
    # owned elsewhere and out of scope here) -- this field exists so a
    # future `sandbox_extra_read` config option can be threaded in without
    # another signature change. Combined at call time with the
    # git-worktree directories `compute_git_extra_read` finds for the
    # current workspace.
    extra_read: tuple[Path, ...] = ()
    # Candidate venv OUTSIDE the workspace, set only by trusted server code (jobs.py: the
    # source repo's `.venv` for a worktree job). Never worker or orchestrator input;
    # re-validated by `resolve_venv` on every call.
    repo_venv: Path | None = None


def _clamp_int(value: Any, *, lo: int, hi: int, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer") from None
    return max(lo, min(hi, ivalue))


def _cap_output(text: str, limit: int = DEFAULT_OUTPUT_CAP) -> str:
    if len(text) <= limit:
        return text
    head_len = limit // 2
    tail_len = limit - head_len
    omitted = len(text) - limit
    return f"{text[:head_len]}\n...[{omitted} characters omitted]...\n{text[-tail_len:]}"


# Per-user toolchain bin dirs (all inside sandbox.py's read allowlist) and the system ones.
_HOME_BIN_DIRS: tuple[str, ...] = (
    ".local/bin",
    ".cargo/bin",
    ".pyenv/shims",
    ".asdf/shims",
    ".local/share/mise/shims",
    ".volta/bin",
)
_OPTIONAL_SYSTEM_BIN_DIRS: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")
_SYSTEM_BIN_DIRS: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _is_within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def resolve_venv(candidate: Path | None, deny_read: list[Path]) -> Path | None:
    """The realpath of `candidate` if it is a venv safe to expose read-only, else None.

    Refused: a symlinked venv dir or `pyvenv.cfg` (the sandbox matches realpaths, so a link
    could point the read allowance anywhere), a dir not owned by this user, anything
    overlapping a denied path, and `/` or `$HOME` or an ancestor of it. Never raises.
    """
    if candidate is None:
        return None
    try:
        candidate = Path(os.path.abspath(candidate))
        if candidate.is_symlink() or not candidate.is_dir():
            return None
        real = os.path.realpath(candidate)
        if real != os.path.join(os.path.realpath(candidate.parent), candidate.name):
            return None
        cfg = candidate / "pyvenv.cfg"
        if cfg.is_symlink() or not cfg.is_file():
            return None
        if not (candidate / "bin" / "python").exists():
            return None
        if os.stat(real).st_uid != os.getuid():
            return None
        home = os.path.realpath(Path.home())
        if _is_within(home, real):  # `/`, $HOME itself, or an ancestor of $HOME
            return None
        for denied in deny_read:
            denied_real = os.path.realpath(denied)
            if _is_within(real, denied_real) or _is_within(denied_real, real):
                return None
        return Path(real)
    except (OSError, ValueError):
        return None


def _build_env(
    tmp: Path, *, venv: Path | None = None, workspace: Path | None = None
) -> dict[str, str]:
    """A minimal, explicitly-allowlisted environment.

    Built up field by field from nothing (never `dict(os.environ)`), so a
    secret can't reach a worker's Bash subprocess just because some
    unrelated env var happens to match a naming convention we forgot to
    scrub -- there is nothing to scrub because nothing is copied by default.
    PATH is built, not inherited: the server's own PATH names whatever venv
    launched it, which the sandbox can't read and the project didn't choose.
    """
    home = Path.home()
    candidates: list[str] = []
    if venv is not None:
        candidates.append(str(venv / "bin"))
    toolchain_bin = sandbox.real_toolchain_bin()
    if toolchain_bin is not None:
        candidates.append(str(toolchain_bin))
    candidates += [str(home / rel) for rel in _HOME_BIN_DIRS]
    candidates += list(_OPTIONAL_SYSTEM_BIN_DIRS)
    path_dirs = [d for d in dict.fromkeys(candidates) if os.path.isdir(d)]
    path_dirs += [d for d in _SYSTEM_BIN_DIRS if d not in path_dirs]
    env: dict[str, str] = {
        "PATH": os.pathsep.join(path_dirs),
        "TERM": "dumb",
        "TMPDIR": str(tmp),
        "CI": "1",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
        if workspace is not None:
            # An editable install's .pth names the source checkout, which the sandbox can't
            # read; the code under test is the workspace's.
            roots = [d for d in (workspace / "src", workspace) if d.is_dir()]
            if roots:
                env["PYTHONPATH"] = os.pathsep.join(str(d) for d in roots)
    for name in ("HOME", "LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _make_job_tmp(state_dir: Path) -> Path:
    base = state_dir / "bash-tmp"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = Path(tempfile.mkdtemp(dir=base, prefix="job-"))
    os.chmod(tmp, 0o700)
    return tmp


# ---------------------------------------------------------------------------
# Git worktree extra-read computation.
#
# A worktree workspace's `.git` is a *file* containing a `gitdir:` pointer
# into `<repo>/.git/worktrees/<id>` (not a directory), and that directory in
# turn has a `commondir` file pointing back at the main repo's `.git` (for
# objects/refs/config). Both live outside the worktree checkout the worker
# is confined to, so `git status`/`git diff`/`git log` need them explicitly
# allowed for read -- see sandbox.py's `extra_read` parameter. A plain repo
# where the workspace is a *subdirectory* of the repo root has the same
# problem in miniature: `.git` lives at an ancestor of the workspace, not
# under it.
# ---------------------------------------------------------------------------

_GIT_WALK_LIMIT = 64


def _find_git_entry(start: Path) -> Path | None:
    """Return the nearest `.git` file/dir at or above `start`, else None."""
    cur = start.resolve()
    for _ in range(_GIT_WALK_LIMIT):
        candidate = cur / ".git"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _resolve_relative_to(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def compute_git_extra_read(workspace: Path) -> tuple[Path, ...]:
    """Directories outside `workspace` that git needs to read, for `sandbox.wrap`.

    Never raises: any malformed/unreadable git metadata just means no extra
    directories are added (git itself will then fail inside the sandbox with
    an ordinary error, same as it would outside one).
    """
    git_entry = _find_git_entry(workspace)
    if git_entry is None:
        return ()

    if git_entry.is_dir():
        ws_git = workspace.resolve() / ".git"
        return () if git_entry == ws_git else (git_entry,)

    # `.git` is a file: worktree `gitdir:` pointer.
    try:
        content = git_entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    gitdir_line = next((ln for ln in content.splitlines() if ln.startswith("gitdir:")), None)
    if gitdir_line is None:
        return ()
    gitdir_path = _resolve_relative_to(git_entry.parent, gitdir_line[len("gitdir:") :].strip())

    extra = [gitdir_path]
    try:
        common_value = (
            (gitdir_path / "commondir").read_text(encoding="utf-8", errors="replace").strip()
        )
    except OSError:
        return tuple(extra)
    if common_value:
        extra.append(_resolve_relative_to(gitdir_path, common_value))
    return tuple(extra)


@dataclass(frozen=True)
class _SandboxPrep:
    """Everything `Bash.run` needs from the blocking sandbox preparation."""

    kind: sandbox.SandboxKind | None
    deny_read: list[Path]
    extra_read: tuple[Path, ...]
    venv: Path | None


def _prep_sandbox(
    *,
    state_dir: Path,
    ws_root: Path,
    policy_extra_read: tuple[Path, ...],
    repo_venv: Path | None,
) -> _SandboxPrep:
    """Gather `sandbox.wrap`'s inputs; blocking, so run it off the event loop.

    `sandbox.detect()` spawns a sandbox-exec/bwrap probe subprocess, and
    `default_deny_read`/`compute_git_extra_read`/`resolve_venv` walk the
    filesystem -- together they stalled the loop on every Bash call. Only
    ever called when a sandbox is in play: with no sandbox detected nothing
    else is probed (same as the old inline `if sandboxed:` guard). Errors
    (none of these raise PolicyError) propagate through `to_thread`
    unchanged.
    """
    kind = sandbox.detect()
    if kind is None:
        return _SandboxPrep(kind=None, deny_read=[], extra_read=(), venv=None)
    deny_read = sandbox.default_deny_read(state_dir)
    extra_read = policy_extra_read + compute_git_extra_read(ws_root)
    venv = resolve_venv(repo_venv, deny_read) or resolve_venv(ws_root / ".venv", deny_read)
    return _SandboxPrep(kind=kind, deny_read=deny_read, extra_read=extra_read, venv=venv)


# ---------------------------------------------------------------------------
# Resource limits: a fork bomb or a disk-filling loop should not be able to
# outlast the per-command timeout unchecked. Applied via plain shell
# builtins in a wrapper process rather than a `preexec_fn` (unsafe to
# combine with threads/asyncio's own internals) -- the wrapper sets the
# limits for itself and then `exec`s into the real (possibly
# sandbox-wrapped) command, which inherits them across exec() same as any
# other process attribute POSIX defines that way.
# ---------------------------------------------------------------------------

DEFAULT_FILE_SIZE_LIMIT_MB = 512
DEFAULT_NPROC_MARGIN = 256
DEFAULT_FD_LIMIT = 1024


async def _current_user_proc_count() -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-u",
            str(os.getuid()),
            "-o",
            "pid=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return 0
    return len(stdout.decode("utf-8", "replace").splitlines())


async def _nproc_ceiling(margin: int = DEFAULT_NPROC_MARGIN) -> int:
    """A RLIMIT_NPROC ceiling that stops a fork bomb without capping the user's session.

    RLIMIT_NPROC is per-UID on macOS/BSD, not per process tree: an absolute
    ceiling picked without looking at the machine could starve the
    operator's own unrelated processes on a busy box, or be far too loose on
    a quiet one. Basing it on the live process count for this UID plus a
    fixed margin (default 256, comfortably more than any legitimate
    test/build command's own process fan-out) stops a runaway fork bomb
    while leaving headroom for the rest of the user's session.
    """
    current = await _current_user_proc_count()
    ceiling = current + margin
    try:
        hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
    except (ValueError, OSError):
        hard = resource.RLIM_INFINITY
    if hard != resource.RLIM_INFINITY and hard > 0:
        ceiling = min(ceiling, hard)
    return max(ceiling, margin)


def _wrap_with_resource_limits(
    argv: list[str],
    *,
    nproc_ceiling: int,
    file_size_mb: int = DEFAULT_FILE_SIZE_LIMIT_MB,
    fd_limit: int = DEFAULT_FD_LIMIT,
) -> list[str]:
    """Wrap `argv` in a ulimit-setting shell.

    `argv`'s own elements are passed as positional parameters to the
    wrapper shell (`"$0" "$1" ...`), never spliced into the script text, so
    this cannot reintroduce shell interpretation of the command's own
    arguments -- confirmed empirically (`sh -c 'exec "$0" "$@"' /bin/echo
    '$HOME'` prints the literal string `$HOME`, and a `*` argument is never
    glob-expanded), which matters because the unsandboxed path exists
    specifically to run a single argv-exec'd command with no shell in
    between.
    """
    file_size_blocks = file_size_mb * 1024 * 1024 // 512  # ulimit -f unit is 512-byte blocks
    script = (
        f"ulimit -u {nproc_ceiling} 2>/dev/null; "
        f"ulimit -f {file_size_blocks} 2>/dev/null; "
        f"ulimit -n {fd_limit} 2>/dev/null; "
        'exec "$0" "$@"'
    )
    return ["/bin/sh", "-c", script, *argv]


def _kill_process_group_blocking(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the whole process group, escalate to SIGKILL, and reap it.

    Blocking by design -- run via `asyncio.to_thread` (see `_kill_process_group`).
    `proc.wait()` is safe to call here even while another thread is blocked
    inside this same `Popen`'s `communicate()` (e.g. the command's own
    in-flight call, abandoned when its awaiting task was cancelled):
    `subprocess.Popen` guards its wait/poll bookkeeping with an internal lock
    for exactly this kind of concurrent access, so this never double-reaps or
    races on `returncode`.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
            proc.wait(timeout=3)


async def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    await asyncio.to_thread(_kill_process_group_blocking, proc)


# ---------------------------------------------------------------------------
# Surviving-descendant reaping.
#
# `_kill_process_group` alone is not enough on the *success* path: an
# allowlisted command (e.g. `python3 script.py`) can spawn a child with
# `start_new_session=True` and redirected fds, and that child calls setsid(),
# leaving the process group -- so `killpg` never sees it, and once the parent
# exits and is reaped the orphan is reparented away (its ppid no longer leads
# back to the launched pid, so it can't even be identified after the fact).
# The fix is to track descendants while they can still be identified: sample
# `ps -axo pid=,ppid=` on a short interval while the command runs, collecting
# every pid whose parent chain leads to the launched pid. When the call
# finishes -- success, failure, timeout, or cancel -- those tracked pids are
# killed along with the process group. All best-effort: never raise.
# ---------------------------------------------------------------------------

_PS_SAMPLE_INTERVAL_S = 0.5


async def _sample_descendants(root_pid: int) -> dict[int, str]:
    """{pid: start time} of every process whose parent chain leads to `root_pid`.

    The start time (`ps` lstart) is the process's identity: a pid alone can be
    reused by an unrelated process of the user between sampling and killing.
    Best-effort: any failure to run or parse `ps` yields an empty dict.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-axo",
            "pid=,ppid=,lstart=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        finally:
            with contextlib.suppress(ProcessLookupError):
                if proc.returncode is None:
                    proc.kill()
    except (OSError, TimeoutError):
        return {}
    parents: dict[int, int] = {}
    started: dict[int, str] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parents[pid] = ppid
        started[pid] = parts[2].strip()
    descendants: set[int] = set()
    frontier = {pid for pid, ppid in parents.items() if ppid == root_pid and pid != root_pid}
    while frontier:
        descendants |= frontier
        frontier = {
            pid for pid, ppid in parents.items() if ppid in frontier and pid not in descendants
        }
    descendants -= {0, 1, os.getpid(), root_pid}
    return {pid: started[pid] for pid in descendants}


async def _sample_by_env_marker(marker: str) -> dict[int, str]:
    """{pid: start time} of the user's processes whose environment mentions `marker`.

    Catches what parent-chain sampling cannot: a child that detached (setsid) and whose
    parent exited between two samples is reparented to pid 1, but still carries the
    call's unique TMPDIR. A child that also scrubs its environment escapes this on
    macOS (documented residual; on Linux bwrap's pid namespace dies with the call).
    """
    if len(marker) < 8:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps",
            # Show each process's environment: BSD ps spells it -E, procps spells it `e`.
            *(["-axEww"] if sys.platform == "darwin" else ["axeww"]),
            "-o",
            "pid=,lstart=,command=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return {}
    out: dict[int, str] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit() or marker not in parts[6]:
            continue
        pid = int(parts[0])
        if pid not in (0, 1, os.getpid()):
            out[pid] = " ".join(parts[1:6])
    return out


async def _process_start_times(pids: set[int]) -> dict[int, str]:
    """Current {pid: start time} for the given pids that still exist."""
    if not pids:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-o",
            "pid=,lstart=",
            "-p",
            ",".join(str(p) for p in sorted(pids)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return {}
    out: dict[int, str] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out[int(parts[0])] = parts[1].strip()
    return out


async def _track_descendants(proc: subprocess.Popen[bytes], into: dict[int, str]) -> None:
    """Sample the process table while the command runs, so a descendant is
    identified *before* its parent exits and reparenting hides it.
    Cancelled by the caller once the command is finished; never raises.
    """
    try:
        while True:
            into.update(await _sample_descendants(proc.pid))
            await asyncio.sleep(_PS_SAMPLE_INTERVAL_S)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - best-effort: losing the tracker must not fail the call
        return


async def _kill_process_tree(
    proc: subprocess.Popen[bytes], tracked: dict[int, str], marker: str = ""
) -> None:
    """Kill the process group plus every descendant that escaped it (setsid).

    Best-effort by design: runs after every Bash call (success included) and
    must never be the thing that makes a finished command fail. A tracked pid
    is signalled only while its start time still matches what was sampled --
    never a recycled pid that now belongs to something else.
    """
    with contextlib.suppress(Exception):
        await _kill_process_group(proc)
    victims = dict(tracked)
    with contextlib.suppress(Exception):
        # One more sample: children still parented to the (possibly just-exited) main process.
        victims.update(await _sample_descendants(proc.pid))
    with contextlib.suppress(Exception):
        victims.update(await _sample_by_env_marker(marker))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not victims:
            return
        try:
            alive = await _process_start_times(set(victims))
        except Exception:  # noqa: BLE001 - best-effort
            return
        victims = {pid: st for pid, st in victims.items() if alive.get(pid) == st}
        for pid in victims:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        if sig == signal.SIGTERM and victims:
            await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Tool description: states the rules validate_command (above) actually
# enforces plus the actual allowlist, so the worker model sees them up front
# instead of burning turns probing. Pure text; it never changes validation.
# ---------------------------------------------------------------------------


def _tool_description(allow_prefixes: tuple[str, ...]) -> str:
    """The per-instance Bash description: real rules + real allowlist."""
    shown = allow_prefixes[:60]
    more = len(allow_prefixes) - len(shown)
    listing = ", ".join(shown)
    if more:
        listing += f", ... ({more} more)"
    rules = (
        "Run a shell command with the workspace root as the current directory: use relative "
        "paths; do not `cd` and do not use absolute paths. Sandboxed: no network, writes only "
        "in the workspace and a temp dir, most of the filesystem outside it is unreadable. A "
        "command must START with one of the allowed prefixes. No output redirection (`>`) "
        "except `2>&1` and `2>/dev/null`, no "
        "`VAR=value` prefixes, no path-qualified commands, no command substitution, no "
        "backgrounding. Chaining/piping is allowed only between allowlisted commands. Do not "
        "probe for interpreters or tools (`which`, `python -c`, `env`): if an allowed test "
        "command fails to start, say so in your report and carry on by reading the code "
        "instead of retrying variants. Git: no `git show`; `git log` only `--oneline`/`-n <N>`; "
        "`git diff` only against HEAD (optionally `-- <paths>`). "
        "If the project has a `.venv`, `python` and `pytest` "
        "on PATH are that venv's and imports resolve to the workspace's code; `uv run` cannot "
        "work inside the sandbox."
    )
    return f"{rules}\nAllowed command prefixes: {listing}"


class Bash:
    """Run a shell command inside an OS sandbox, subject to a prefix allowlist."""

    name = "Bash"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": (
                "Run a shell command in the workspace. Sandboxed: no network access, "
                "writes confined to the workspace and a scratch temp directory. Only "
                "commands matching an operator-configured allowlist (test/lint/build "
                "tools, read-only git/file inspection) may run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120, max 600).",
                    },
                    "description": {
                        "type": "string",
                        "description": "A short, human-readable description of what this does.",
                    },
                },
                "required": ["command"],
            },
        },
    }

    def __init__(self, policy: BashPolicy) -> None:
        # Per-instance schema: the description states the actual rules and this
        # policy's actual allowlist. The class-level schema stays generic, so it
        # is deep-copied rather than mutated in place (no shared mutation).
        self.policy = policy
        self.schema = copy.deepcopy(type(self).schema)
        self.schema["function"]["description"] = _tool_description(policy.allow_prefixes)

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        command = args.get("command")
        timeout = _clamp_int(
            args.get("timeout"),
            lo=MIN_TIMEOUT_S,
            hi=MAX_TIMEOUT_S,
            default=DEFAULT_TIMEOUT_S,
            name="timeout",
        )

        # Off the loop: `detect()` probes for a sandbox with a subprocess and
        # the profile inputs below walk the filesystem (see `_prep_sandbox`);
        # inline they stalled the loop on every Bash call. Exceptions still
        # propagate through `to_thread` unchanged.
        prep = await asyncio.to_thread(
            _prep_sandbox,
            state_dir=self.policy.state_dir,
            ws_root=ws.root,
            policy_extra_read=self.policy.extra_read,
            repo_venv=self.policy.repo_venv,
        )
        kind = prep.kind
        sandboxed = kind is not None
        if not sandboxed and not self.policy.allow_unsandboxed:
            raise PolicyError(
                "Bash is unavailable: no OS sandbox (Seatbelt or bwrap) was detected on "
                "this machine, and allow_unsandboxed_bash is not enabled in config.yaml."
            )

        segments = validate_command(
            command, self.policy.allow_prefixes, sandboxed=sandboxed, workspace=ws.root
        )
        assert isinstance(command, str)  # validate_command already enforced this

        tmp = _make_job_tmp(self.policy.state_dir)
        try:
            env = _build_env(tmp)
            if sandboxed:
                deny_read = prep.deny_read
                extra_read = prep.extra_read
                venv = prep.venv
                if venv is not None:
                    env = _build_env(tmp, venv=venv, workspace=ws.root)
                    deny_read = [*deny_read, venv / "pip.conf"]  # may hold index credentials
                    if not _is_within(str(venv), os.path.realpath(ws.root)):
                        extra_read = (*extra_read, venv)
                # Off the loop: `wrap` (its bwrap profile) walks the whole
                # workspace for secret files -- seconds on a large checkout,
                # which would stall every concurrent MCP call and running job.
                # Exceptions (PolicyError) propagate through `to_thread` unchanged.
                argv = await asyncio.to_thread(
                    sandbox.wrap,
                    ["/bin/sh", "-c", command],
                    workspace=ws.root,
                    tmp=tmp,
                    deny_read=deny_read,
                    extra_read=extra_read,
                    kind=kind,
                )
            else:
                if len(segments) != 1:
                    raise PolicyError("multiple commands require a sandboxed environment")
                argv = segments[0]
            ceiling = await _nproc_ceiling()
            argv = _wrap_with_resource_limits(argv, nproc_ceiling=ceiling)
            return await self._exec(argv, cwd=ws.root, env=env, timeout=timeout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    async def _exec(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> str:
        # Blocking `subprocess.Popen`/`communicate()` run in worker threads, not
        # `asyncio.create_subprocess_exec`/`proc.communicate()` directly: cancelling an
        # asyncio task while it's inside the latter's subprocess-creation machinery hangs
        # forever on CPython 3.11/3.12 (fixed in 3.13) -- see tests/test_subprocess_cancel.py.
        # A blocking call in a thread has no such hazard: a cancelled `await
        # asyncio.to_thread(...)` just abandons the thread, which runs to completion (or
        # until we kill the process group below) without ever wedging the event loop.
        try:
            proc = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group, so we can kill grandchildren too
            )
        except OSError as exc:
            raise ToolError(f"failed to start command: {exc}") from exc

        # Descendant tracking: sample while the main process is alive so a
        # setsid'd grandchild is identified before reparenting hides it.
        tracked: dict[int, str] = {}
        tracker = asyncio.create_task(_track_descendants(proc, tracked))
        timed_out = False
        try:
            try:
                stdout, _ = await asyncio.to_thread(proc.communicate, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                await _kill_process_group(proc)
                stdout = b""
                # The prior to_thread call already returned (with TimeoutExpired), so no other
                # thread is touching this Popen -- safe to call communicate() again to drain
                # whatever output is left buffered in the now-closed pipes.
                with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
                    stdout, _ = await asyncio.to_thread(proc.communicate, timeout=5)
            except asyncio.CancelledError:
                # The `communicate()` call above may still be running in its own thread (the
                # cancellation only abandoned *our* await of it) -- kill the process group so
                # that thread's read unblocks and it can finish on its own; do NOT call
                # communicate() again ourselves, since calling it concurrently from two threads
                # on the same Popen is unsafe. Always re-raise: cancellation must propagate.
                await _kill_process_group(proc)
                raise

            text = _cap_output(stdout.decode("utf-8", errors="replace"))
            if timed_out:
                return f"exit code: -1 (timed out after {timeout}s; process group killed)\n{text}"
            return f"exit code: {proc.returncode}\n{text}"
        finally:
            # Every exit path (success, failure, timeout, cancel): nothing the command
            # started may outlive the call -- a detached child would race later file-tool
            # calls and the finalize policy scan.
            tracker.cancel()
            with contextlib.suppress(Exception):
                await _kill_process_tree(proc, dict(tracked), env.get("TMPDIR", ""))
