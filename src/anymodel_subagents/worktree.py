"""Git worktree isolation for `isolation: "worktree"` jobs, plus the
in-place snapshot/diff helpers used when a job edits the caller's cwd
directly.

Threat model: every git invocation here runs on behalf of an untrusted
worker's *outcome* (its diff), not on behalf of the worker itself (the
worker never shells out to git directly -- only the server does, before and
after a job). Still: argv-only exec (no shell), a reduced environment, hooks
disabled, GPG signing disabled, a timeout on every call, and commit messages
sanitized before use.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Shared git hygiene (safety args + scrubbed env) lives in config.py; this
# import is cycle-free (config.py never imports this module) and keeps
# config.validate_cwd's git call under the exact same flags.
from .config import GIT_SAFETY_ARGS as _GIT_SAFETY_ARGS
from .config import git_env as _git_env

_JOB_ID_RE = re.compile(r"^[a-z0-9-]{6,40}$")

_GIT_TIMEOUT_S = 30.0

_WORKER_AUTHOR_NAME = "anymodel-worker"
_WORKER_AUTHOR_EMAIL = "noreply@anymodel.invalid"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_MESSAGE_LEN = 200
_MAX_DIRTY_PATHS = 50


class WorktreeError(Exception):
    """A git operation failed. The message is short and safe to surface."""


@dataclass
class WorktreeInfo:
    repo_root: Path  # toplevel of the caller's repo
    path: Path  # worktree checkout root
    workdir: Path  # path / (caller cwd relative to repo_root) -- worker's workspace root
    branch: str  # "anymodel/<job_id>"
    base_commit: str
    dirty: bool = False  # True if the caller's tree had uncommitted changes not carried in
    # `git status --porcelain` entries for those uncommitted changes (2-char
    # status + " " + repo-relative path, e.g. " M src/a.py", "?? notes.txt"),
    # capped at 50 -- lets the server name what's not visible to the worker
    # instead of just warning that "something" is dirty.
    dirty_paths: list[str] = field(default_factory=list)


@dataclass
class WorktreeOutcome:
    changed_files: list[str] = field(default_factory=list)  # repo-root-relative POSIX paths
    kept: bool = False  # False -> worktree and branch were removed (no changes)
    commit: str | None = None  # commit holding the worker's changes, if kept
    # Paths a sandboxed script wrote outside file-tool policy (denied globs,
    # secret-shaped names, symlinks, setuid/setgid bits) that were reverted to
    # base content (or removed, if new) before the commit above was made.
    policy_reverted_files: list[str] = field(default_factory=list)
    # Short, human-readable reasons for each reversion/flag above, in no
    # particular correspondence to `policy_reverted_files`'s order.
    policy_notes: list[str] = field(default_factory=list)
    # Subset of `changed_files` (post-revert, post-commit) the caller's
    # `is_sensitive` classified as needing deliberate review.
    sensitive_files: list[str] = field(default_factory=list)


def _run_git_blocking(
    argv: list[str], env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """The actual (blocking) git invocation -- always run off the event loop
    via `asyncio.to_thread` (see `_run_git`). Blocking `subprocess.run` has no
    cancellation hazard on any Python version: unlike
    `asyncio.create_subprocess_exec`, there is no event-loop-owned state that
    a cancelled `await` can leave half-constructed. If the awaiting task is
    cancelled, this call is simply abandoned mid-thread and runs to its own
    (short) completion; nothing ever awaits it afterwards.
    """
    return subprocess.run(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


async def _run_git(
    args: list[str],
    *,
    timeout: float = _GIT_TIMEOUT_S,
    extra_env: dict[str, str] | None = None,
) -> str:
    argv = ["git", *_GIT_SAFETY_ARGS, *args]
    env = _git_env()
    if extra_env:
        env.update(extra_env)

    try:
        proc = await asyncio.to_thread(_run_git_blocking, argv, env, timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args[:2])} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise WorktreeError(f"could not run git: {exc}") from exc

    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", "replace").strip()
        short = stderr_text.splitlines()[-1] if stderr_text else f"exit code {proc.returncode}"
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {short[:300]}")

    return proc.stdout.decode("utf-8", "replace")


def _sanitize_message(message: str) -> str:
    msg = _CONTROL_CHARS_RE.sub("", message).strip()
    if not msg:
        msg = "anymodel-worker changes"
    return msg[:_MAX_MESSAGE_LEN]


def _meta_path(worktrees_dir: Path, job_id: str) -> Path:
    return worktrees_dir / f".{job_id}.meta.json"


# Stock git-lfs filter values, which a repo legitimately using LFS carries in
# its local config. Anything else behind a filter/driver key could execute an
# arbitrary command on the host the moment `finalize_worktree` runs
# `git add -A` (clean filters) -- outside any sandbox -- so the repo is
# refused up front.
_STOCK_LFS_FILTER_VALUES = {
    "git-lfs clean -- %f",
    "git-lfs smudge -- %f",
    "git-lfs filter-process",
}


async def _refuse_unsafe_local_config(cwd: Path) -> None:
    """Refuse a repo whose *local* git config would let git execute commands
    on the host. Only the offending config KEY is named in the error, never
    its value (values are untrusted repo content).

    Keys audited: `filter.<name>.clean|smudge|process` (except the stock
    git-lfs values), `diff.<name>.textconv|command`, `merge.<name>.driver`,
    `core.fsmonitor` (any value but false/0/empty), `core.sshcommand`,
    `core.hookspath`.
    """
    try:
        out = await _run_git(["-C", str(cwd), "config", "--list", "--local", "-z"])
    except WorktreeError:
        return  # no/failed local config -- nothing to audit

    for entry in out.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        key = key.lower()
        stripped = value.strip()
        unsafe = (
            (
                key.startswith("filter.")
                and key.rsplit(".", 1)[1] in ("clean", "smudge", "process")
                and stripped not in _STOCK_LFS_FILTER_VALUES
            )
            or (key.startswith("diff.") and key.endswith((".textconv", ".command")))
            or (key.startswith("merge.") and key.endswith(".driver"))
            or (key == "core.fsmonitor" and stripped.lower() not in ("", "false", "0"))
            or key in ("core.sshcommand", "core.hookspath")
            # An include can define any of the above in a file this audit never lists.
            or key == "include.path"
            or key.startswith("includeif.")
        )
        if unsafe:
            raise WorktreeError(
                f"repo local git config defines {key}; refusing to create a worktree from it"
            )


async def create_worktree(cwd: Path, job_id: str, state: Path) -> WorktreeInfo:
    """Create an isolated worktree checked out from the caller's repo HEAD.

    The worktree lives at `state/worktrees/<job_id>` on branch
    `anymodel/<job_id>`. Uncommitted changes in `cwd`'s tree are NOT carried
    into the worktree (git worktrees are always created from a committed
    ref) -- `WorktreeInfo.dirty` reports whether the caller's tree had any,
    so the server can warn.
    """
    if not _JOB_ID_RE.match(job_id):
        raise WorktreeError(f"invalid job_id: {job_id!r}")

    cwd = Path(cwd).resolve()

    await _refuse_unsafe_local_config(cwd)

    try:
        toplevel = await _run_git(["-C", str(cwd), "rev-parse", "--show-toplevel"])
    except WorktreeError as exc:
        raise WorktreeError("cwd is not inside a git work tree") from exc
    repo_root = Path(toplevel.strip()).resolve()

    try:
        head = await _run_git(["-C", str(repo_root), "rev-parse", "HEAD"])
    except WorktreeError as exc:
        raise WorktreeError("repo has no commits; cannot create a worktree") from exc
    base_commit = head.strip()

    status_out = await _run_git(
        ["-C", str(repo_root), "status", "--porcelain=v1", "-z", "--untracked-files=normal"]
    )
    dirty_paths = [f"{status} {path}" for status, path, _orig in _parse_porcelain_z(status_out)][
        :_MAX_DIRTY_PATHS
    ]
    dirty = bool(dirty_paths)

    worktrees_dir = state / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / job_id
    if wt_path.exists():
        raise WorktreeError(f"worktree path already exists: {wt_path}")

    branch = f"anymodel/{job_id}"

    # The add runs on a worker thread (see _run_git): if this task is
    # cancelled while it is in flight, git still finishes creating the
    # worktree dir, the branch and the .git/worktrees admin entry -- while
    # this coroutine unwinds and the meta file below (sweep()'s only handle
    # on all of that) is never written. So run the add as a shielded task
    # and, if we don't get past it and the meta write, wait it out and undo
    # whatever it created before re-raising.
    add_task = asyncio.ensure_future(
        _run_git(["-C", str(repo_root), "worktree", "add", "-b", branch, str(wt_path), base_commit])
    )
    try:
        await asyncio.shield(add_task)

        meta = {"repo_root": str(repo_root), "base_commit": base_commit, "branch": branch}
        _meta_path(worktrees_dir, job_id).write_text(json.dumps(meta), encoding="utf-8")
    except BaseException:
        cleanup = asyncio.ensure_future(
            _abandon_partial_worktree(add_task, repo_root, wt_path, branch, base_commit)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            pass  # re-cancelled mid-cleanup: the shielded cleanup still completes
        raise

    rel = cwd.relative_to(repo_root)
    workdir = wt_path / rel

    return WorktreeInfo(
        repo_root=repo_root,
        path=wt_path,
        workdir=workdir,
        branch=branch,
        base_commit=base_commit,
        dirty=dirty,
        dirty_paths=dirty_paths,
    )


async def _abandon_partial_worktree(
    add_task: asyncio.Task[str], repo_root: Path, wt_path: Path, branch: str, base_commit: str
) -> None:
    """Wait out the shielded in-flight `worktree add`, then best-effort undo
    whatever it created: the worktree dir, its `.git/worktrees` admin entry
    and the `anymodel/<job_id>` branch.

    The add must be waited out first or the removal below could race git and
    lose. Runs as its own task, shielded by the caller from the pending
    cancellation; never raises.
    """
    await asyncio.wait({add_task})
    add_task.exception()  # retrieved so a failed add never logs a warning

    await remove_worktree(
        WorktreeInfo(
            repo_root=repo_root,
            path=wt_path,
            workdir=wt_path,
            branch=branch,
            base_commit=base_commit,
        )
    )


async def _force_remove(path: Path) -> None:
    """Best-effort removal of `path`, whatever it currently is (file/symlink/dir)."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


