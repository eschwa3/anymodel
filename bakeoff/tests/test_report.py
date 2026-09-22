"""Verifies report.bash_stats (the --bash effectiveness metric: did the
worker actually get test results out of Bash?) and that the per-model
summary carries its totals -- including for records/old results files that
predate the bash_stats field.
"""

from __future__ import annotations

import json

from real.report import (
    RealRunRecord,
    _median,
    aggregate,
    bash_stats,
    render_summary_md,
    tokens_per_second,
)


def _fixture_lines() -> list[dict]:
    """A small hand-written transcript covering: one rejected Bash call, one
    pytest call with real results ("3 passed"), one pytest call that failed
    to start, one non-Bash tool call, and a duplicated message (which must
    not double-count -- dedupe is by tool_call_id).
    """
    user_msg = {"role": "user", "content": "fix the failing tests"}
    rejected_call = {
        "id": "call_rej",
        "function": {"name": "Bash", "arguments": json.dumps({"command": "ls -la"})},
    }
    rejected_result = {
        "role": "tool",
        "tool_call_id": "call_rej",
        "content": "Error: command 'ls -la' is not on the allowlist",
    }
    pytest_ok_call = {
        "id": "call_ok",
        "function": {"name": "Bash", "arguments": json.dumps({"command": "uv run pytest -q"})},
    }
    pytest_ok_result = {"role": "tool", "tool_call_id": "call_ok", "content": "3 passed in 0.12s"}
    pytest_broken_call = {
        "id": "call_broken",
        "function": {"name": "Bash", "arguments": json.dumps({"command": "pytest tests/"})},
    }
    pytest_broken_result = {
        "role": "tool",
        "tool_call_id": "call_broken",
        "content": "No module named pytest",
    }
    read_call = {
        "id": "call_read",
        "function": {"name": "Read", "arguments": json.dumps({"file_path": "x.py"})},
    }
    read_result = {"role": "tool", "tool_call_id": "call_read", "content": "file contents"}

    turn_2 = {
        "turn": 2,
        "new_messages": [
            user_msg,
            {"role": "assistant", "tool_calls": [rejected_call]},
            rejected_result,
        ],
        "response_message": {"role": "assistant", "tool_calls": [pytest_ok_call, read_call]},
    }
    return [
        {
            "turn": 1,
            "new_messages": [user_msg],
            "response_message": {"role": "assistant", "tool_calls": [rejected_call]},
        },
        turn_2,
        {
            "turn": 3,
            "new_messages": [pytest_ok_result, read_result],
            "response_message": {"role": "assistant", "tool_calls": [pytest_broken_call]},
        },
        {
            "turn": 4,
            "new_messages": [pytest_broken_result],
            "response_message": {"role": "assistant", "content": "done"},
        },
        # The same message appearing twice in the file: deduped by id.
        turn_2,
    ]


def test_bash_stats_counts_rejections_and_test_results(tmp_path):
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        "\n".join(json.dumps(line) for line in _fixture_lines()) + "\n", encoding="utf-8"
    )

    assert bash_stats(transcript) == {
        "bash_calls": 3,
        "bash_rejected": 1,
        "test_runs_with_results": 1,
    }


def test_bash_stats_missing_or_empty_transcript_is_zeros(tmp_path):
    zeros = {"bash_calls": 0, "bash_rejected": 0, "test_runs_with_results": 0}
    assert bash_stats(tmp_path / "missing.json") == zeros
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert bash_stats(empty) == zeros


def _rec(model: str, stats: dict[str, int], **overrides) -> RealRunRecord:
    kwargs = {
        "model": model,
        "task_id": "R4",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 1.0},
        "overall_score": 1.0,
        "usage": {"cost": 0.01},
        "turns": 3,
        "tool_calls": 5,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 1.0,
        "orchestrator_read_cost_est": 0.0,
        "final_message": "done",
        "bash_stats": stats,
    }
    kwargs.update(overrides)
    return RealRunRecord(**kwargs)


