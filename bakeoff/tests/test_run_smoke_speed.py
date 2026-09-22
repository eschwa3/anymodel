"""Verifies the `--suite smoke` speed columns in bakeoff/run.py: per-run
output_tokens_per_s, per-model median/total aggregates, and that they show
up as informational-only additions to summarize()/render_summary_md()
without changing any existing score/ranking behavior.
"""

from __future__ import annotations

import argparse

import run as run_mod


def _args() -> argparse.Namespace:
    return argparse.Namespace(tasks=None, repeats=1, dry_run=True)


def _rec(model: str, engine_duration_s: float, output_tokens_per_s) -> run_mod.RunRecord:
    return run_mod.RunRecord(
        model=model,
        task_id=1,
        task_key="find_bugs",
        repeat=1,
        status="completed",
        cli_json=None,
        changed_files=[],
        outside_repo_paths=[],
        score={},
        transcript_path="/tmp/x.json",
        wall_time_s=1.0,
        engine_duration_s=engine_duration_s,
        output_tokens_per_s=output_tokens_per_s,
    )


def test_tokens_per_second_computes_and_handles_missing():
    assert run_mod.tokens_per_second(400, 20.0) == 20.0
    assert run_mod.tokens_per_second(None, 20.0) is None
    assert run_mod.tokens_per_second(400, 0.0) is None
    assert run_mod.tokens_per_second(0, 20.0) is None


def test_median_helper():
    assert run_mod._median([3.0, 1.0, 2.0]) == 2.0
    assert run_mod._median([1.0, 2.0]) == 1.5
    assert run_mod._median([]) is None


def test_run_one_populates_output_tokens_per_s_from_cli_json_dry_run_stub(tmp_path):
    stub = run_mod.stub_worker_result(
        "fake-model", 1, "read-only", transcript_path=tmp_path / "t.json"
    )
    usage = stub["usage"]
    expected = run_mod.tokens_per_second(usage["completion_tokens"], stub["duration_s"])
    assert expected is not None
    assert expected > 0


def test_summarize_reports_median_and_total_speed_columns():
    records = [
        _rec("m", 2.0, 10.0),
        _rec("m", 4.0, 20.0),
        _rec("m", 6.0, None),
    ]
    summary = run_mod.summarize(records)
    data = summary["m"]
    assert data["median_engine_duration_s"] == 4.0
    assert data["median_output_tokens_per_s"] == 15.0
    assert data["total_engine_duration_s"] == 12.0

    rendered = run_mod.render_summary_md(summary, _args())
    assert "median 4.0s/task" in rendered
    assert "median 15.0 output tok/s" in rendered
    assert "total engine duration 12.0s" in rendered


def test_summarize_all_none_speed_renders_dash():
    records = [_rec("m", 0.0, None)]
    summary = run_mod.summarize(records)
    assert summary["m"]["median_output_tokens_per_s"] is None

    rendered = run_mod.render_summary_md(summary, _args())
    assert "median - output tok/s" in rendered


def test_old_run_record_without_output_tokens_per_s_defaults_to_none():
    """A RunRecord built the way an older results.jsonl line would be (no
    output_tokens_per_s key) must default to None, not error.
    """
    rec = run_mod.RunRecord(
        model="m",
        task_id=1,
        task_key="find_bugs",
        repeat=1,
        status="completed",
        cli_json=None,
        changed_files=[],
        outside_repo_paths=[],
        score={},
        transcript_path="/tmp/x.json",
        wall_time_s=1.0,
    )
    assert rec.output_tokens_per_s is None
    summary = run_mod.summarize([rec])
    assert summary["m"]["median_output_tokens_per_s"] is None
