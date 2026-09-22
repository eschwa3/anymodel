"""Cost estimation and summary aggregation/rendering for `--suite real`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from real.tasks import REAL_TASKS, ROLE_GROUPS

# Rough per-task-run cost estimates (USD), used only for the pre-flight
# estimate printed before a real (non --dry-run) run -- actual cost is
# whatever OpenRouter reports afterward. Based on v1's observed range
# ($0.005-$0.10/run); read-only tasks are cheaper (less context, no edits),
# edit tasks costlier, R8 is 3 sub-runs.
_COST_PER_RUN_ESTIMATE: dict[str, float] = {
    "R1": 0.02,
    "R2": 0.015,
    "R3": 0.02,
    "R4": 0.05,
    "R5": 0.06,
    "R6": 0.05,
    "R7": 0.02,
    "R8": 0.04,  # R8: per sub-task
}
_R8_SUBTASKS = 3


def estimate_cost(models: list[str], task_ids: list[str], repeats: int) -> float:
    total = 0.0
    for task_id in task_ids:
        per_run = _COST_PER_RUN_ESTIMATE.get(task_id, 0.03)
        multiplier = _R8_SUBTASKS if task_id == "R8" else 1
        total += per_run * multiplier * len(models) * repeats
    return round(total, 4)


@dataclass
class RealRunRecord:
    model: str
    task_id: str
    repeat: int
    status: str
    score: dict[str, Any]
    overall_score: float | None
    usage: dict[str, Any]
    turns: int
    tool_calls: int
    invalid_tool_calls: int
    engine_duration_s: float
    wall_time_s: float
    report_tokens_est: float
    orchestrator_read_cost_est: float
    final_message: str
    changed_files: list[str] = field(default_factory=list)
    error: str | None = None
    patch_path: str | None = None
    # Bash usage from the run's transcript (see bash_stats): did `--bash`
    # actually buy the worker test feedback? Empty for R8 (whose workers run
    # through the JobManager with their own transcripts) and for old results
    # files that predate the field.
    bash_stats: dict[str, int] = field(default_factory=dict)
    # Output tokens per wall-clock second for this run (usage's
    # completion_tokens / engine_duration_s), or None when either is missing
    # or zero -- see `tokens_per_second`. None for old results files that
    # predate this field.
    output_tokens_per_s: float | None = None


# Statuses that mean "we don't have a real signal about the model's
# capability for this run" -- an infra/API failure, not model behavior.
# These are excluded from score averages (a 402/credit-exhausted run scored
# as a 0.0 previously dragged a model's average down for reasons that have
# nothing to do with the model) and reported separately instead.
# `timeout`/`max_turns` are deliberately NOT in this set: a model that runs
# out of turns or wall-clock time is real model behavior and should count.
NON_SCORING_STATUSES = frozenset({"error", "harness_error", "harness_timeout", "aborted"})

# Statuses that count as a "timeout" for the per-model timeouts column.
_TIMEOUT_STATUSES = frozenset({"timeout", "harness_timeout"})


def report_tokens_est(final_message: str) -> float:
    return round(len(final_message or "") / 4, 2)


def tokens_per_second(output_tokens: float | None, wall_s: float | None) -> float | None:
    """Output tokens per wall-clock second, or None if either input is missing or zero.

    Used for the speed columns in the summary (informational -- never folds
    into quality/score, see bakeoff/README.md).
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


def orchestrator_read_cost_est(tokens_est: float, price_per_mtok: float) -> float:
    return round(tokens_est / 1_000_000 * price_per_mtok, 6)


# A pytest run that actually produced results (as opposed to one that never
# started -- "No module named pytest", command not found, a rejected call...).
_PYTEST_RESULT_RE = re.compile(r"\d+ (passed|failed|error)")


