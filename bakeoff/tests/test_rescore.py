"""Verifies `--rescore` (real/rescore.py): recomputing scores from saved
`results_real.jsonl` + `patches/` with no model calls, always producing a
complete output where every record either got genuinely re-scored or
carries its original score forward tagged `rescored: false`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from real.report import aggregate, render_summary_md
from real.rescore import _dict_to_real_run_record, rescore_record, run_rescore

R8_PATCH_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "r8_patches"
HIDDEN_REAL_DIR = Path(__file__).resolve().parent.parent / "hidden_real"


def _r1_record(final_message: str, **overrides) -> dict:
    rec = {
        "model": "fake/model",
        "task_id": "R1",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 0.0},
        "overall_score": 0.0,
        "usage": {"cost": 0.01},
        "turns": 3,
        "tool_calls": 3,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 10.0,
        "orchestrator_read_cost_est": 0.0001,
        "final_message": final_message,
        "changed_files": [],
        "error": None,
        "patch_path": None,
    }
    rec.update(overrides)
    return rec


def test_rescore_free_text_recomputes_from_final_message():
    good_msg = (
        "1. jobsched/scheduler.py line 49: off-by-one -- uses > instead of >= cfg.max_job_retries.\n"
        "2. jobsched/repository.py mark_many_done never commits, silently discarded.\n"
        "3. jobsched/repository.py search_by_name f-string SQL injection.\n"
    )
    rec = _r1_record(good_msg, score={"overall": 0.1}, overall_score=0.1)
    out = rescore_record(rec, patches_dir=Path("/nonexistent"), timeout=60)
    assert out["rescored"] is True
    assert out["overall_score"] > 0.5  # recomputed correctly, unlike the stale 0.1 stored


def test_rescore_skips_non_scoring_statuses():
    rec = _r1_record("irrelevant", status="error", error="OpenRouter returned HTTP 402")
    out = rescore_record(rec, patches_dir=Path("/nonexistent"), timeout=60)
    assert out["rescored"] is False
    assert out["overall_score"] == 0.0  # original value carried through unchanged


def test_rescore_edit_task_without_saved_patch_carries_old_score_forward():
    rec = {
        "model": "fake/model",
        "task_id": "R4",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 0.55, "note": "stale"},
        "overall_score": 0.55,
        "usage": {},
        "turns": 1,
        "tool_calls": 1,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 1.0,
        "orchestrator_read_cost_est": 0.0,
        "final_message": "fixed it",
        "changed_files": ["jobsched/billing.py"],
        "error": None,
        "patch_path": None,
    }
    out = rescore_record(rec, patches_dir=Path("/nonexistent-patches-dir"), timeout=60)
    assert out["rescored"] is False
    assert out["overall_score"] == 0.55
    assert out["score"]["note"] == "stale"


def test_rescore_edit_task_with_saved_patch_recomputes(tmp_path):
    patches_dir = tmp_path / "patches"
    patches_dir.mkdir()

    # Build a real patch: apply the reference R4 fix to a fresh fixture repo.
    import subprocess

    from real import fixture

    repo_dir = tmp_path / "repo"
    fixture.build_repo_for_task(repo_dir, "R4")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r4" / "billing.py", repo_dir / "jobsched" / "billing.py"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, capture_output=True, check=False)
    diff = subprocess.run(
        ["git", "diff", "--cached", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    slug = "fake_model__R4__r1"
    (patches_dir / f"{slug}.patch").write_text(diff)

    rec = {
        "model": "fake/model",
        "task_id": "R4",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 0.0},
        "overall_score": 0.0,
        "usage": {},
        "turns": 1,
        "tool_calls": 1,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 1.0,
        "orchestrator_read_cost_est": 0.0,
        "final_message": "fixed the rounding bug",
        "changed_files": ["jobsched/billing.py"],
        "error": None,
        "patch_path": str(patches_dir / f"{slug}.patch"),
    }
    out = rescore_record(rec, patches_dir=patches_dir, timeout=60)
    assert out["rescored"] is True
    assert out["overall_score"] == 1.0
    assert out["score"]["hidden_test_pass"] is True


def test_rescore_r8_with_all_worker_patches_recomputes(tmp_path):
    # `real.rescore._rescore_r8` looks for
    # "<model>__R8__r<repeat>__<worker-key>.patch"; copy the raw reference
    # patches into that naming convention for this (model, repeat).
    patches_dir = tmp_path / "patches"
    patches_dir.mkdir()
    slug = "fake_model__R8__r1"
    for worker_key in ("r8-worker-a", "r8-worker-b", "r8-worker-c"):
        shutil.copy2(
            R8_PATCH_FIXTURES / f"{worker_key}.patch", patches_dir / f"{slug}__{worker_key}.patch"
        )

    rec = {
        "model": "fake/model",
        "task_id": "R8",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 0.0},
        "overall_score": 0.0,
        "usage": {},
        "turns": 1,
        "tool_calls": 1,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 1.0,
        "orchestrator_read_cost_est": 0.0,
        "final_message": "{}",
        "changed_files": [],
        "error": None,
        "patch_path": None,
    }
    out = rescore_record(rec, patches_dir=patches_dir, timeout=60)
    assert out["rescored"] is True
    assert out["overall_score"] == 1.0


def test_rescore_r8_without_saved_patches_carries_old_score():
    rec = {
        "model": "fake/model",
        "task_id": "R8",
        "repeat": 1,
        "status": "completed",
        "score": {"overall": 0.42},
        "overall_score": 0.42,
        "usage": {},
        "turns": 1,
        "tool_calls": 1,
        "invalid_tool_calls": 0,
        "engine_duration_s": 1.0,
        "wall_time_s": 1.0,
        "report_tokens_est": 1.0,
        "orchestrator_read_cost_est": 0.0,
        "final_message": "{}",
        "changed_files": [],
        "error": None,
        "patch_path": None,
    }
    out = rescore_record(rec, patches_dir=Path("/nonexistent"), timeout=60)
    assert out["rescored"] is False
    assert out["overall_score"] == 0.42


def test_rescore_old_results_file_without_bash_stats_field():
    """Records saved before the bash_stats field existed must still rescore
    and aggregate into a summary (the field just stays empty) -- --rescore
    keeps working on old results files.
    """
    good_msg = (
        "1. jobsched/scheduler.py line 49: off-by-one -- uses > instead of >= cfg.max_job_retries.\n"
        "2. jobsched/repository.py mark_many_done never commits, silently discarded.\n"
        "3. jobsched/repository.py search_by_name f-string SQL injection.\n"
    )
    rec = _r1_record(good_msg)  # no "bash_stats" key, like every older run
    assert "bash_stats" not in rec

    out = rescore_record(rec, patches_dir=Path("/nonexistent"), timeout=60)
    assert out["rescored"] is True

    record = _dict_to_real_run_record(out)
    assert record.bash_stats == {}
    summary = aggregate([record], price_per_mtok=5.0)
    model = summary["per_model"]["fake/model"]
    assert model["bash_calls"] == 0
    assert "Bash (calls / rejected / pytest-ok)" in render_summary_md(summary, price_per_mtok=5.0)


def test_rescore_old_results_file_without_output_tokens_per_s_field():
    """Records saved before the output_tokens_per_s speed field existed
    must still rescore, rebuild into a RealRunRecord (defaulting to None),
    and render a summary showing "-" instead of erroring.
    """
    good_msg = (
        "1. jobsched/scheduler.py line 49: off-by-one -- uses > instead of >= cfg.max_job_retries.\n"
        "2. jobsched/repository.py mark_many_done never commits, silently discarded.\n"
        "3. jobsched/repository.py search_by_name f-string SQL injection.\n"
    )
    rec = _r1_record(good_msg)  # no "output_tokens_per_s" key, like every older run
    assert "output_tokens_per_s" not in rec

    out = rescore_record(rec, patches_dir=Path("/nonexistent"), timeout=60)
    assert out["rescored"] is True

    record = _dict_to_real_run_record(out)
    assert record.output_tokens_per_s is None
    summary = aggregate([record], price_per_mtok=5.0)
    model = summary["per_model"]["fake/model"]
    assert model["median_output_tokens_per_s"] is None
    rendered = render_summary_md(summary, price_per_mtok=5.0)
    assert "Median tok/s" in rendered
    assert "| - |" in rendered


def test_rescore_record_with_bash_stats_round_trips():
    """A record that carries bash_stats (post-feature) flows through the
    rescore path unchanged into the rebuilt RealRunRecord.
    """
    rec = _r1_record(
        "irrelevant",
        bash_stats={"bash_calls": 2, "bash_rejected": 1, "test_runs_with_results": 1},
    )
    record = _dict_to_real_run_record(rec)
    assert record.bash_stats == {"bash_calls": 2, "bash_rejected": 1, "test_runs_with_results": 1}


def test_run_rescore_end_to_end_writes_files_without_touching_originals(tmp_path):
    run_dir = tmp_path / "20260101-000000"
    run_dir.mkdir()
    original_records = [
        _r1_record(
            "1. jobsched/scheduler.py line 49: off-by-one -- uses > instead of >= cfg.max_job_retries.\n"
            "2. jobsched/repository.py mark_many_done never commits, silently discarded.\n"
            "3. jobsched/repository.py search_by_name f-string SQL injection.\n",
            score={"overall": 0.1},
            overall_score=0.1,
        ),
        _r1_record(
            "irrelevant", task_id="R2", status="error", error="OpenRouter returned HTTP 402"
        ),
    ]
    results_path = run_dir / "results_real.jsonl"
    original_text = "\n".join(json.dumps(r) for r in original_records) + "\n"
    results_path.write_text(original_text)

    rc = run_rescore(run_dir, timeout=60)
    assert rc == 0

    # Originals are untouched.
    assert results_path.read_text() == original_text
    assert not (run_dir / "summary_real.md").exists()

    rescored_path = run_dir / "results_real.rescored.jsonl"
    assert rescored_path.exists()
    rescored = [json.loads(line) for line in rescored_path.read_text().splitlines()]
    assert len(rescored) == 2
    assert rescored[0]["rescored"] is True
    assert rescored[0]["overall_score"] > 0.5
    assert rescored[1]["rescored"] is False

    summary_path = run_dir / "summary_real.rescored.md"
    assert summary_path.exists()
    assert "Rescored offline" in summary_path.read_text()
