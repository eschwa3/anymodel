#!/usr/bin/env python3
"""Bake-off harness for anymodel-subagents.

Runs a small suite of fixed tasks against a set of candidate worker
models through the `anymodel-worker` CLI, scores each run, and writes
a per-model summary (turns, tool calls, cost, and task scores).

    uv run python bakeoff/run.py --models a,b,c [--tasks 1,2,3] \\
        [--repeats 1] [--max-turns 30] [--timeout 600] [--parallel 3]

    uv run python bakeoff/run.py --dry-run --models a,b   # offline smoke test

See bakeoff/README.md for what each score means and how to read the
output. This script never calls a model API directly -- it only shells
out to the `anymodel-worker` CLI (unless --dry-run is given, in which
case it fabricates a plausible response and touches no files).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BAKEOFF_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = BAKEOFF_DIR / "fixture"
TASKS_DIR = BAKEOFF_DIR / "tasks"
HIDDEN_DIR = BAKEOFF_DIR / "hidden"
RUNS_DIR = BAKEOFF_DIR / "runs"

GIT_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME": "bakeoff-harness",
    "GIT_AUTHOR_EMAIL": "bakeoff@example.invalid",
    "GIT_COMMITTER_NAME": "bakeoff-harness",
    "GIT_COMMITTER_EMAIL": "bakeoff@example.invalid",
}

# --- Task definitions ------------------------------------------------------

TASKS: dict[int, dict[str, str]] = {
    1: {
        "key": "find_bugs",
        "mode": "read-only",
        "prompt_file": str(TASKS_DIR / "01_find_bugs.md"),
    },
    2: {
        "key": "write_tests",
        "mode": "edit",
        "prompt_file": str(TASKS_DIR / "02_write_tests.md"),
    },
    3: {
        "key": "multi_file_edit",
        "mode": "edit",
        "prompt_file": str(TASKS_DIR / "03_multi_file_edit.md"),
    },
}

# Mirrors bakeoff/ANSWERS.md. Keep the two in sync if either changes.
# "keywords" are distinctive phrases/substrings that a correct report of
# this bug is likely to contain, independent of exact line-number citing.
BUGS: list[dict[str, Any]] = [
    {
        "id": 1,
        "file": "inventory_lib/discounts.py",
        "line": 34,
        "keywords": [
            "quantity > threshold",
            "off-by-one",
            "off by one",
            "tier boundary",
            "exact threshold",
            ">= threshold",
            "strict >",
        ],
    },
    {
        "id": 2,
        "file": "inventory_lib/pricing.py",
        "line": 40,
        "keywords": [
            "tax_amount = round(subtotal",
            "taxed on the subtotal",
            "tax on subtotal",
            "pre-discount",
            "taxable_amount",
            "tax before discount",
            "tax on the full subtotal",
        ],
    },
    {
        "id": 3,
        "file": "inventory_lib/inventory.py",
        "line": 100,
        "keywords": [
            "partial release",
            "partial-release",
            "_reserved[reservation.sku]",
            "never freed",
            "not restored",
            "doesn't restore",
            "does not restore",
            "stock stays reserved",
        ],
    },
]


def default_models() -> list[str]:
    return [
        "deepseek/deepseek-v4.1-flash",
        "deepseek/deepseek-v4-flash-0731",
        "deepseek/deepseek-v4-pro-0813",
        "z-ai/glm-5.3-flash",
        "z-ai/glm-5.3",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.8-27b",
        "minimax/minimax-m3",
        "google/gemini-3.8-flash",
    ]


# --- Small utilities ---------------------------------------------------


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def tokens_per_second(output_tokens: float | None, wall_s: float | None) -> float | None:
    """Output tokens per wall-clock second, or None if either input is missing or zero.

    Informational speed metric only -- never folds into score/ranking (see
    bakeoff/README.md). Mirrored in real/report.py for the --suite real path.
    """
    if not output_tokens or not wall_s:
        return None
    return round(output_tokens / wall_s, 2)


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return round(vals[mid], 4)
    return round((vals[mid - 1] + vals[mid]) / 2, 4)


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", text).strip("_")


def run_cmd(
    cmd: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def git_init_and_commit(repo_dir: Path) -> None:
    """Initialize a throwaway git repo so file changes can be diffed.

    This repo is never pushed anywhere and is deleted with the temp
    directory; gpg signing is explicitly disabled here only so a
    machine with `commit.gpgsign = true` in its global git config
    doesn't hang this scratch commit waiting on a passphrase.
    """
    env = {**os.environ, **GIT_ENV_OVERRIDES}
    run_cmd(["git", "init", "-q"], cwd=repo_dir, timeout=30, env=env)
    run_cmd(["git", "-c", "commit.gpgsign=false", "add", "-A"], cwd=repo_dir, timeout=30, env=env)
    run_cmd(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial fixture snapshot"],
        cwd=repo_dir,
        timeout=30,
        env=env,
    )


def git_changed_files(repo_dir: Path) -> list[str]:
    """Files changed relative to the initial commit, including new/untracked ones."""
    env = {**os.environ, **GIT_ENV_OVERRIDES}
    run_cmd(["git", "add", "-A"], cwd=repo_dir, timeout=30, env=env)
    result = run_cmd(
        ["git", "diff", "--cached", "--name-only", "HEAD"], cwd=repo_dir, timeout=30, env=env
    )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted(files)


def copy_fixture(dest: Path) -> None:
    shutil.copytree(
        FIXTURE_DIR,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


PYTEST_SUMMARY_RE = re.compile(
    r"(?P<counts>\d+ (?:passed|failed|error|skipped)(?:, \d+ (?:passed|failed|error|skipped))*)"
    r".* in [\d.]+s"
)


def run_pytest(repo_dir: Path, target: str, timeout: float) -> tuple[bool, str]:
    """Run pytest on `target` (relative to repo_dir) inside repo_dir.

    Returns (all_passed, human_summary). "No tests collected" counts as
    not passed, since that generally means the thing under test wasn't
    actually exercised.
    """
    cmd = [sys.executable, "-m", "pytest", target, "-q"]
    try:
        result = run_cmd(cmd, cwd=repo_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"pytest timed out after {timeout}s"
    output = (result.stdout or "") + (result.stderr or "")
    match = PYTEST_SUMMARY_RE.search(output)
    summary = match.group(0) if match else output.strip().splitlines()[-1] if output.strip() else ""
    if result.returncode == 5:
        return False, "no tests collected"
    return result.returncode == 0, summary


def repo_relpath_or_none(path_str: str, repo_dir: Path) -> str | None:
    try:
        resolved = Path(path_str)
        if not resolved.is_absolute():
            resolved = (repo_dir / resolved).resolve()
        else:
            resolved = resolved.resolve()
        resolved.relative_to(repo_dir.resolve())
        return str(resolved)
    except (ValueError, OSError):
        return None


def find_outside_repo_paths(reported_changed_files: list[Any], repo_dir: Path) -> list[str]:
    """Of the paths the CLI *claims* it changed, which ones resolve outside repo_dir?"""
    offenders = []
    for raw in reported_changed_files or []:
        if not isinstance(raw, str):
            continue
        if repo_relpath_or_none(raw, repo_dir) is None:
            offenders.append(raw)
    return offenders


# --- Scoring -------------------------------------------------------------

LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)


def score_task1(final_message: str) -> dict[str, Any]:
    text = final_message or ""
    text_lower = text.lower()
    matched_ids: list[int] = []
    for bug in BUGS:
        basename = Path(bug["file"]).name.lower()
        window = range(max(0, bug["line"] - 5), bug["line"] + 6)
        line_hit = basename in text_lower and any(re.search(rf"\b{n}\b", text) for n in window)
        keyword_hit = any(kw.lower() in text_lower for kw in bug["keywords"])
        if line_hit or keyword_hit:
            matched_ids.append(bug["id"])

    num_items = len(LIST_ITEM_RE.findall(text))
    recall = len(matched_ids)
    false_positives = max(0, num_items - recall)
    return {
        "bugs_found": recall,
        "bugs_total": len(BUGS),
        "matched_bug_ids": matched_ids,
        "num_listed_items_heuristic": num_items,
        "false_positives_heuristic": false_positives,
    }


def score_task2(repo_dir: Path, changed_files: list[str], timeout: float) -> dict[str, Any]:
    test_rel = "tests/test_importer.py"
    test_path = repo_dir / test_rel
    tests_created = test_path.exists()
    non_test_files_modified = [f for f in changed_files if not f.startswith("tests/")]

    result: dict[str, Any] = {
        "tests_created": tests_created,
        "non_test_files_modified": non_test_files_modified,
    }
    if not tests_created:
        result.update(
            tests_pass_on_original=False,
            pytest_summary=None,
            mutants_total=0,
            mutants_killed=0,
            mutation_score=None,
        )
        return result

    passed, summary = run_pytest(repo_dir, test_rel, timeout)
    result["tests_pass_on_original"] = passed
    result["pytest_summary"] = summary

    importer_path = repo_dir / "inventory_lib" / "importer.py"
    mutants_dir = HIDDEN_DIR / "task2_mutants"
    mutant_files = sorted(mutants_dir.glob("*.py")) if mutants_dir.is_dir() else []

    killed = 0
    mutant_reports = []
    if importer_path.exists() and mutant_files:
        original_content = importer_path.read_text()
        try:
            for mutant_path in mutant_files:
                importer_path.write_text(mutant_path.read_text())
                m_passed, m_summary = run_pytest(repo_dir, test_rel, timeout)
                survived = m_passed  # tests still green against a broken module == bad
                if not survived:
                    killed += 1
                mutant_reports.append(
                    {"mutant": mutant_path.name, "survived": survived, "summary": m_summary}
                )
        finally:
            importer_path.write_text(original_content)

    result["mutants_total"] = len(mutant_files)
    result["mutants_killed"] = killed
    result["mutation_score"] = (killed / len(mutant_files)) if mutant_files else None
    result["mutant_results"] = mutant_reports
    return result


def score_task3(repo_dir: Path, changed_files: list[str], timeout: float) -> dict[str, Any]:
    hidden_test_src = HIDDEN_DIR / "task3" / "test_currency_feature.py"
    dest = repo_dir / "tests" / "test_currency_feature_hidden.py"
    injected = False
    if hidden_test_src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(hidden_test_src.read_text())
        injected = True

    passed, summary = run_pytest(repo_dir, "tests", timeout) if injected else (False, None)

    files_changed_count = len(changed_files)
    return {
        "hidden_test_injected": injected,
        "tests_pass_all": passed,
        "pytest_summary": summary,
        "files_changed_count": files_changed_count,
        "files_changed_ge_3": files_changed_count >= 3,
        "changed_files": changed_files,
    }


# --- Dry-run stub ----------------------------------------------------------


def stub_worker_result(
    model: str, task_id: int, mode: str, transcript_path: Path
) -> dict[str, Any]:
    """Fabricate a plausible CLI response without touching any files or network."""
    rng = random.Random(f"{model}:{task_id}")
    prompt_tokens = rng.randint(800, 4000)
    completion_tokens = rng.randint(200, 1500)
    turns = rng.randint(3, 12)
    tool_calls = rng.randint(turns, turns * 3)
    invalid_tool_calls = rng.randint(0, 1)
    cost = round((prompt_tokens * 0.0000002 + completion_tokens * 0.0000008), 6)

    if task_id == 1:
        final_message = (
            "1. discounts.py around the tier threshold check looks off by one; "
            "quantity 10 should get the 5% tier but the strict `>` excludes it.\n"
            "2. pricing.py taxes the pre-discount subtotal instead of the taxable_amount, "
            "overcharging tax whenever a discount applies.\n"
            "3. inventory.py's partial release path doesn't restore reserved stock "
            "(never freed), unlike the full-release path."
        )
    else:
        final_message = f"[dry-run stub] pretended to complete task {task_id} with {model}."

    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        json.dumps({"note": "dry-run stub transcript, no real worker was invoked"}, indent=2)
    )

    return {
        "status": "completed",
        "final_message": final_message,
        "model": model,
        "turns": turns,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost": cost,
            "requests": turns,
        },
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid_tool_calls,
        "changed_files": [],
        "transcript_path": str(transcript_path),
        "error": None,
        "duration_s": round(rng.uniform(2, 20), 2),
    }


# --- Per-run record ----------------------------------------------------


@dataclass
class RunRecord:
    model: str
    task_id: int
    task_key: str
    repeat: int
    status: str
    cli_json: dict[str, Any] | None
    changed_files: list[str]
    outside_repo_paths: list[str]
    score: dict[str, Any]
    transcript_path: str
    wall_time_s: float
    engine_duration_s: float = 0.0
    error: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    # Output tokens per wall-clock second (usage.completion_tokens /
    # engine_duration_s), or None when either is missing or zero. None for
    # old results files that predate this field. Informational only -- see
    # summarize()/render_summary_md.
    output_tokens_per_s: float | None = None


async def run_one(
    model: str,
    task_id: int,
    repeat: int,
    args: argparse.Namespace,
    out_dir: Path,
    semaphore: asyncio.Semaphore,
) -> RunRecord:
    task = TASKS[task_id]

    slug = f"{slugify(model)}__task{task_id}__r{repeat}"
    transcript_path = out_dir / "transcripts" / f"{slug}.json"

    async with semaphore:
        # Measured *after* acquiring the semaphore, deliberately: time spent
        # waiting for a free slot is queueing, not work, and must not be
        # blamed on the model. `engine_duration_s` below (from the worker
        # result's own `duration_s`, timed inside engine.run_worker) is the
        # number to use for any per-model speed comparison; `wall_time_s` is
        # kept only as a harness-side sanity figure.
        start = asyncio.get_event_loop().time()
        tmp_dir = Path(tempfile.mkdtemp(prefix="bakeoff-"))
        repo_dir = tmp_dir / "repo"
        try:
            copy_fixture(repo_dir)
            git_init_and_commit(repo_dir)

            if args.dry_run:
                cli_json = stub_worker_result(model, task_id, task["mode"], transcript_path)
                status = cli_json["status"]
                stdout_tail = stderr_tail = None
                error = None
            else:
                cmd = [
                    "uv",
                    "run",
                    "anymodel-worker",
                    "run",
                    "--model",
                    model,
                    "--cwd",
                    str(repo_dir),
                    "--prompt-file",
                    task["prompt_file"],
                    "--mode",
                    task["mode"],
                    "--max-turns",
                    str(args.max_turns),
                    "--timeout",
                    str(args.timeout),
                    "--transcript",
                    str(transcript_path),
                    "--json",
                ]
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                cli_json, status, error, stdout_tail, stderr_tail = await _invoke_worker_cli(
                    cmd, args.timeout
                )

            changed_files = git_changed_files(repo_dir)
            reported_changed = (cli_json or {}).get("changed_files") if cli_json else None
            outside_repo_paths = find_outside_repo_paths(reported_changed, repo_dir)

            score: dict[str, Any] = {}
            final_message = (cli_json or {}).get("final_message", "") if cli_json else ""
            if cli_json is not None:
                if task_id == 1:
                    score = score_task1(final_message)
                elif task_id == 2:
                    score = score_task2(repo_dir, changed_files, args.timeout)
                elif task_id == 3:
                    score = score_task3(repo_dir, changed_files, args.timeout)

            wall_time = asyncio.get_event_loop().time() - start
            engine_duration_s = round(float((cli_json or {}).get("duration_s") or 0.0), 3)
            completion_tokens = ((cli_json or {}).get("usage") or {}).get("completion_tokens")
            tok_per_s = tokens_per_second(completion_tokens, engine_duration_s)
            return RunRecord(
                model=model,
                task_id=task_id,
                task_key=task["key"],
                repeat=repeat,
                status=status,
                cli_json=cli_json,
                changed_files=changed_files,
                outside_repo_paths=outside_repo_paths,
                score=score,
                transcript_path=str(transcript_path),
                wall_time_s=round(wall_time, 3),
                engine_duration_s=engine_duration_s,
                error=error,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                output_tokens_per_s=tok_per_s,
            )
        except Exception as exc:  # noqa: BLE001 - a single run's failure must not sink the batch
            wall_time = asyncio.get_event_loop().time() - start
            return RunRecord(
                model=model,
                task_id=task_id,
                task_key=task["key"],
                repeat=repeat,
                status="harness_error",
                cli_json=None,
                changed_files=[],
                outside_repo_paths=[],
                score={},
                transcript_path=str(transcript_path),
                wall_time_s=round(wall_time, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _invoke_worker_cli(
    cmd: list[str], worker_timeout: float
) -> tuple[dict[str, Any] | None, str, str | None, str | None, str | None]:
    """Run the anymodel-worker CLI and parse its one-line JSON contract.

    Any crash, timeout, or non-JSON stdout is reported as a failed run
    rather than raised, so one broken model/task never stops the batch.
    """
    overall_timeout = worker_timeout + 60  # generous buffer over the worker's own timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
    except (OSError, FileNotFoundError) as exc:
        return None, "harness_error", f"failed to launch CLI: {exc}", None, None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=overall_timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "harness_timeout", f"subprocess exceeded {overall_timeout}s", None, None

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    stdout_tail = stdout[-2000:]
    stderr_tail = stderr[-2000:]

    try:
        cli_json = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return (
            None,
            "harness_error",
            "CLI did not print valid JSON on stdout",
            stdout_tail,
            stderr_tail,
        )

    status = cli_json.get("status", "unknown")
    return cli_json, status, cli_json.get("error"), stdout_tail, stderr_tail


# --- Aggregation & reporting ------------------------------------------


def task_overall_score(task_id: int, score: dict[str, Any]) -> float | None:
    """Collapse a task's score dict into a single 0..1 number."""
    if not score:
        return None
    if task_id == 1:
        recall = score.get("bugs_found", 0) / max(1, score.get("bugs_total", 3))
        fp_penalty = min(0.3, 0.1 * score.get("false_positives_heuristic", 0))
        return max(0.0, round(recall - fp_penalty, 4))
    if task_id == 2:
        if not score.get("tests_created"):
            return 0.0
        pass_score = 1.0 if score.get("tests_pass_on_original") else 0.0
        mutation = score.get("mutation_score")
        mutation = mutation if mutation is not None else 0.0
        penalty = 0.2 if score.get("non_test_files_modified") else 0.0
        return max(0.0, round(0.3 * pass_score + 0.7 * mutation - penalty, 4))
    if task_id == 3:
        pass_score = 1.0 if score.get("tests_pass_all") else 0.0
        files_score = 1.0 if score.get("files_changed_ge_3") else 0.0
        return round(0.7 * pass_score + 0.3 * files_score, 4)
    return None


