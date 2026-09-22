"""Offline re-scoring: re-applies the CURRENT scorers to a saved run's
results with no model calls.

    uv run python bakeoff/run.py --rescore bakeoff/runs/<dir>

For each record in `<dir>/results_real.jsonl`:

- R1/R2/R3/R7 (free-text) are always fully re-scorable, straight from the
  saved `final_message` -- no repo needed.
- R4/R5/R6 (edit tasks) are re-scorable only if `<dir>/patches/` has a
  saved `.patch` for that (model, task, repeat): the patch is applied onto
  a freshly built fixture repo and the real scorer (which runs pytest /
  static checks against that repo) runs exactly as it would live.
- R8 (swarm) is re-scorable only if all three worker patches were saved
  for that (model, repeat); see `real.swarm.rescore_r8_from_patches`.

A record that can't be recomputed (older run, no saved patch, or a status
in `report.NON_SCORING_STATUSES`) carries its original score forward
unchanged and is marked `rescored: false`, so the output is always a
complete, consistent results file -- never a partial one silently missing
rows.

Writes `results_real.rescored.jsonl` and `summary_real.rescored.md` next
to the originals; both are gitignored (like everything under
bakeoff/runs/) and this command never touches the original files.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_BAKEOFF_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BAKEOFF_DIR.parent
for _p in (str(_BAKEOFF_DIR), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from real import fixture, report
from real.report import RealRunRecord
from real.scoring import ScoreContext, score_task
from real.swarm import WORKERS, rescore_r8_from_patches
from real.tasks import REAL_TASKS

FREE_TEXT_TASKS = frozenset({"R1", "R2", "R3", "R7"})
PATCH_RESCORABLE_TASKS = frozenset({"R4", "R5", "R6"})

_RECORD_FIELDS = {f.name for f in dataclasses.fields(RealRunRecord)}


def _slug(model: str, task_id: str, repeat: int) -> str:
    return f"{model.replace('/', '_')}__{task_id}__r{repeat}"


def _apply_patch_and_get_changed_files(repo_dir: Path, patch_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git apply failed for {patch_path.name}: {result.stderr.strip()}")
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return [line for line in diff.stdout.splitlines() if line.strip()]


def _rescore_free_text(rec: dict[str, Any], timeout: float) -> dict[str, Any]:
    ctx = ScoreContext(
        repo_dir=Path("."),
        final_message=rec.get("final_message") or "",
        changed_files=rec.get("changed_files") or [],
        timeout=timeout,
    )
    return score_task(REAL_TASKS[rec["task_id"]].scorer, ctx)


def _rescore_from_patch(
    rec: dict[str, Any], patches_dir: Path, timeout: float
) -> tuple[dict[str, Any], list[str]] | None:
    patch_path = patches_dir / f"{_slug(rec['model'], rec['task_id'], rec['repeat'])}.patch"
    if not patch_path.is_file():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="bakeoff-rescore-"))
    try:
        repo_dir = tmp / "repo"
        fixture.build_repo_for_task(repo_dir, rec["task_id"])
        changed_files = _apply_patch_and_get_changed_files(repo_dir, patch_path)
        ctx = ScoreContext(
            repo_dir=repo_dir,
            final_message=rec.get("final_message") or "",
            changed_files=changed_files,
            timeout=timeout,
        )
        score = score_task(REAL_TASKS[rec["task_id"]].scorer, ctx)
        return score, changed_files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rescore_r8(rec: dict[str, Any], patches_dir: Path, timeout: float) -> dict[str, Any] | None:
    slug = f"{rec['model'].replace('/', '_')}__R8__r{rec['repeat']}"
    patch_paths = {}
    for worker in WORKERS:
        candidate = patches_dir / f"{slug}__{worker['key']}.patch"
        if candidate.is_file():
            patch_paths[worker["key"]] = candidate
    if not patch_paths:
        return None
    return rescore_r8_from_patches(patch_paths, timeout)


def rescore_record(rec: dict[str, Any], patches_dir: Path, timeout: float) -> dict[str, Any]:
    """Return a copy of `rec` with its score recomputed from saved
    artifacts where possible, tagged `rescored: bool`.
    """
    out = dict(rec)
    task_id = rec.get("task_id")

    if rec.get("status") in report.NON_SCORING_STATUSES:
        out["rescored"] = False
        return out

    try:
        if task_id in FREE_TEXT_TASKS:
            score = _rescore_free_text(rec, timeout)
            out["score"] = score
            out["overall_score"] = score.get("overall")
            out["rescored"] = True
            return out

        if task_id in PATCH_RESCORABLE_TASKS:
            result = _rescore_from_patch(rec, patches_dir, timeout)
            if result is None:
                out["rescored"] = False
                return out
            score, changed_files = result
            out["score"] = score
            out["overall_score"] = score.get("overall")
            out["changed_files"] = changed_files
            out["rescored"] = True
            return out

        if task_id == "R8":
            score = _rescore_r8(rec, patches_dir, timeout)
            if score is None:
                out["rescored"] = False
                return out
            out["score"] = score
            out["overall_score"] = score.get("overall")
            out["rescored"] = True
            return out
    except Exception as exc:  # noqa: BLE001 - one record's failure must not sink the batch
        out["rescored"] = False
        out["rescore_error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["rescored"] = False
    return out


def _dict_to_real_run_record(rec: dict[str, Any]) -> RealRunRecord:
    kwargs = {k: v for k, v in rec.items() if k in _RECORD_FIELDS}
    kwargs.setdefault("score", {})
    return RealRunRecord(**kwargs)


def run_rescore(run_dir: Path, timeout: float = 120.0) -> int:
    results_path = run_dir / "results_real.jsonl"
    if not results_path.is_file():
        print(f"error: {results_path} not found", file=sys.stderr)
        return 2

    patches_dir = run_dir / "patches"
    records = [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]

    rescored_records = [rescore_record(rec, patches_dir, timeout) for rec in records]

    out_results = run_dir / "results_real.rescored.jsonl"
    with out_results.open("w", encoding="utf-8") as fh:
        for rec in rescored_records:
            fh.write(json.dumps(rec, default=str) + "\n")

    real_records = [_dict_to_real_run_record(rec) for rec in rescored_records]
    summary = report.aggregate(real_records, price_per_mtok=5.0)
    summary_md = report.render_summary_md(summary, price_per_mtok=5.0)

    n_rescored = sum(1 for rec in rescored_records if rec.get("rescored"))
    header = (
        f"<!-- Rescored offline from {results_path.name}: {n_rescored}/{len(rescored_records)} "
        "records recomputed from saved artifacts (final_message / patches); the rest carry "
        "their original score forward (rescored: false). See bakeoff/README.md's --rescore. -->\n\n"
    )
    out_summary = run_dir / "summary_real.rescored.md"
    out_summary.write_text(header + summary_md)

    print(f"Rescored {n_rescored}/{len(rescored_records)} records from saved artifacts.")
    print(f"Wrote {out_results}")
    print(f"Wrote {out_summary}")
    print()
    print(summary_md)
    return 0
