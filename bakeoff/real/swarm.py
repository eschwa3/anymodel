"""R8 (swarm-feature): dispatches 3 codegen tasks concurrently through the
real `anymodel_subagents.jobs.JobManager` (not the CLI), each in its own git
worktree, then plays orchestrator: merges the three `anymodel/<job_id>`
branches into an integration branch, records conflicts, and runs the
existing + hidden integration tests on the result.

`--dry-run` swaps in a stub `run_worker` that copies the matching reference
patch (`bakeoff/hidden_real/reference/r8/worker_{a,b,c}/`) into the
worktree instead of calling a real model, so the whole dispatch -> worktree
-> merge -> integration-test path is exercised offline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anymodel_subagents.config import Config
from anymodel_subagents.jobs import Job, JobManager, TaskSpec
from anymodel_subagents.types import Usage, WorkerResult
from real import fixture
from real.scoring import run_pytest

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
HIDDEN_REAL_DIR = BAKEOFF_DIR / "hidden_real"
REF_R8_DIR = HIDDEN_REAL_DIR / "reference" / "r8"
PROMPT_DIR = BAKEOFF_DIR / "tasks" / "real" / "R8_swarm_feature"

# Ordered: (task-id marker, prompt file, reference subdir, owned path prefixes).
WORKERS: list[dict[str, Any]] = [
    {
        "key": "r8-worker-a",
        "label": "r8-worker-a-persistence",
        "prompt_file": PROMPT_DIR / "worker_a_persistence.md",
        "reference_dir": REF_R8_DIR / "worker_a",
        "owned_prefixes": ("jobsched/migrations/", "jobsched/models.py", "jobsched/repository.py"),
    },
    {
        "key": "r8-worker-b",
        "label": "r8-worker-b-service",
        "prompt_file": PROMPT_DIR / "worker_b_service.md",
        "reference_dir": REF_R8_DIR / "worker_b",
        "owned_prefixes": ("jobsched/service.py",),
    },
    {
        "key": "r8-worker-c",
        "label": "r8-worker-c-cli-handlers",
        "prompt_file": PROMPT_DIR / "worker_c_cli_handlers.md",
        "reference_dir": REF_R8_DIR / "worker_c",
        "owned_prefixes": ("jobsched/cli.py", "jobsched/handlers.py"),
    },
]

_TASK_ID_RE = re.compile(r"task-id:\s*(\S+)")


class _FakeClient:
    def redaction_secrets(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


def _copy_reference_into(reference_dir: Path, workspace_root: Path) -> list[str]:
    changed: list[str] = []
    for path in reference_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(reference_dir)
        target = workspace_root / "jobsched" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        changed.append(f"jobsched/{rel.as_posix()}")
    return changed


def make_stub_run_worker():
    """A fake engine.run_worker for --dry-run: applies the reference patch
    matching the task's `<!-- task-id: ... -->` marker instead of calling a
    model, and reports a synthetic but well-formed WorkerResult.
    """

    async def _stub(
        *,
        client: Any,
        model: str,
        system_prompt: str,
        task_prompt: str,
        tools: Any,
        ws: Any,
        max_turns: int,
        timeout_s: float,
        transcript_path: Path | None = None,
        cancel_event: Any = None,
        **_engine_kwargs: Any,  # e.g. on_progress; the stub ignores what it doesn't model
    ) -> WorkerResult:
        match = _TASK_ID_RE.search(task_prompt)
        key = match.group(1) if match else None
        worker = next((w for w in WORKERS if w["key"] == key), None)
        changed: list[str] = []
        message = f"[dry-run] no matching reference patch for task-id {key!r}"
        if worker is not None:
            changed = _copy_reference_into(worker["reference_dir"], ws.root)
            message = f"[dry-run] applied reference patch for {worker['key']}: {', '.join(changed)}"
        return WorkerResult(
            status="completed",
            final_message=message,
            model=model,
            turns=1,
            usage=Usage(prompt_tokens=500, completion_tokens=200, cost=0.0, requests=1),
            tool_calls=len(changed),
            changed_files=changed,
            duration_s=0.5,
        )

    return _stub


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )


@dataclass
class BranchOutcome:
    label: str
    job_status: str
    kept: bool
    branch: str | None
    changed_files: list[str]
    ownership_ok: bool
    import_ok: bool
    merge_status: str  # "merged" | "conflict" | "no_changes" | "job_failed"
    conflicting_files: list[str] = field(default_factory=list)
    # Why a worker job failed (402, timeout, truncation...): `job_status` alone can't say.
    error: str | None = None


def _job_error(job: Any) -> str | None:
    """The job's own error, else its result's (already redacted by JobManager)."""
    result = getattr(job, "result", None)
    return getattr(job, "error", None) or (result.error if result is not None else None)


def _ownership_ok(changed_files: list[str], owned_prefixes: tuple[str, ...]) -> bool:
    """A branch stays within its lane if every changed file is under one of
    its owned prefixes -- *or* under tests/. The shared codegen role prompt
    (src/anymodel_subagents/workers/codegen.md) explicitly tells every
    worker to "add or update tests for the behavior you changed", so a
    worker adding its own test file is expected behavior, not an ownership
    violation, regardless of which slice it's implementing. Since each
    worker runs in its own worktree/branch, its own added tests can't
    collide with another worker's files here.
    """
    allowed = (*owned_prefixes, "tests/")
    return all(any(f.startswith(p) for p in allowed) for f in changed_files)


def _import_smoke_ok(worktree_dir: Path, timeout: float) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import jobsched, jobsched.cli, jobsched.service, jobsched.repository",
        ],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.returncode == 0


def _merge_branches(base_repo: Path, jobs_final: list[Job], timeout: float) -> list[BranchOutcome]:
    outcomes: list[BranchOutcome] = []
    _run_git(["checkout", "-b", "integration"], cwd=base_repo)

    for worker, job in zip(WORKERS, jobs_final, strict=True):
        wt = job.worktree_meta or {}
        branch = wt.get("branch")
        kept = bool(wt.get("kept"))
        changed_files = list(
            (job.result.changed_files if job.result else []) or wt.get("changed_files") or []
        )
        ownership_ok = _ownership_ok(changed_files, worker["owned_prefixes"])

        import_ok = False
        worktree_path = None
        if job.worktree_info is not None:
            worktree_path = job.worktree_info.workdir
            if worktree_path.is_dir():
                import_ok = _import_smoke_ok(worktree_path, timeout)

        if job.status != "completed":
            outcomes.append(
                BranchOutcome(
                    label=worker["label"],
                    job_status=job.status,
                    error=_job_error(job),
                    kept=kept,
                    branch=branch,
                    changed_files=changed_files,
                    ownership_ok=ownership_ok,
                    import_ok=import_ok,
                    merge_status="job_failed",
                )
            )
            continue

        if not branch or not kept:
            outcomes.append(
                BranchOutcome(
                    label=worker["label"],
                    job_status=job.status,
                    error=_job_error(job),
                    kept=kept,
                    branch=branch,
                    changed_files=changed_files,
                    ownership_ok=ownership_ok,
                    import_ok=import_ok,
                    merge_status="no_changes",
                )
            )
            continue

        result = _run_git(
            ["merge", "--no-ff", "-m", f"merge {worker['label']}", branch], cwd=base_repo
        )
        if result.returncode != 0:
            conflicts = _run_git(
                ["diff", "--name-only", "--diff-filter=U"], cwd=base_repo
            ).stdout.split()
            _run_git(["merge", "--abort"], cwd=base_repo)
            outcomes.append(
                BranchOutcome(
                    label=worker["label"],
                    job_status=job.status,
                    error=_job_error(job),
                    kept=kept,
                    branch=branch,
                    changed_files=changed_files,
                    ownership_ok=ownership_ok,
                    import_ok=import_ok,
                    merge_status="conflict",
                    conflicting_files=conflicts,
                )
            )
        else:
            outcomes.append(
                BranchOutcome(
                    label=worker["label"],
                    job_status=job.status,
                    error=_job_error(job),
                    kept=kept,
                    branch=branch,
                    changed_files=changed_files,
                    ownership_ok=ownership_ok,
                    import_ok=import_ok,
                    merge_status="merged",
                )
            )

    return outcomes


def _save_branch_patches(
    base_repo: Path, jobs_final: list[Job], out_dir: Path | None, run_slug: str | None
) -> None:
    """Save one unified-diff patch per worker branch (`base_commit..branch`,
    computed straight in `base_repo` since a worktree branch lives in the
    same repo) so this R8 run can be fully re-scored offline later without
    a model call -- see bakeoff/README.md's `--rescore`.
    """
    if out_dir is None or run_slug is None:
        return
    patches_dir = out_dir / "patches"
    for worker, job in zip(WORKERS, jobs_final, strict=True):
        wt = job.worktree_meta or {}
        branch = wt.get("branch")
        base_commit = wt.get("base_commit")
        if not branch or not base_commit:
            continue
        result = _run_git(["diff", f"{base_commit}..{branch}"], cwd=base_repo)
        if not result.stdout.strip():
            continue
        patches_dir.mkdir(parents=True, exist_ok=True)
        (patches_dir / f"{run_slug}__{worker['key']}.patch").write_text(result.stdout)


def _worker_specs(model: str, cwd: str, max_turns: int, mode: str) -> list[TaskSpec]:
    """The three R8 worker TaskSpecs. `mode` is "edit", or "edit+bash" when
    the harness runs with --bash; both keep worktree isolation (edit+bash
    requires it -- isolation "none" is rejected for that mode -- and R8's
    merge step needs the branches regardless).
    """
    return [
        TaskSpec(
            prompt=w["prompt_file"].read_text(),
            cwd=cwd,
            role="codegen",
            model=model,
            mode=mode,
            isolation="worktree",
            label=w["label"],
            max_turns=max_turns,
        )
        for w in WORKERS
    ]


async def run_r8(
    model: str,
    run_root: Path,
    *,
    max_turns: int,
    timeout: float,
    dry_run: bool,
    out_dir: Path | None = None,
    run_slug: str | None = None,
    bash: bool = False,
) -> dict[str, Any]:
    """Run the full R8 swarm for one model. `run_root` is a fresh, throwaway
    directory this call owns entirely. If `out_dir` and `run_slug` are
    given, each worker branch's patch is saved under `out_dir/patches/` for
    offline re-scoring. With `bash=True` the workers are dispatched in
    `edit+bash` mode (same worktree isolation they already use); if dispatch
    rejects that mode -- no OS sandbox on this machine -- all three fall
    back to plain `edit` and the returned dict carries a `bash_warning`.
    A failed fixture-venv build (`--bash`, live runs only) is recorded as
    `venv_warning`, also never fatal.
    """
    base_repo = run_root / "base_repo"
    fixture.build_base_repo(base_repo)

    # With --bash the workers' sandbox reads its venv from the source repo
    # (their worktrees won't have one: `.venv/` is git-excluded in base_repo,
    # and untracked files aren't copied into a new worktree). Build it before
    # dispatch; a failure is recorded as `venv_warning`, never fatal.
    venv_warning = fixture.ensure_fixture_venv(base_repo) if bash and not dry_run else None

    state_dir = run_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    prev_state_env = os.environ.get("ANYMODEL_STATE_DIR")
    os.environ["ANYMODEL_STATE_DIR"] = str(state_dir)

    cfg = Config(
        default_model=model,
        max_concurrency=3,
        max_turns=max_turns,
        timeout_s=timeout,
        max_tasks_per_dispatch=5,
        allowed_roots=(base_repo,),
    )

    if dry_run:
        run_worker_fn = make_stub_run_worker()

        def client_factory() -> Any:
            return _FakeClient()
    else:
        from anymodel_subagents import engine
        from anymodel_subagents.openrouter import OpenRouterClient

        run_worker_fn = engine.run_worker

        def client_factory() -> Any:
            return OpenRouterClient(os.environ["OPENROUTER_API_KEY"])

    manager = JobManager(cfg, state_dir, client_factory, run_worker=run_worker_fn)
    mode = "edit+bash" if bash else "edit"
    bash_warning: str | None = None
    start = time.monotonic()
    try:
        await manager.start()

        specs = _worker_specs(model, str(base_repo), max_turns, mode)
        try:
            _swarm_id, jobs = await manager.dispatch(specs)
        except ValueError as exc:
            # edit+bash is rejected at dispatch exactly when no OS sandbox is
            # available (jobs.py's _validate_task); --bash must never sink a
            # whole R8 run, so fall back to plain edit for all three workers.
            if not (bash and "edit+bash" in str(exc)):
                raise
            bash_warning = f"edit+bash rejected at dispatch, falling back to edit: {exc}"
            mode = "edit"
            specs = _worker_specs(model, str(base_repo), max_turns, mode)
            _swarm_id, jobs = await manager.dispatch(specs)
        job_ids = [j.job_id for j in jobs]

        deadline = time.monotonic() + timeout + 180
        while time.monotonic() < deadline:
            wait_result = await manager.wait(job_ids, timeout_s=40.0, mode="all")
            if wait_result["done"]:
                break

        jobs_final = [manager.get(jid) for jid in job_ids]
    finally:
        await manager.shutdown()
        if prev_state_env is None:
            os.environ.pop("ANYMODEL_STATE_DIR", None)
        else:
            os.environ["ANYMODEL_STATE_DIR"] = prev_state_env

    engine_duration_s = time.monotonic() - start

    _save_branch_patches(base_repo, jobs_final, out_dir, run_slug)

    per_job = [
        {
            "label": w["label"],
            "job_id": job.job_id,
            "status": job.status,
            "turns": job.result.turns if job.result else 0,
            "tool_calls": job.result.tool_calls if job.result else 0,
            "cost_usd": job.result.usage.cost if job.result else 0.0,
            "completion_tokens": job.result.usage.completion_tokens if job.result else 0,
            "duration_s": job.result.duration_s if job.result else 0.0,
            "final_message": job.result.final_message if job.result else "",
            "changed_files": job.result.changed_files if job.result else [],
        }
        for w, job in zip(WORKERS, jobs_final, strict=True)
    ]

    branch_outcomes = _merge_branches(base_repo, jobs_final, timeout)
    scored = _score_from_branch_outcomes(base_repo, branch_outcomes, timeout)

    total_cost = sum(p["cost_usd"] for p in per_job)
    total_turns = sum(p["turns"] for p in per_job)
    total_tool_calls = sum(p["tool_calls"] for p in per_job)
    total_completion_tokens = sum(p["completion_tokens"] for p in per_job)

    result = {
        **scored,
        "mode": mode,
        "per_job": per_job,
        "engine_duration_s": round(engine_duration_s, 3),
        "total_cost_usd": round(total_cost, 6),
        "total_turns": total_turns,
        "total_tool_calls": total_tool_calls,
        "total_completion_tokens": total_completion_tokens,
    }
    if bash_warning:
        result["bash_warning"] = bash_warning
    if venv_warning:
        result["venv_warning"] = venv_warning
    return result


def _score_from_branch_outcomes(
    base_repo: Path, branch_outcomes: list[BranchOutcome], timeout: float
) -> dict[str, Any]:
    """Shared tail of R8 scoring: merge cleanliness, the existing + hidden
    integration test run on the merged result, and the weighted `overall`.
    Used by both the live `run_r8` path and `rescore_r8_from_patches`.
    """
    merged_count = sum(1 for b in branch_outcomes if b.merge_status == "merged")
    merge_cleanliness = merged_count / len(branch_outcomes) if branch_outcomes else 0.0

    existing_pass, existing_summary = run_pytest(base_repo, "tests", timeout)
    hidden_src = HIDDEN_REAL_DIR / "hidden_tests" / "r8_integration_test.py"
    hidden_dest = base_repo / "tests" / "test_job_tags_integration.py"
    hidden_dest.write_text(hidden_src.read_text())
    hidden_pass, hidden_summary = run_pytest(base_repo, "tests", timeout)

    per_branch_scores = [
        (0.5 if b.ownership_ok else 0.0) + (0.5 if b.import_ok else 0.0) for b in branch_outcomes
    ]
    avg_per_branch = sum(per_branch_scores) / len(per_branch_scores) if per_branch_scores else 0.0

    overall = round(
        0.2 * avg_per_branch
        + 0.3 * merge_cleanliness
        + 0.25 * float(existing_pass)
        + 0.25 * float(hidden_pass),
        4,
    )

    return {
        "overall": overall,
        "branch_outcomes": [
            {
                "label": b.label,
                "job_status": b.job_status,
                "error": b.error,
                "kept": b.kept,
                "merge_status": b.merge_status,
                "ownership_ok": b.ownership_ok,
                "import_ok": b.import_ok,
                "conflicting_files": b.conflicting_files,
                "changed_files": b.changed_files,
            }
            for b in branch_outcomes
        ],
        "merge_cleanliness": round(merge_cleanliness, 4),
        "existing_tests_pass": existing_pass,
        "existing_tests_summary": existing_summary,
        "hidden_integration_tests_pass": hidden_pass,
        "hidden_integration_tests_summary": hidden_summary,
    }


def rescore_r8_from_patches(patch_paths: dict[str, Path], timeout: float) -> dict[str, Any]:
    """Reconstruct and re-score an R8 swarm run entirely offline from saved
    per-worker patches (see `_save_branch_patches`) -- no model call: a
    fresh base repo, one branch per worker with its saved patch applied and
    committed on top of the same base commit, then the same
    merge/ownership/import-smoke/hidden-test scoring `run_r8` runs live.
    `patch_paths` maps a worker key (`WORKERS[i]["key"]`) to its saved
    `.patch` file; a worker with no entry (or a missing/empty file) is
    scored as having made no changes.
    """
    run_root = Path(tempfile.mkdtemp(prefix="bakeoff-r8-rescore-"))
    try:
        base_repo = run_root / "base_repo"
        fixture.build_base_repo(base_repo)
        base_commit = _run_git(["rev-parse", "HEAD"], cwd=base_repo).stdout.strip()
        original_branch = _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo
        ).stdout.strip()

        prepared: list[dict[str, Any]] = []
        for worker in WORKERS:
            patch_path = patch_paths.get(worker["key"])
            branch = f"anymodel/{worker['key']}"
            changed_files: list[str] = []
            kept = False
            if (
                patch_path is not None
                and Path(patch_path).is_file()
                and Path(patch_path).stat().st_size > 0
            ):
                _run_git(["checkout", "-b", branch, base_commit], cwd=base_repo)
                apply_result = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", str(patch_path)],
                    cwd=base_repo,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if apply_result.returncode == 0:
                    _run_git(["add", "-A"], cwd=base_repo)
                    changed_files = [
                        line
                        for line in _run_git(
                            ["diff", "--cached", "--name-only"], cwd=base_repo
                        ).stdout.splitlines()
                        if line.strip()
                    ]
                    _run_git(
                        [
                            "-c",
                            "commit.gpgsign=false",
                            "commit",
                            "-q",
                            "-m",
                            f"rescore: {worker['key']}",
                        ],
                        cwd=base_repo,
                    )
                    kept = True
                _run_git(["checkout", original_branch], cwd=base_repo)
            prepared.append(
                {
                    "worker": worker,
                    "branch": branch if kept else None,
                    "changed_files": changed_files,
                }
            )

        outcomes: list[BranchOutcome] = []
        _run_git(["checkout", "-b", "integration"], cwd=base_repo)
        for item in prepared:
            worker = item["worker"]
            branch = item["branch"]
            changed_files = item["changed_files"]
            ownership_ok = _ownership_ok(changed_files, worker["owned_prefixes"])

            if branch is None:
                outcomes.append(
                    BranchOutcome(
                        label=worker["label"],
                        job_status="completed",
                        kept=False,
                        branch=None,
                        changed_files=changed_files,
                        ownership_ok=ownership_ok,
                        import_ok=False,
                        merge_status="no_changes",
                    )
                )
                continue

            import_ok = False
            if _run_git(["checkout", branch], cwd=base_repo).returncode == 0:
                import_ok = _import_smoke_ok(base_repo, timeout)
                _run_git(["checkout", "integration"], cwd=base_repo)

            merge_result = _run_git(
                ["merge", "--no-ff", "-m", f"merge {worker['label']}", branch], cwd=base_repo
            )
            if merge_result.returncode != 0:
                conflicts = _run_git(
                    ["diff", "--name-only", "--diff-filter=U"], cwd=base_repo
                ).stdout.split()
                _run_git(["merge", "--abort"], cwd=base_repo)
                outcomes.append(
                    BranchOutcome(
                        label=worker["label"],
                        job_status="completed",
                        kept=True,
                        branch=branch,
                        changed_files=changed_files,
                        ownership_ok=ownership_ok,
                        import_ok=import_ok,
                        merge_status="conflict",
                        conflicting_files=conflicts,
                    )
                )
            else:
                outcomes.append(
                    BranchOutcome(
                        label=worker["label"],
                        job_status="completed",
                        kept=True,
                        branch=branch,
                        changed_files=changed_files,
                        ownership_ok=ownership_ok,
                        import_ok=import_ok,
                        merge_status="merged",
                    )
                )

        return _score_from_branch_outcomes(base_repo, outcomes, timeout)
    finally:
        shutil.rmtree(run_root, ignore_errors=True)