def _walk_json_objects(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield every JSON object in a decoded transcript structure, recursively."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_json_objects(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_json_objects(value)


def bash_stats(transcript_path: Path) -> dict[str, int]:
    """Count Bash tool calls in a worker transcript (JSON Lines): how many
    Bash calls it made, how many were rejected (result text starting with
    "Error:"), and how many pytest runs produced a real result line
    ("N passed/failed/error").

    Assistant messages carry `tool_calls` items shaped
    `{"id": ..., "function": {"name": "Bash", "arguments": "<json string>"}}`;
    tool results are objects with `role == "tool"`, a `tool_call_id`, and
    string `content`. The same message can appear in the file more than once
    (each transcript line re-lists the messages since the last turn), so
    calls and results are both deduped by tool_call_id. A missing, unreadable,
    or empty transcript -- e.g. any `--dry-run` run -- counts as all zeros;
    this never raises.
    """
    try:
        text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"bash_calls": 0, "bash_rejected": 0, "test_runs_with_results": 0}

    commands: dict[Any, str] = {}
    results: dict[Any, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk_json_objects(record):
            tool_calls = node.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = call.get("id")
                    fn = call.get("function")
                    if not isinstance(fn, dict) or fn.get("name") != "Bash":
                        continue
                    args = fn.get("arguments")
                    try:
                        parsed = json.loads(args) if isinstance(args, str) else args
                    except (json.JSONDecodeError, ValueError):
                        continue
                    command = parsed.get("command") if isinstance(parsed, dict) else None
                    if call_id is not None and isinstance(command, str):
                        commands.setdefault(call_id, command)
            if node.get("role") == "tool" and node.get("tool_call_id") is not None:
                content = node.get("content")
                if isinstance(content, str):
                    results.setdefault(node["tool_call_id"], content)

    rejected = sum(1 for cid in commands if results.get(cid, "").startswith("Error:"))
    with_results = 0
    for cid, command in commands.items():
        if "pytest" in command and _PYTEST_RESULT_RE.search(results.get(cid, "")):
            with_results += 1
    return {
        "bash_calls": len(commands),
        "bash_rejected": rejected,
        "test_runs_with_results": with_results,
    }


def _safe_div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def aggregate(records: list[RealRunRecord], price_per_mtok: float) -> dict[str, Any]:
    by_model: dict[str, list[RealRunRecord]] = {}
    for rec in records:
        by_model.setdefault(rec.model, []).append(rec)

    per_model: dict[str, Any] = {}
    for model, recs in by_model.items():
        per_task: dict[str, list[float]] = {}
        total_cost = 0.0
        total_turns = 0
        total_tool_calls = 0
        total_invalid = 0
        total_engine_duration = 0.0
        total_read_cost = 0.0
        status_counts: dict[str, int] = {}
        errored_runs: list[dict[str, Any]] = []
        timeouts = 0
        total_bash_calls = 0
        total_bash_rejected = 0
        total_test_runs = 0
        durations_s: list[float] = []
        tok_per_s_values: list[float] = []

        for rec in recs:
            status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
            total_cost += rec.usage.get("cost") or 0.0
            total_turns += rec.turns
            total_tool_calls += rec.tool_calls
            total_invalid += rec.invalid_tool_calls
            total_engine_duration += rec.engine_duration_s
            durations_s.append(rec.engine_duration_s)
            if rec.output_tokens_per_s is not None:
                tok_per_s_values.append(rec.output_tokens_per_s)
            total_read_cost += rec.orchestrator_read_cost_est
            total_bash_calls += (rec.bash_stats or {}).get("bash_calls", 0)
            total_bash_rejected += (rec.bash_stats or {}).get("bash_rejected", 0)
            total_test_runs += (rec.bash_stats or {}).get("test_runs_with_results", 0)
            if rec.status in _TIMEOUT_STATUSES:
                timeouts += 1
            if rec.status in NON_SCORING_STATUSES:
                errored_runs.append(
                    {
                        "task_id": rec.task_id,
                        "repeat": rec.repeat,
                        "status": rec.status,
                        "error": rec.error,
                    }
                )
                continue  # infra/API failure -- not a model-capability signal
            if rec.overall_score is not None:
                per_task.setdefault(rec.task_id, []).append(rec.overall_score)

        n = len(recs)
        avg_scores = {tid: round(sum(v) / len(v), 4) for tid, v in per_task.items() if v}
        overall_avg = round(sum(avg_scores.values()) / len(avg_scores), 4) if avg_scores else None

        per_model[model] = {
            "n_runs": n,
            "status_counts": status_counts,
            "per_task_avg_score": avg_scores,
            "per_task_repeats": {tid: len(v) for tid, v in per_task.items()},
            "overall_avg_score": overall_avg,
            "total_cost_usd": round(total_cost, 6),
            "avg_turns": round(total_turns / n, 2) if n else 0,
            "avg_tool_calls": round(total_tool_calls / n, 2) if n else 0,
            "invalid_tool_call_rate": _safe_div(total_invalid, total_tool_calls) or 0.0,
            "avg_engine_duration_s": round(total_engine_duration / n, 2) if n else 0,
            "total_orchestrator_read_cost_est": round(total_read_cost, 6),
            "score_per_dollar": _safe_div(overall_avg or 0.0, total_cost) if total_cost else None,
            "timeouts": timeouts,
            "errored_runs": errored_runs,
            "bash_calls": total_bash_calls,
            "bash_rejected": total_bash_rejected,
            "test_runs_with_results": total_test_runs,
            # Speed, informational only -- never folds into score/ranking (see
            # bakeoff/README.md). Computed over all recs (including
            # errored/aborted ones, whose engine_duration_s is 0.0), matching
            # avg_engine_duration_s above for consistency.
            "median_engine_duration_s": _median(durations_s),
            "median_output_tokens_per_s": _median(tok_per_s_values),
            "total_engine_duration_s": round(sum(durations_s), 2),
        }

    # Per-role rankings.
    role_rankings: dict[str, Any] = {}
    for role, task_ids in ROLE_GROUPS.items():
        rows = []
        for model, data in per_model.items():
            scores = [
                data["per_task_avg_score"][t] for t in task_ids if t in data["per_task_avg_score"]
            ]
            repeats = [data["per_task_repeats"].get(t, 0) for t in task_ids]
            if not scores:
                continue
            avg = round(sum(scores) / len(scores), 4)
            low_confidence = any(r < 3 for r in repeats)
            rows.append(
                {
                    "model": model,
                    "avg_score": avg,
                    "total_cost_usd": data["total_cost_usd"],
                    "low_confidence": low_confidence,
                }
            )
        rows.sort(key=lambda r: (-r["avg_score"], r["total_cost_usd"]))
        role_rankings[role] = rows

    # Injection obedience table (R7).
    obedience_rows = []
    for model, recs in by_model.items():
        r7_recs = [r for r in recs if r.task_id == "R7"]
        if not r7_recs:
            continue
        obedient_count = sum(1 for r in r7_recs if r.score.get("obedient"))
        obedience_rows.append(
            {
                "model": model,
                "n": len(r7_recs),
                "obedient_count": obedient_count,
                "obedience_rate": round(obedient_count / len(r7_recs), 4),
            }
        )
    obedience_rows.sort(key=lambda r: r["obedience_rate"])

    # Task x model matrix.
    matrix: dict[str, dict[str, float | None]] = {tid: {} for tid in REAL_TASKS}
    for model, data in per_model.items():
        for tid in REAL_TASKS:
            matrix[tid][model] = data["per_task_avg_score"].get(tid)

    return {
        "per_model": per_model,
        "role_rankings": role_rankings,
        "injection_obedience": obedience_rows,
        "task_model_matrix": matrix,
    }


def render_summary_md(summary: dict[str, Any], price_per_mtok: float) -> str:
    lines = ["# Bake-off summary (--suite real)", ""]

    lines.append("## Per-model (score averages exclude errored runs -- see 'Errored runs' below)")
    lines.append("")
    lines.append(
        "| Model | Runs | Overall avg score | Total cost (USD) | Score/$ | "
        "Avg engine duration (s) | Median s/task | Median tok/s | Total wall s | "
        "Invalid tool-call rate | Orchestrator read cost est (USD) | "
        "Timeouts | Errored runs | Bash (calls / rejected / pytest-ok) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for model, data in summary["per_model"].items():
        median_s = data["median_engine_duration_s"]
        median_tps = data["median_output_tokens_per_s"]
        lines.append(
            f"| {model} | {data['n_runs']} | {data['overall_avg_score']} | "
            f"{data['total_cost_usd']} | {data['score_per_dollar']} | "
            f"{data['avg_engine_duration_s']} | "
            f"{median_s if median_s is not None else '-'} | "
            f"{median_tps if median_tps is not None else '-'} | "
            f"{data['total_engine_duration_s']} | "
            f"{data['invalid_tool_call_rate']} | "
            f"{data['total_orchestrator_read_cost_est']} | {data['timeouts']} | "
            f"{len(data['errored_runs'])} | "
            f"{data['bash_calls']} / {data['bash_rejected']} / "
            f"{data['test_runs_with_results']} |"
        )
    lines.append("")

    lines.append("## Task x model matrix (avg score, 0-1)")
    lines.append("")
    models = list(summary["per_model"])
    lines.append("| Task | " + " | ".join(models) + " |")
    lines.append("|---|" + "---|" * len(models))
    for tid, row in summary["task_model_matrix"].items():
        cells = [str(row.get(m, "")) for m in models]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Per-role rankings (score first, then cost; low_confidence = <3 repeats)")
    lines.append("")
    for role, rows in summary["role_rankings"].items():
        lines.append(f"### {role}")
        lines.append("")
        lines.append("| Rank | Model | Avg score | Total cost (USD) | Low confidence |")
        lines.append("|---|---|---|---|---|")
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"| {i} | {row['model']} | {row['avg_score']} | {row['total_cost_usd']} | "
                f"{'yes' if row['low_confidence'] else 'no'} |"
            )
        if rows:
            recommended = rows[0]
            note = " (low confidence)" if recommended["low_confidence"] else ""
            lines.append(f"\n**Recommended for {role}: {recommended['model']}{note}**")
        lines.append("")

    lines.append("## Injection obedience (R7) -- lower is better/safer")
    lines.append("")
    lines.append("| Model | Runs | Obedient (leaked canary or malicious rec.) | Obedience rate |")
    lines.append("|---|---|---|---|")
    for row in summary["injection_obedience"]:
        lines.append(
            f"| {row['model']} | {row['n']} | {row['obedient_count']} | {row['obedience_rate']} |"
        )
    lines.append("")

    lines.append(
        "## Errored runs (excluded from score averages above -- infra/API failures, "
        "not model behavior; timeout/max_turns runs are NOT listed here, they count normally)"
    )
    lines.append("")
    any_errors = any(data["errored_runs"] for data in summary["per_model"].values())
    if not any_errors:
        lines.append("None.")
    else:
        lines.append("| Model | Task | Repeat | Status | Error |")
        lines.append("|---|---|---|---|---|")
        for model, data in summary["per_model"].items():
            for row in data["errored_runs"]:
                lines.append(
                    f"| {model} | {row['task_id']} | {row['repeat']} | {row['status']} | "
                    f"{row['error']} |"
                )
    lines.append("")

    return "\n".join(lines)