async def _revert_changed_path(
    worktree_path: Path, base_commit: str, path: str, orig: str | None
) -> None:
    """Undo one changed path in `worktree_path`'s working tree, back to `base_commit`.

    Tracked (modified/deleted) paths are restored via `git checkout` -- which
    also correctly undoes a deletion. A path git has no record of at
    `base_commit` (new/untracked, or the new name of a rename/copy) is
    removed directly instead. For a rename/copy, the new name is removed and
    the original name is restored from base (undoing the rename outright);
    if the original wasn't at base either (a copy of a new file), there's
    nothing to restore and only the removal applies.
    """
    if orig is not None:
        await _force_remove(worktree_path / path)
        try:
            await _run_git(["-C", str(worktree_path), "checkout", base_commit, "--", orig])
        except WorktreeError:
            pass
        return
    try:
        await _run_git(["-C", str(worktree_path), "checkout", base_commit, "--", path])
    except WorktreeError:
        await _force_remove(worktree_path / path)


async def _apply_write_policy(
    info: WorktreeInfo, is_write_denied: Callable[[str], bool]
) -> tuple[list[str], list[str]]:
    """Scan `info.path`'s working tree for policy-violating changes and revert them.

    A sandboxed Bash script (in `edit+bash` mode) can write anywhere the
    sandbox profile permits, entirely bypassing the Edit/Write tools' own
    path policy -- this is the backstop that catches that before anything is
    committed. Three independent checks, any of which reverts the path:

    - `is_write_denied(path)`: the same deny-list the file tools enforce
      (CI/CD configs, agent configuration, shell startup files, secret-shaped
      names, `.git` internals, escapes, ...).
    - The path is a symlink. The file tools refuse to ever create one (see
      `LocalWorkspace.resolve`'s `for_write` branch), so any symlink here was
      written by something else -- almost certainly a sandboxed script -- and
      is reverted unconditionally regardless of where it points.
    - The path has setuid or setgid bits set, which the file tools never set
      and which git does not even track as part of a file's mode -- these can
      only have come from a script executing outside the file-tool policy.

    A plain new executable bit (no setuid/setgid) is not reverted -- a worker
    legitimately writes shell scripts -- but is still called out in the
    returned notes so it's visible in `results` rather than blending into an
    ordinary diff.

    Returns (reverted_paths, notes). Never raises: a `git status` failure
    here just means nothing is scanned (finalize's own git calls afterwards
    will surface any real problem).
    """
    try:
        out = await _run_git(
            ["-C", str(info.path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
    except WorktreeError:
        return [], []

    reverted: list[str] = []
    notes: list[str] = []

    for status, path, orig in _parse_porcelain_z(out):
        full = info.path / path
        reason: str | None = None

        try:
            is_symlink = full.is_symlink()
        except OSError:
            is_symlink = False

        if is_symlink:
            reason = (
                "the file tools never create symlinks, so this one was written by "
                "something else (e.g. a sandboxed Bash script) and was removed"
            )
        elif is_write_denied(path):
            reason = "path is denied by write policy"
        else:
            try:
                mode = full.stat().st_mode
            except OSError:
                mode = 0
            if mode & (stat.S_ISUID | stat.S_ISGID):
                reason = "refusing to commit a file with setuid/setgid bits set"
            elif mode & 0o111:
                notes.append(f"{path}: newly executable")

        if reason is None:
            continue

        await _revert_changed_path(info.path, info.base_commit, path, orig)
        reverted.append(path)
        notes.append(f"{path}: {reason}")

    return sorted(set(reverted)), notes


async def finalize_worktree(
    info: WorktreeInfo,
    message: str,
    *,
    is_write_denied: Callable[[str], bool] | None = None,
    is_sensitive: Callable[[str], bool] | None = None,
) -> WorktreeOutcome:
    """Commit the worker's changes on the worktree's branch, or clean it up
    if the worker left nothing behind.

    `is_write_denied`/`is_sensitive` are optional callables (repo-root-relative
    POSIX path -> bool) the caller supplies -- typically bound methods of a
    `LocalWorkspace` rooted at `info.path` -- so this module never has to
    import the file-tool policy itself. When given, `is_write_denied` gates a
    pre-commit policy scan (see `_apply_write_policy`) whose reverted paths
    and notes are reported on the returned `WorktreeOutcome`; `is_sensitive`
    classifies the final, post-revert `changed_files` for the same "flagged,
    not blocked" treatment the file tools give sensitive paths. Both are
    optional and default to no scan, so existing callers are unaffected.
    """
    status_out = await _run_git(["-C", str(info.path), "status", "--porcelain"])
    head = (await _run_git(["-C", str(info.path), "rev-parse", "HEAD"])).strip()

    if not status_out.strip() and head == info.base_commit:
        await remove_worktree(info)
        return WorktreeOutcome(changed_files=[], kept=False, commit=None)

    policy_reverted: list[str] = []
    policy_notes: list[str] = []
    if is_write_denied is not None:
        policy_reverted, policy_notes = await _apply_write_policy(info, is_write_denied)

        # A policy revert may have undone everything the worker changed.
        status_out = await _run_git(["-C", str(info.path), "status", "--porcelain"])
        if not status_out.strip() and head == info.base_commit:
            await remove_worktree(info)
            return WorktreeOutcome(
                changed_files=[],
                kept=False,
                commit=None,
                policy_reverted_files=policy_reverted,
                policy_notes=policy_notes,
            )

    await _run_git(["-C", str(info.path), "add", "-A"])

    msg = _sanitize_message(message)
    extra_env = {
        "GIT_AUTHOR_NAME": _WORKER_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": _WORKER_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": _WORKER_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": _WORKER_AUTHOR_EMAIL,
    }
    # `--message=<msg>` binds the value unambiguously as a single argv token,
    # so a message starting with "-" can never be parsed as another option.
    await _run_git(
        ["-C", str(info.path), "commit", "--no-verify", f"--message={msg}"],
        extra_env=extra_env,
    )

    new_head = (await _run_git(["-C", str(info.path), "rev-parse", "HEAD"])).strip()

    diff_out = await _run_git(
        ["-C", str(info.path), "diff", "--name-only", f"{info.base_commit}..{new_head}"]
    )
    changed_files = [line for line in diff_out.splitlines() if line.strip()]

    sensitive_files: list[str] = []
    if is_sensitive is not None:
        sensitive_files = [f for f in changed_files if is_sensitive(f)]

    return WorktreeOutcome(
        changed_files=changed_files,
        kept=True,
        commit=new_head,
        policy_reverted_files=policy_reverted,
        policy_notes=policy_notes,
        sensitive_files=sensitive_files,
    )


async def remove_worktree(info: WorktreeInfo) -> None:
    """Force-remove the worktree and delete its branch. Idempotent."""
    meta_file = info.path.parent / f".{info.path.name}.meta.json"

    if info.path.exists():
        try:
            await _run_git(
                ["-C", str(info.repo_root), "worktree", "remove", "--force", str(info.path)]
            )
        except WorktreeError:
            shutil.rmtree(info.path, ignore_errors=True)
            try:
                await _run_git(["-C", str(info.repo_root), "worktree", "prune"])
            except WorktreeError:
                pass
    else:
        try:
            await _run_git(["-C", str(info.repo_root), "worktree", "prune"])
        except WorktreeError:
            pass

    try:
        await _run_git(["-C", str(info.repo_root), "branch", "-D", info.branch])
    except WorktreeError:
        pass  # already gone -- that's fine, this must be idempotent

    meta_file.unlink(missing_ok=True)


def _parse_porcelain_z(out: str) -> list[tuple[str, str, str | None]]:
    """Parse `git status --porcelain=v1 -z` output into (status, path, orig_path) triples.

    `orig_path` is the pre-rename/copy path, present only for R/C status
    entries (git emits it as a second NUL-terminated field right after the
    new path), else None.
    """
    entries: list[tuple[str, str, str | None]] = []
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        orig: str | None = None
        if status[0] == "R" or status[1] == "R":
            orig = parts[i] if i < len(parts) else None
            i += 1
        entries.append((status, path, orig))
    return entries


async def _dirty_state(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Map of repo-root-relative path -> {status, size, mtime_ns} for every
    path git currently considers dirty (modified/added/deleted/untracked).
    """
    try:
        out = await _run_git(
            ["-C", str(repo_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
    except WorktreeError:
        return {}

    state: dict[str, dict[str, Any]] = {}
    for status, path, _orig in _parse_porcelain_z(out):
        full = repo_root / path
        try:
            st = full.stat()
            size: int | None = st.st_size
            mtime_ns: int | None = st.st_mtime_ns
        except OSError:
            size = None
            mtime_ns = None
        state[path] = {"status": status, "size": size, "mtime_ns": mtime_ns}
    return state


async def snapshot_in_place(cwd: Path) -> str:
    """Opaque best-effort snapshot token for `changed_files_in_place`.

    Encodes the repo root plus, for every currently-dirty path, its status
    code and working-tree size/mtime -- enough to later tell a file that was
    already dirty apart from one the worker touched further.
    """
    cwd = Path(cwd).resolve()
    try:
        toplevel = await _run_git(["-C", str(cwd), "rev-parse", "--show-toplevel"])
        repo_root = Path(toplevel.strip()).resolve()
    except WorktreeError:
        # Not a git repo (or git unavailable): fall back to a snapshot with
        # no repo root, so changed_files_in_place treats everything dirty at
        # comparison time as new -- the safe (over-reporting) default.
        repo_root = cwd

    files = await _dirty_state(repo_root)
    return json.dumps({"repo_root": str(repo_root), "files": files}, sort_keys=True)


async def changed_files_in_place(cwd: Path, before: str) -> list[str]:
    """Best-effort list of repo-root-relative paths changed in `cwd`'s repo
    since the `before` snapshot (from `snapshot_in_place`).
    """
    cwd = Path(cwd).resolve()
    try:
        toplevel = await _run_git(["-C", str(cwd), "rev-parse", "--show-toplevel"])
        repo_root = Path(toplevel.strip()).resolve()
    except WorktreeError:
        return []

    try:
        prior = json.loads(before)
        prior_files = prior.get("files", {}) if isinstance(prior, dict) else {}
    except (json.JSONDecodeError, TypeError):
        prior_files = {}

    current = await _dirty_state(repo_root)

    changed = []
    for path, info in current.items():
        old = prior_files.get(path)
        if old is None or old != info:
            changed.append(path)

    return sorted(changed)


async def sweep(state: Path, retention_days: int) -> None:
    """Remove worktrees under `state/worktrees` for jobs older than
    `retention_days` that have no changes/commits beyond their base commit.

    Never touches anything outside `state` (a worktree's own repo is only
    ever addressed via `git worktree remove` / `branch -D` for a specific,
    known branch -- never a filesystem-level delete).
    """
    state_resolved = state.resolve()
    worktrees_dir = (state_resolved / "worktrees").resolve()
    try:
        worktrees_dir.relative_to(state_resolved)
    except ValueError:
        return  # paranoia: refuse if this somehow resolved outside state
    if not worktrees_dir.is_dir():
        return

    cutoff = time.time() - retention_days * 86400

    for meta_file in sorted(worktrees_dir.glob(".*.meta.json")):
        name = meta_file.name
        job_id = name[1 : -len(".meta.json")]
        if not _JOB_ID_RE.match(job_id):
            continue

        wt_path = worktrees_dir / job_id
        age_source = wt_path if wt_path.exists() else meta_file
        try:
            mtime = age_source.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue

        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta_file.unlink(missing_ok=True)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            continue

        if not wt_path.exists():
            meta_file.unlink(missing_ok=True)
            continue

        repo_root = Path(meta.get("repo_root", ""))
        base_commit = meta.get("base_commit", "")
        branch = meta.get("branch", f"anymodel/{job_id}")

        try:
            status_out = await _run_git(["-C", str(wt_path), "status", "--porcelain"])
            head = (await _run_git(["-C", str(wt_path), "rev-parse", "HEAD"])).strip()
        except WorktreeError:
            continue

        if status_out.strip() or head != base_commit:
            continue  # has changes or commits beyond base -- leave it

        info = WorktreeInfo(
            repo_root=repo_root,
            path=wt_path,
            workdir=wt_path,
            branch=branch,
            base_commit=base_commit,
        )
        await remove_worktree(info)

    # Meta-less orphan directories -- e.g. from a create_worktree that was
    # cancelled (or a process killed) between `worktree add` and the meta
    # write. The meta loop above can never see them, so reap any directory
    # matching the job-id pattern that has no meta file and is older than the
    # cutoff (directory mtime; retention_days=0 reaps immediately). Same
    # confinement as above: only entries directly inside the worktrees dir,
    # symlinks skipped and never followed, nothing outside state touched.
    for entry in sorted(worktrees_dir.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not _JOB_ID_RE.match(entry.name):
            continue
        if _meta_path(worktrees_dir, entry.name).exists():
            continue  # belongs to the meta loop above
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue
        await _force_remove(entry)
