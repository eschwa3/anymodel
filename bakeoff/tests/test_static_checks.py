"""Verifies the R5 AST-based deprecated-call scanner in isolation."""

from __future__ import annotations

import shutil
from pathlib import Path

from real import fixture, static_checks

HIDDEN_REAL_DIR = Path(__file__).resolve().parent.parent / "hidden_real"


def test_finds_at_least_twelve_sites_across_at_least_eight_files(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    findings = static_checks.find_deprecated_now_calls(dest)
    assert len(findings) >= 12
    assert len({f.file for f in findings}) >= 8


def test_exempt_file_is_skipped_by_default(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    findings = static_checks.find_deprecated_now_calls(dest)
    assert not any(f.file == "jobsched/reports/legacy_export.py" for f in findings)


def test_reference_migration_leaves_zero_findings(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    ref_dir = HIDDEN_REAL_DIR / "reference" / "r5"
    for p in ref_dir.rglob("*.py"):
        rel = p.relative_to(ref_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    assert static_checks.find_deprecated_now_calls(dest) == []


def test_handles_aliased_import_and_lambda_and_comprehension_sites(tmp_path):
    dest = tmp_path / "repo"
    fixture.build_repo_for_task(dest, "R5")
    findings = static_checks.find_deprecated_now_calls(dest)
    files = {f.file for f in findings}
    assert "jobsched/handlers.py" in files  # aliased import (`now as clock`)
    assert "jobsched/importer.py" in files  # lambda default_factory
    assert "jobsched/service.py" in files  # list comprehension