def test_aggregate_and_summary_report_bash_stats_totals():
    records = [
        _rec("m", {"bash_calls": 4, "bash_rejected": 1, "test_runs_with_results": 2}),
        # An edit-only (or R8, or old-results-file) record: contributes zeros.
        _rec("m", {}),
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    model = summary["per_model"]["m"]
    assert model["bash_calls"] == 4
    assert model["bash_rejected"] == 1
    assert model["test_runs_with_results"] == 2

    rendered = render_summary_md(summary, price_per_mtok=5.0)
    assert "Bash (calls / rejected / pytest-ok)" in rendered
    assert "| 4 / 1 / 2 |" in rendered


# --- Speed metrics: tokens_per_second, _median, and the summary's speed
# columns (informational only -- must never affect ranking order). ---------


def test_tokens_per_second_computes():
    assert tokens_per_second(300, 10.0) == 30.0


def test_tokens_per_second_none_when_missing_or_zero():
    assert tokens_per_second(None, 10.0) is None
    assert tokens_per_second(0, 10.0) is None
    assert tokens_per_second(300, 0.0) is None
    assert tokens_per_second(300, None) is None


def test_median_odd_even_and_empty():
    assert _median([1.0, 3.0, 2.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert _median([]) is None


def test_aggregate_speed_columns_median_and_total():
    records = [
        _rec("m", {}, engine_duration_s=2.0, output_tokens_per_s=10.0),
        _rec("m", {}, engine_duration_s=4.0, output_tokens_per_s=20.0),
        _rec("m", {}, engine_duration_s=6.0, output_tokens_per_s=None),  # e.g. zero tokens
    ]
    summary = aggregate(records, price_per_mtok=5.0)
    model = summary["per_model"]["m"]
    assert model["median_engine_duration_s"] == 4.0
    assert model["median_output_tokens_per_s"] == 15.0  # None excluded from the median
    assert model["total_engine_duration_s"] == 12.0

    rendered = render_summary_md(summary, price_per_mtok=5.0)
    assert "Median s/task" in rendered
    assert "Median tok/s" in rendered
    assert "Total wall s" in rendered
    assert "| 4.0 | 15.0 | 12.0 |" in rendered


def test_aggregate_speed_all_none_renders_dash():
    records = [_rec("m", {}, engine_duration_s=0.0, output_tokens_per_s=None)]
    summary = aggregate(records, price_per_mtok=5.0)
    model = summary["per_model"]["m"]
    assert model["median_output_tokens_per_s"] is None

    rendered = render_summary_md(summary, price_per_mtok=5.0)
    assert "| 0.0 | - |" in rendered  # median duration 0.0, median tok/s "-"


def test_old_record_without_output_tokens_per_s_defaults_to_none():
    """A RealRunRecord built the way an older results_real.jsonl line would
    be (no output_tokens_per_s key at all) must default to None, not error.
    """
    rec = _rec("m", {})
    assert rec.output_tokens_per_s is None


def test_speed_columns_do_not_change_role_ranking_order():
    """Quality stays the gate: a slower-but-higher-scoring model must still
    rank above a faster-but-lower-scoring one in role_rankings.
    """
    fast_low_score = _rec(
        "fast-low",
        {},
        task_id="R1",
        overall_score=0.2,
        score={"overall": 0.2},
        engine_duration_s=1.0,
        output_tokens_per_s=500.0,
    )
    slow_high_score = _rec(
        "slow-high",
        {},
        task_id="R1",
        overall_score=0.9,
        score={"overall": 0.9},
        engine_duration_s=60.0,
        output_tokens_per_s=5.0,
    )
    summary = aggregate([fast_low_score, slow_high_score], price_per_mtok=5.0)
    reviewer_rows = summary["role_rankings"]["reviewer"]
    assert [r["model"] for r in reviewer_rows] == ["slow-high", "fast-low"]
