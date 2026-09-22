"""Verifies the bake-off harness's handling of infra/API failures (item 5):
a fatal 401/402/403 aborts the rest of the invocation instead of burning
through it, score averages exclude error/harness-failure runs (a
credit-exhausted run must not drag a model's average down), timeouts are
tracked separately, and errored runs are reported rather than silently
folded into the score.
"""

from __future__ import annotations

import argparse

import pytest
from real import runner
from real.report import RealRunRecord, aggregate, render_summary_md


def _rec(
    model: str, task_id: str, repeat: int, status: str, overall_score, error=None
) -> RealRunRecord:
    return RealRunRecord(
        model=model,
        task_id=task_id,
        repeat=repeat,
        status=status,
        score={"overall": overall_score},
        overall_score=overall_score,
        usage={"cost": 0.01},
        turns=1,
        tool_calls=1,
        invalid_tool_calls=0,
        engine_duration_s=1.0,
        wall_time_s=1.0,
        report_tokens_est=1.0,
        orchestrator_read_cost_est=0.0,
        final_message="x",
        error=error,
    )


def test_error_classification():
    assert runner._is_fatal_auth_error("OpenRouter returned HTTP 401") is True
    assert runner._is_fatal_auth_error("OpenRouter returned HTTP 403: forbidden") is True
    assert runner._is_fatal_auth_error("OpenRouter returned HTTP 402") is False
    assert runner._is_fatal_auth_error("OpenRouter returned HTTP 500") is False
    assert runner._is_fatal_auth_error(None) is False
    assert runner._is_payment_error("OpenRouter returned HTTP 402: fewer max_tokens") is True
    assert runner._is_payment_error("subprocess exceeded 600s") is False


def test_aggregate_excludes_error_runs_from_score_average():
    """A 0.0 from an infra failure (HTTP 402) must not be averaged in with
    genuine model scores -- this is the exact qwen/qwen3.8-27b bug from the
    2026-09-18 run (20/24 runs errored on credit exhaustion, and those
    zeros were averaged into its scores).
    """
    records = [
        _rec("m", "R1", 1, "completed", 1.0),
        _rec("m", "R1", 2, "error", 0.0, error="OpenRouter returned HTTP 402"),
        _rec("m", "R1", 3, "error", 0.0, error="OpenRouter returned HTTP 402"),
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    data = summary["per_model"]["m"]
    assert data["per_task_avg_score"]["R1"] == 1.0  # only the completed run counts
    assert data["per_task_repeats"]["R1"] == 1
    assert len(data["errored_runs"]) == 2


def test_aggregate_keeps_timeout_and_max_turns_in_score_average():
    """Unlike an infra error, a timeout/max_turns run is real model
    behavior and must still count toward the average.
    """
    records = [
        _rec("m", "R6", 1, "completed", 1.0),
        _rec("m", "R6", 2, "timeout", 0.0),
        _rec("m", "R6", 3, "max_turns", 0.2),
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    data = summary["per_model"]["m"]
    assert data["per_task_repeats"]["R6"] == 3
    assert data["errored_runs"] == []
    assert data["timeouts"] == 1


def test_aggregate_timeouts_column_counts_harness_timeout_too():
    records = [
        _rec("m", "R4", 1, "timeout", 0.0),
        _rec("m", "R4", 2, "harness_timeout", None, error="subprocess exceeded 660s"),
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    data = summary["per_model"]["m"]
    assert data["timeouts"] == 2
    assert len(data["errored_runs"]) == 1  # only harness_timeout is a non-scoring status


def test_render_summary_md_includes_errored_runs_table():
    records = [
        _rec("m", "R1", 1, "error", 0.0, error="OpenRouter returned HTTP 402"),
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    md = render_summary_md(summary, price_per_mtok=5.0)
    assert "Errored runs" in md
    assert "HTTP 402" in md
    assert "Timeouts" in md


def _base_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        dry_run=True,
        repeats=1,
        max_turns=5,
        timeout=30,
        parallel=1,
        bash=False,
        yes=True,
        orchestrator_price_per_mtok=5.0,
        judge_model=None,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


@pytest.mark.asyncio
async def test_run_real_suite_aborts_after_fatal_error(tmp_path, monkeypatch):
    """Once a fatal 401 comes back, no further (model, task, repeat)
    combination should actually be invoked -- the rest are reported as
    `aborted` instead of burning through the batch.
    """
    calls = []

    async def _fake_run_one_cli_task(model, task, repeat, args, out_dir):
        calls.append((model, task.task_id, repeat))
        if model == "broke-model":
            return _rec(
                model, task.task_id, repeat, "error", 0.0, error="OpenRouter returned HTTP 401"
            )
        return _rec(model, task.task_id, repeat, "completed", 1.0)

    monkeypatch.setattr(runner, "_run_one_cli_task", _fake_run_one_cli_task)

    args = _base_args()
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    rc = await runner.run_real_suite(args, out_dir, ["broke-model", "good-model"], ["R1", "R2"])

    assert rc == 0
    results = (out_dir / "results_real.jsonl").read_text().splitlines()
    assert len(results) == 4  # 2 models x 2 tasks x 1 repeat
    # Not every combination was actually invoked -- the abort short-circuited the rest.
    assert len(calls) < 4

    import json as _json

    statuses = {_json.loads(line)["status"] for line in results}
    assert "aborted" in statuses
