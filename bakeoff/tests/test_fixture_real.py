"""Verifies the fixture_real history/overlay builder and the fixture's own
test suite -- i.e. that `real.fixture` produces a working git repo, that the
base app's tests pass, and that R1's planted regression is real (breaks an
existing test) while R2's addition is genuinely clean.
"""

from __future__ import annotations

import subprocess
import sys

from real import fixture


def _pytest(repo_dir, target="tests"):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def test_base_repo_has_three_commits(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_base_repo(dest)
    messages = fixture.git_log_messages(dest)
    assert len(messages) == 3


def test_base_repo_tests_pass(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_base_repo(dest)
    rc, output = _pytest(dest)
    assert rc == 0, output


def test_r1_overlay_adds_one_commit_and_introduces_regression(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R1")
    messages = fixture.git_log_messages(dest)
    assert len(messages) == 4

    diff_text = fixture.git_diff_head(dest)
    assert "search_by_name" in diff_text
    assert "mark_many_done" in diff_text

    rc, output = _pytest(dest, "tests/test_scheduler.py")
    assert rc != 0, "expected the retry-limit regression to break an existing test"
    assert "test_fail_job_reaches_limit_marks_dead" in output


def test_r1_diff_touches_multiple_files_with_substantial_size(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R1")
    stat = fixture.git_diff_stat_head(dest)
    # "multi-file" and "substantial": several files, not a one-line tweak.
    file_lines = [line for line in stat.splitlines() if "|" in line]
    assert len(file_lines) >= 5
    full_diff_line_count = len(fixture.git_diff_head(dest).splitlines())
    assert full_diff_line_count >= 150


def test_r2_overlay_is_clean(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R2")
    messages = fixture.git_log_messages(dest)
    assert len(messages) == 4

    rc, output = _pytest(dest)
    assert rc == 0, output

    diff_text = fixture.git_diff_head(dest)
    assert "get_customer_summary" in diff_text
