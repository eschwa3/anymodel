"""Builds a throwaway git repo for the `real` suite's fixture app (`jobsched`).

The app's history is stored as an ordered set of full-tree *snapshots*
(`fixture_real/history/000N_*`) rather than unified diffs, so building the
repo is just "copy snapshot, `git add -A`, `git commit`" repeated in order --
robust to reformatting and never subject to patch fuzz. R1/R2 need one more
commit on top of that history whose diff is the thing under review; those
live as *overlays* (`fixture_real/overlays/<name>/`) that partially copy
their files over the last snapshot and commit again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
FIXTURE_REAL_DIR = BAKEOFF_DIR / "fixture_real"
HISTORY_DIR = FIXTURE_REAL_DIR / "history"
OVERLAY_DIR = FIXTURE_REAL_DIR / "overlays"

# Ordered by commit sequence -- do not reorder.
HISTORY_SNAPSHOTS: list[tuple[str, str]] = [
    ("0001_scaffold", "Scaffold jobsched: config, models, db, plan/customer/job repos"),
    ("0002_scheduler_billing", "Add job scheduler (reserve/complete/fail) and invoice billing"),
    ("0003_handlers_cli", "Add service layer, handlers, CLI, importer, notifications, docs"),
]

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")

_GIT_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME": "jobsched-history",
    "GIT_AUTHOR_EMAIL": "jobsched-history@example.invalid",
    "GIT_COMMITTER_NAME": "jobsched-history",
    "GIT_COMMITTER_EMAIL": "jobsched-history@example.invalid",
}


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, **_GIT_ENV_OVERRIDES}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _replace_tree(src: Path, dest: Path) -> None:
    """Make `dest`'s contents (except `.git`) exactly match `src`."""
    for entry in dest.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_IGNORE)


def _overlay_tree(overlay_dir: Path, dest: Path) -> None:
    """Copy every file under `overlay_dir` on top of `dest`, leaving
    everything else in `dest` untouched (a partial overlay, not a mirror).
    """
    for path in overlay_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(overlay_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _commit(dest: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=dest)
    result = _run_git(["-c", "commit.gpgsign=false", "commit", "-q", "-m", message], cwd=dest)
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed: {result.stderr.strip()}")


def build_base_repo(dest: Path) -> None:
    """Build the shared base history (all 3 snapshots) into `dest`, a fresh git repo."""
    dest.mkdir(parents=True, exist_ok=True)
    init = _run_git(["init", "-q", "-b", "main"], cwd=dest)
    if init.returncode != 0:
        # Older git without -b support.
        _run_git(["init", "-q"], cwd=dest)

    for name, message in HISTORY_SNAPSHOTS:
        snapshot_dir = HISTORY_DIR / name
        if not snapshot_dir.is_dir():
            raise FileNotFoundError(f"missing history snapshot: {snapshot_dir}")
        _replace_tree(snapshot_dir, dest)
        _commit(dest, message)


_OVERLAY_MESSAGES = {
    "r1_defects": "Refactor job search + add bulk-complete admin command",
    "r2_clean": "Add read-only customer-summary rollup for the ops dashboard",
}


def apply_overlay(dest: Path, overlay_name: str) -> None:
    overlay_dir = OVERLAY_DIR / overlay_name
    if not overlay_dir.is_dir():
        raise FileNotFoundError(f"missing overlay: {overlay_dir}")
    _overlay_tree(overlay_dir, dest)
    _commit(dest, _OVERLAY_MESSAGES.get(overlay_name, f"Apply overlay {overlay_name}"))


# Which overlay (if any) sits on top of the base history, per task id.
TASK_OVERLAY: dict[str, str | None] = {
    "R1": "r1_defects",
    "R2": "r2_clean",
    "R3": None,
    "R4": None,
    "R5": None,
    "R6": None,
    "R7": None,
    "R8": None,
}


def build_repo_for_task(dest: Path, task_id: str) -> Path:
    """Build the repo a given real-suite task's worker should see, at `dest`."""
    build_base_repo(dest)
    overlay = TASK_OVERLAY.get(task_id)
    if overlay:
        apply_overlay(dest, overlay)
    return dest


def ensure_fixture_venv(repo_dir: Path, *, timeout: float = 180.0) -> str | None:
    """Create a real `.venv` (with pytest installed) inside a fixture repo so
    a `--bash` run's sandboxed Bash tool can run `python -m pytest` (the
    sandbox makes the venv readable and puts its `bin` first on PATH).

    Returns None on success or a short warning string on failure -- a missing
    venv only costs the worker its test loop, it must never be a fatal harness
    error. Venvs are not relocatable, so each repo builds its own; there is
    deliberately no cross-run cache.
    """
    venv_dir = repo_dir / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        return None

    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout, check=False
        )

    def _warn(what: str, result: subprocess.CompletedProcess) -> str:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        tail = lines[-1][:160] if lines else ""
        return f"fixture venv: {what}" + (f" ({tail})" if tail else "")

    try:
        if shutil.which("uv"):
            result = _run(["uv", "venv", "--python", sys.executable, str(venv_dir)])
            if result.returncode != 0:
                return _warn("venv creation failed", result)
            result = _run(
                ["uv", "pip", "install", "--offline", "--python", str(venv_python), "pytest"]
            )
            if result.returncode != 0:
                # --offline only works when the wheel is already in uv's cache.
                result = _run(["uv", "pip", "install", "--python", str(venv_python), "pytest"])
        else:
            result = _run([sys.executable, "-m", "venv", str(venv_dir)])
            if result.returncode != 0:
                return _warn("venv creation failed", result)
            result = _run([str(venv_python), "-m", "pip", "install", "pytest"])
        if result.returncode != 0:
            return _warn("pytest install failed", result)
        verify = _run([str(venv_python), "-c", "import pytest"])
        if verify.returncode != 0:
            return _warn("pytest not importable in venv", verify)

        # Keep `.venv/` out of git WITHOUT touching tracked fixture files:
        # `git add -A` and `git status --porcelain` respect `.git/info/exclude`,
        # so the per-run patch and the CLI's git-derived changed_files never
        # include the venv.
        info_dir = repo_dir / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude = info_dir / "exclude"
        current = exclude.read_text() if exclude.exists() else ""
        if ".venv/" not in current.splitlines():
            if current and not current.endswith("\n"):
                current += "\n"
            exclude.write_text(current + ".venv/\n")
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"fixture venv: {type(exc).__name__}: {exc}"


def git_log_messages(repo_dir: Path) -> list[str]:
    result = _run_git(["log", "--format=%s"], cwd=repo_dir)
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_diff_head(repo_dir: Path) -> str:
    """Diff of the last commit (HEAD~1..HEAD)."""
    result = _run_git(["diff", "HEAD~1..HEAD"], cwd=repo_dir)
    return result.stdout


def git_diff_stat_head(repo_dir: Path) -> str:
    result = _run_git(["diff", "--stat", "HEAD~1..HEAD"], cwd=repo_dir)
    return result.stdout
