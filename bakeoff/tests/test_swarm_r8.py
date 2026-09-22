"""Verifies the R8 (swarm-feature) path end-to-end in --dry-run: real
JobManager dispatch/wait, worktree isolation, branch merge, and hidden
integration test scoring, all with a stub run_worker (no network).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from real import swarm

from anymodel_subagents.tools import sandbox as sandbox_mod

PROMPT_DIR = swarm.PROMPT_DIR
R8_PATCH_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "r8_patches"


def test_r8_dry_run_end_to_end(tmp_path):
    async def _run():
        return await swarm.run_r8("fake-model", tmp_path, max_turns=10, timeout=60, dry_run=True)

    result = asyncio.run(_run())

    assert result["overall"] == 1.0
    assert result["mode"] == "edit"
    assert "bash_warning" not in result
    assert result["merge_cleanliness"] == 1.0
    assert result["existing_tests_pass"] is True
    assert result["hidden_integration_tests_pass"] is True
    assert len(result["branch_outcomes"]) == 3
    assert all(b["merge_status"] == "merged" for b in result["branch_outcomes"])
    assert all(b["ownership_ok"] for b in result["branch_outcomes"])


def test_r8_worker_specs_mode_follows_bash_flag():
    """Dispatch mode is "edit" by default and "edit+bash" with --bash; both
    keep worktree isolation (edit+bash rejects isolation "none", and the
    merge step needs the branches anyway).
    """
    bash_specs = swarm._worker_specs("fake-model", "/tmp/unused", 10, "edit+bash")
    assert [s.mode for s in bash_specs] == ["edit+bash"] * 3
    assert all(s.isolation == "worktree" for s in bash_specs)
    assert [s.mode for s in swarm._worker_specs("fake-model", "/tmp/unused", 10, "edit")] == [
        "edit"
    ] * 3


def test_r8_dry_run_with_bash_dispatches_edit_bash(tmp_path, monkeypatch):
    """--bash must reach the swarm workers: with a sandbox available the
    three TaskSpecs dispatch as edit+bash (the stub run_worker ignores the
    mode, but JobManager validates it at dispatch) and the run still scores
    a clean 1.0.
    """
    monkeypatch.setattr(sandbox_mod, "detect", lambda: "seatbelt")

    async def _run():
        return await swarm.run_r8(
            "fake-model", tmp_path, max_turns=10, timeout=60, dry_run=True, bash=True
        )

    result = asyncio.run(_run())

    assert result["mode"] == "edit+bash"
    assert "bash_warning" not in result
    assert result["overall"] == 1.0


def test_r8_dry_run_with_bash_falls_back_to_edit_without_sandbox(tmp_path, monkeypatch):
    """With no OS sandbox, dispatch rejects edit+bash; the swarm must fall
    back to plain edit for all three workers and record the warning instead
    of failing the whole run (--bash is always safe to pass).
    """
    monkeypatch.setattr(sandbox_mod, "detect", lambda: None)

    async def _run():
        return await swarm.run_r8(
            "fake-model", tmp_path, max_turns=10, timeout=60, dry_run=True, bash=True
        )

    result = asyncio.run(_run())

    assert result["mode"] == "edit"
    assert "edit+bash" in result["bash_warning"]
    assert result["overall"] == 1.0


def test_r8_ownership_violation_is_detected():
    from real.swarm import _ownership_ok

    assert _ownership_ok(["jobsched/models.py"], ("jobsched/models.py",)) is True
    assert _ownership_ok(["jobsched/service.py"], ("jobsched/models.py",)) is False


def test_r8_ownership_allows_own_added_tests():
    """The shared codegen role prompt tells every worker to add/update
    tests for the behavior it changed -- a worker doing exactly that must
    not be flagged as an ownership violation just because tests/ isn't one
    of its `owned_prefixes` (regression test: every real R8 run previously
    had `ownership_ok: False` on every branch for exactly this reason).
    """
    from real.swarm import _ownership_ok

    assert (
        _ownership_ok(
            ["jobsched/repository.py", "tests/test_repository.py"],
            ("jobsched/repository.py",),
        )
        is True
    )
    # Still catches a real violation even when tests/ is also touched.
    assert (
        _ownership_ok(
            ["jobsched/service.py", "tests/test_repository.py"],
            ("jobsched/repository.py",),
        )
        is False
    )


def test_r8_dry_run_with_worker_added_tests_not_penalized(tmp_path, monkeypatch):
    """End-to-end: if one worker's branch also adds its own test file (as
    the codegen role prompt asks for), the swarm must still score a clean
    1.0 -- ownership_ok, merge, and the hidden integration test are all
    unaffected by an in-lane test addition.
    """
    original_copy = swarm._copy_reference_into

    def _copy_with_extra_test(reference_dir, workspace_root):
        changed = original_copy(reference_dir, workspace_root)
        if reference_dir == swarm.WORKERS[0]["reference_dir"]:
            test_dir = workspace_root / "tests"
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / "test_job_tags_worker_a.py").write_text(
                "def test_placeholder():\n    assert True\n"
            )
            changed.append("tests/test_job_tags_worker_a.py")
        return changed

    monkeypatch.setattr(swarm, "_copy_reference_into", _copy_with_extra_test)

    async def _run():
        return await swarm.run_r8("fake-model", tmp_path, max_turns=10, timeout=60, dry_run=True)

    result = asyncio.run(_run())

    assert result["overall"] == 1.0
    assert all(b["ownership_ok"] for b in result["branch_outcomes"])
    assert all(b["merge_status"] == "merged" for b in result["branch_outcomes"])
    assert result["hidden_integration_tests_pass"] is True


def test_r8_rescore_from_reference_patches_scores_high():
    """`--rescore` (see real/rescore.py) reconstructs an R8 run entirely
    from the saved per-worker patches, with no model call. Using the three
    reference patches (fixtures/r8_patches/, generated from
    hidden_real/reference/r8/) should reproduce the same ~1.0 the live
    dry-run reference path gets.
    """
    patch_paths = {
        "r8-worker-a": R8_PATCH_FIXTURES / "r8-worker-a.patch",
        "r8-worker-b": R8_PATCH_FIXTURES / "r8-worker-b.patch",
        "r8-worker-c": R8_PATCH_FIXTURES / "r8-worker-c.patch",
    }
    result = swarm.rescore_r8_from_patches(patch_paths, timeout=60)

    assert result["overall"] == 1.0
    assert result["merge_cleanliness"] == 1.0
    assert result["existing_tests_pass"] is True
    assert result["hidden_integration_tests_pass"] is True
    assert all(b["merge_status"] == "merged" for b in result["branch_outcomes"])
    assert all(b["ownership_ok"] for b in result["branch_outcomes"])


def test_r8_rescore_missing_patch_scores_as_no_changes():
    """A worker with no saved patch (e.g. an older run, or a worker that
    made no changes) is scored as having contributed nothing -- not a
    crash -- so the swarm's merge cleanliness and hidden test correctly
    reflect the incomplete feature.
    """
    patch_paths = {
        "r8-worker-a": R8_PATCH_FIXTURES / "r8-worker-a.patch",
        "r8-worker-b": R8_PATCH_FIXTURES / "r8-worker-b.patch",
        # worker-c missing entirely.
    }
    result = swarm.rescore_r8_from_patches(patch_paths, timeout=60)

    outcomes = {b["label"]: b for b in result["branch_outcomes"]}
    assert outcomes["r8-worker-c-cli-handlers"]["merge_status"] == "no_changes"
    assert result["hidden_integration_tests_pass"] is False
    assert result["overall"] < 1.0


def test_r8_worker_prompts_specify_jobs_by_tag_output_format():
    """Regression test for the harness defect behind R8's near-universal
    hidden-test failure: the hidden integration test asserts a specific
    `jobs-by-tag` CLI output format ("N job(s) tagged ...") that used to be
    unspecified in worker_c's prompt (worker_c only owns the CLI, but every
    worker prompt carries the same shared contract, matching how a real
    orchestrator would decompose the task) -- so a worker implementing the
    CLI honestly per its own prompt had no way to know the exact wording
    the test wanted. Fixed in the prompts, not the hidden test.
    """
    for name in ("worker_a_persistence.md", "worker_b_service.md", "worker_c_cli_handlers.md"):
        text = (PROMPT_DIR / name).read_text()
        assert "job(s) tagged" in text, f"{name} is missing the shared output-format contract"


def test_r8_missing_worker_patch_fails_import_or_integration(tmp_path, monkeypatch):
    """If a worker contributes nothing (task-id doesn't match any reference
    patch), the merge should still succeed cleanly (no conflicting changes)
    but the hidden integration test must fail, since the feature is
    incomplete.
    """
    import re as _re

    monkeypatch.setattr(swarm, "_TASK_ID_RE", _re.compile(r"NEVER_MATCHES_ANYTHING"))

    async def _run():
        return await swarm.run_r8("fake-model", tmp_path, max_turns=10, timeout=60, dry_run=True)

    result = asyncio.run(_run())
    assert result["hidden_integration_tests_pass"] is False
    assert result["overall"] < 1.0