def summarize(records: list[RunRecord]) -> dict[str, Any]:
    by_model: dict[str, list[RunRecord]] = {}
    for rec in records:
        by_model.setdefault(rec.model, []).append(rec)

    summary: dict[str, Any] = {}
    for model, recs in by_model.items():
        per_task: dict[int, dict[str, Any]] = {}
        total_score = 0.0
        total_cost = 0.0
        total_turns = 0
        total_tool_calls = 0
        total_invalid_tool_calls = 0
        total_wall_time = 0.0
        total_engine_duration = 0.0
        status_counts: dict[str, int] = {}
        n = 0
        durations_s: list[float] = []
        tok_per_s_values: list[float] = []

        for rec in recs:
            n += 1
            status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
            total_wall_time += rec.wall_time_s
            total_engine_duration += rec.engine_duration_s
            durations_s.append(rec.engine_duration_s)
            if rec.output_tokens_per_s is not None:
                tok_per_s_values.append(rec.output_tokens_per_s)
            usage = (rec.cli_json or {}).get("usage") or {}
            total_cost += usage.get("cost") or 0.0
            total_turns += (rec.cli_json or {}).get("turns") or 0
            total_tool_calls += (rec.cli_json or {}).get("tool_calls") or 0
            total_invalid_tool_calls += (rec.cli_json or {}).get("invalid_tool_calls") or 0

            task_score = task_overall_score(rec.task_id, rec.score)
            if task_score is not None:
                total_score += task_score
            bucket = per_task.setdefault(rec.task_id, {"n": 0, "score_sum": 0.0, "scores": []})
            bucket["n"] += 1
            if task_score is not None:
                bucket["score_sum"] += task_score
                bucket["scores"].append(task_score)

        for bucket in per_task.values():
            bucket["avg_score"] = (
                round(bucket["score_sum"] / len(bucket["scores"]), 4) if bucket["scores"] else None
            )

        score_per_dollar = (total_score / total_cost) if total_cost > 0 else None
        summary[model] = {
            "n_runs": n,
            "status_counts": status_counts,
            "per_task": per_task,
            "total_score": round(total_score, 4),
            "total_cost_usd": round(total_cost, 6),
            "score_per_dollar": (
                round(score_per_dollar, 4) if score_per_dollar is not None else None
            ),
            "avg_turns": round(total_turns / n, 2) if n else 0,
            "avg_tool_calls": round(total_tool_calls / n, 2) if n else 0,
            "invalid_tool_call_rate": (
                round(total_invalid_tool_calls / total_tool_calls, 4) if total_tool_calls else 0.0
            ),
            "avg_wall_time_s": round(total_wall_time / n, 2) if n else 0,
            "avg_engine_duration_s": round(total_engine_duration / n, 2) if n else 0,
            # Speed, informational only -- never folds into score/ranking.
            "median_engine_duration_s": _median(durations_s),
            "median_output_tokens_per_s": _median(tok_per_s_values),
            "total_engine_duration_s": round(sum(durations_s), 2),
        }
    return summary


