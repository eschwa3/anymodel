"""Verifies the edit-task scorers (R4, R5, R6) against the reference
solutions (should score ~1.0) and against the unmodified/bad fixture
(should score low) -- proving each hidden test / static check / mutant set
actually discriminates.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from real import fixture
from real.scoring import ScoreContext, score_r4, score_r5, score_r6

HIDDEN_REAL_DIR = Path(__file__).resolve().parent.parent / "hidden_real"


# --------------------------------------------------------------------------
# R4 -- bugfix-from-symptom
# --------------------------------------------------------------------------


def test_r4_unfixed_repo_fails_hidden_test(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R4")
    ctx = ScoreContext(repo_dir=dest, final_message="", changed_files=[])
    result = score_r4(ctx)
    assert result["hidden_test_pass"] is False
    assert result["overall"] < 0.6


def test_r4_reference_fix_scores_high(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R4")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r4" / "billing.py", dest / "jobsched" / "billing.py"
    )
    ctx = ScoreContext(repo_dir=dest, final_message="fixed", changed_files=["jobsched/billing.py"])
    result = score_r4(ctx)
    assert result["hidden_test_pass"] is True
    assert result["existing_tests_pass"] is True
    assert result["overall"] == 1.0


def test_r4_sprawling_diff_penalized(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R4")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r4" / "billing.py", dest / "jobsched" / "billing.py"
    )
    # Simulate a worker that also rewrote an unrelated file substantially.
    unrelated = dest / "jobsched" / "notifications.py"
    unrelated.write_text(unrelated.read_text() + ("\n# padding line\n" * 150))
    ctx = ScoreContext(
        repo_dir=dest,
        final_message="fixed",
        changed_files=["jobsched/billing.py", "jobsched/notifications.py"],
    )
    result = score_r4(ctx)
    assert result["diff_size_ok"] is False
    assert result["overall"] < 1.0


def test_r4_reference_fix_plus_regression_test_scores_diff_ok(tmp_path):
    """A worker that fixes the bug *and* adds a regression test (which the
    codegen role prompt explicitly asks for) must not be penalized for it:
    only non-test source lines count against the diff-size threshold, and
    tests/ is an allowed path alongside jobsched/.
    """
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R4")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r4" / "billing.py", dest / "jobsched" / "billing.py"
    )
    test_path = dest / "tests" / "test_billing.py"
    test_path.write_text(test_path.read_text() + ("\n# a new regression test\n" * 40))
    ctx = ScoreContext(
        repo_dir=dest,
        final_message="fixed and added a regression test",
        changed_files=["jobsched/billing.py", "tests/test_billing.py"],
    )
    result = score_r4(ctx)
    assert result["files_outside_jobsched"] == []
    assert result["diff_size_ok"] is True
    assert result["overall"] == 1.0


def test_r4_diff_size_ok_ignores_test_line_count_against_threshold(tmp_path):
    """Even a large addition to the test file alone (well beyond the total
    diff-line threshold the old scorer used) must not trip `diff_size_ok`,
    since only non-test source lines are counted.
    """
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R4")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r4" / "billing.py", dest / "jobsched" / "billing.py"
    )
    test_path = dest / "tests" / "test_billing.py"
    test_path.write_text(test_path.read_text() + ("\n# a new regression test line\n" * 200))
    ctx = ScoreContext(
        repo_dir=dest,
        final_message="fixed and added a thorough regression test",
        changed_files=["jobsched/billing.py", "tests/test_billing.py"],
    )
    result = score_r4(ctx)
    assert result["test_diff_lines"] > 100
    assert result["diff_size_ok"] is True


# --------------------------------------------------------------------------
# R5 -- bulk-migration
# --------------------------------------------------------------------------


def test_r5_unmigrated_repo_scores_low(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    ctx = ScoreContext(repo_dir=dest, final_message="", changed_files=[])
    result = score_r5(ctx)
    assert result["migration_clean"] is False
    assert len(result["remaining_deprecated_calls"]) >= 12
    assert result["overall"] < 0.6


def test_r5_reference_migration_scores_high(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    ref_dir = HIDDEN_REAL_DIR / "reference" / "r5"
    for p in ref_dir.rglob("*.py"):
        rel = p.relative_to(ref_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    ctx = ScoreContext(repo_dir=dest, final_message="migrated", changed_files=[])
    result = score_r5(ctx)
    assert result["migration_clean"] is True
    assert result["legacy_site_untouched"] is True
    assert result["existing_tests_pass"] is True
    assert result["overall"] == 1.0


def test_r5_over_eager_migration_of_exempt_file_is_detected(tmp_path):
    """If a worker mistakenly migrates the one call site that must NOT
    change, the static scan (run with an empty exempt set) should no longer
    find a deprecated call there -- i.e. `legacy_site_untouched` goes False.
    """
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    legacy_path = dest / "jobsched" / "reports" / "legacy_export.py"
    text = legacy_path.read_text()
    legacy_path.write_text(
        text.replace(
            "from jobsched.utils.time import now",
            "from jobsched.utils.time import utcnow",
        ).replace("now()", "utcnow()")
    )
    ctx = ScoreContext(repo_dir=dest, final_message="migrated everything", changed_files=[])
    result = score_r5(ctx)
    assert result["legacy_site_untouched"] is False


# --------------------------------------------------------------------------
# R6 -- write-tests (notifications.py)
# --------------------------------------------------------------------------


def test_r6_no_test_file_scores_zero(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R6")
    ctx = ScoreContext(repo_dir=dest, final_message="", changed_files=[])
    result = score_r6(ctx)
    assert result["overall"] == 0.0


def test_r6_reference_tests_score_high_and_kill_all_mutants(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R6")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r6" / "reference_test_notifications.py",
        dest / "tests" / "test_notifications.py",
    )
    ctx = ScoreContext(
        repo_dir=dest, final_message="wrote tests", changed_files=["tests/test_notifications.py"]
    )
    result = score_r6(ctx)
    assert result["tests_pass_on_original"] is True
    assert result["mutants_killed"] == result["mutants_total"]
    assert result["mutants_total"] >= 8
    assert result["overall"] >= 0.95


def test_r6_weak_test_scores_low(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R6")
    (dest / "tests" / "test_notifications.py").write_text("def test_dummy():\n    assert True\n")
    ctx = ScoreContext(
        repo_dir=dest, final_message="wrote a test", changed_files=["tests/test_notifications.py"]
    )
    result = score_r6(ctx)
    assert result["overall"] < 0.3
    assert result["weak_test_patterns_hit"]


def test_r6_non_test_file_modification_penalized(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R6")
    shutil.copy2(
        HIDDEN_REAL_DIR / "reference" / "r6" / "reference_test_notifications.py",
        dest / "tests" / "test_notifications.py",
    )
    ctx = ScoreContext(
        repo_dir=dest,
        final_message="wrote tests and tweaked the module",
        changed_files=["tests/test_notifications.py", "jobsched/notifications.py"],
    )
    result = score_r6(ctx)
    assert result["non_test_files_modified"] == ["jobsched/notifications.py"]
    assert result["overall"] < 0.95