def render_summary_md(summary: dict[str, Any], args: argparse.Namespace) -> str:
    lines = ["# Bake-off summary", "", f"Generated: {datetime.now(UTC).isoformat()}"]
    lines.append(
        f"Tasks: {args.tasks or '1,2,3'} | repeats: {args.repeats} | dry-run: {args.dry_run}"
    )
    lines.append("")

    for model, data in summary.items():
        lines.append(f"## {model}")
        lines.append("")
        lines.append(f"- runs: {data['n_runs']}, statuses: {data['status_counts']}")
        lines.append(
            f"- avg turns: {data['avg_turns']}, avg tool calls: {data['avg_tool_calls']}, "
            f"invalid tool call rate: {data['invalid_tool_call_rate']}"
        )
        lines.append(
            f"- avg engine duration: {data['avg_engine_duration_s']}s (harness wall time, "
            f"incl. queue wait, was {data['avg_wall_time_s']}s), total cost: "
            f"${data['total_cost_usd']}"
        )
        median_s = data["median_engine_duration_s"]
        median_tps = data["median_output_tokens_per_s"]
        lines.append(
            f"- speed (informational, not part of the score): median {median_s if median_s is not None else '-'}s/task, "
            f"median {median_tps if median_tps is not None else '-'} output tok/s, "
            f"total engine duration {data['total_engine_duration_s']}s"
        )
        lines.append("")
        lines.append("| Task | Runs | Avg score (0-1) |")
        lines.append("|---|---|---|")
        for task_id in sorted(data["per_task"]):
            bucket = data["per_task"][task_id]
            task_name = TASKS[task_id]["key"]
            lines.append(f"| {task_id} ({task_name}) | {bucket['n']} | {bucket['avg_score']} |")
        lines.append("")
        spd = data["score_per_dollar"]
        spd_str = f"{spd}" if spd is not None else "N/A (zero cost recorded)"
        lines.append(
            f"**Score per dollar: {spd_str}** (total_score={data['total_score']} / "
            f"total_cost_usd={data['total_cost_usd']})"
        )
        lines.append("")

    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------


def parse_int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--suite",
        choices=["smoke", "real"],
        default="real",
        help="'smoke' is the original v1 quick-check (3 tiny tasks); 'real' (default) is the "
        "realistic mid-size fixture with tasks R1-R8 -- see bakeoff/README.md.",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Comma-separated OpenRouter model ids (default: the README shortlist)",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated task ids: 1,2,3 for --suite smoke (default: all three); "
        "R1..R8 for --suite real (default: all eight)",
    )
    parser.add_argument("--repeats", type=int, default=1, help="Repeats per (model, task) pair")
    parser.add_argument(
        "--max-turns", type=int, default=30, help="Passed through to the worker CLI"
    )
    parser.add_argument(
        "--timeout", type=int, default=600, help="Per-run wall-clock timeout, seconds"
    )
    parser.add_argument("--parallel", type=int, default=3, help="Max concurrent worker runs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use a fabricated stub instead of invoking the real CLI (no network, no edits)",
    )
    parser.add_argument(
        "--out-dir", default=None, help="Override the runs/<timestamp> output directory"
    )
    parser.add_argument(
        "--bash",
        action="store_true",
        help="(--suite real only) run codegen/test-writer tasks in edit+bash mode; falls back to "
        "edit with a warning if the CLI rejects that mode.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the cost-estimate confirmation gate (required when the estimate exceeds $5).",
    )
    parser.add_argument(
        "--orchestrator-price-per-mtok",
        type=float,
        default=5.0,
        help="(--suite real only) USD per million input tokens, for orchestrator_read_cost_est "
        "(default: 5.0, a rough premium-model input price).",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="(--suite real only) optional OpenRouter model id to re-grade R1/R2/R3/R7 with an "
        "LLM judge; stored alongside the heuristic score, never replacing it. This is the "
        "recommended way to get a reliable number for those four heuristically-scored tasks.",
    )
    parser.add_argument(
        "--rescore",
        default=None,
        metavar="RUN_DIR",
        help="Re-apply the CURRENT scorers to a saved run's results_real.jsonl with NO model "
        "calls (offline): uv run python bakeoff/run.py --rescore bakeoff/runs/<dir>. Writes "
        "results_real.rescored.jsonl and summary_real.rescored.md next to the originals, never "
        "overwriting them. All other flags are ignored in this mode.",
    )
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if args.suite == "real":
        from real.runner import run_real_suite
        from real.tasks import ALL_TASK_IDS

        models = [m.strip() for m in args.models.split(",")] if args.models else default_models()
        models = [m for m in models if m]
        if not models:
            print("error: --models must list at least one model id", file=sys.stderr)
            return 2
        task_ids = (
            [t.strip().upper() for t in args.tasks.split(",")] if args.tasks else list(ALL_TASK_IDS)
        )
        task_ids = [t for t in task_ids if t]

        out_dir = Path(args.out_dir) if args.out_dir else RUNS_DIR / now_stamp()
        out_dir.mkdir(parents=True, exist_ok=True)
        return await run_real_suite(args, out_dir, models, task_ids)

    return await main_async_smoke(args)


async def main_async_smoke(args: argparse.Namespace) -> int:
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = default_models()
    task_ids = parse_int_list(args.tasks or "1,2,3")
    for task_id in task_ids:
        if task_id not in TASKS:
            print(f"error: unknown task id {task_id!r} (valid: {sorted(TASKS)})", file=sys.stderr)
            return 2
    if not models:
        print("error: --models must list at least one model id", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else RUNS_DIR / now_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print(
            "warning: OPENROUTER_API_KEY is not set; the worker CLI will likely fail every run. "
            "Use --dry-run to test the harness without it.",
            file=sys.stderr,
        )

    semaphore = asyncio.Semaphore(max(1, args.parallel))
    coros = [
        run_one(model, task_id, repeat, args, out_dir, semaphore)
        for model in models
        for task_id in task_ids
        for repeat in range(1, args.repeats + 1)
    ]

    print(
        f"Running {len(coros)} (model, task, repeat) combinations "
        f"(parallel={args.parallel}, dry_run={args.dry_run})..."
    )
    records = await asyncio.gather(*coros)

    results_path = out_dir / "results.jsonl"
    with results_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(asdict(rec), default=str) + "\n")

    summary = summarize(list(records))
    summary_md = render_summary_md(summary, args)
    (out_dir / "summary.md").write_text(summary_md)

    print()
    print(summary_md)
    print(f"\nWrote {len(records)} records to {results_path}")
    print(f"Wrote summary to {out_dir / 'summary.md'}")
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.rescore:
        from real.rescore import run_rescore

        return run_rescore(Path(args.rescore), timeout=args.timeout)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
