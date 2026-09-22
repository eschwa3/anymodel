"""Loop-test helper: build an arm's working repo, or score one.

  uv run python bakeoff/looptest/prepare.py new <dir> [--arm A|B|C]  # fresh repo + SPEC.md + venv; --arm pins the delegation tool
  uv run python bakeoff/looptest/prepare.py score <dir>    # copy hidden tests in, run, print JSON
  uv run python bakeoff/looptest/prepare.py selfcheck      # hidden tests fail bare, pass on reference

The hidden tests and reference never enter an arm's repo before `score`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from real import fixture

HIDDEN = sorted((HERE / "hidden_tests").glob("test_lane*.py"))


def _pytest(repo: Path, *paths: str) -> tuple[int, int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

    def count(word: str) -> int:
        found = re.search(rf"(\d+) {word}", tail)
        return int(found.group(1)) if found else 0

    return count("passed"), count("failed") + count("error"), tail


# The pilot's GOAL.md left the delegation tool open, and two of three arms did not run as
# designed (A's subagents inherited the lead's model; C never called anymodel). An arm line
# pins the tool so the arms compare what they are meant to compare.
_NATIVE = (
    '- Delegation tool for this run: the native Agent tool, with `model: "sonnet"` on every '
    "call. Do not use MCP worker tools.\n"
)
_ANYMODEL = (
    "- Delegation tool for this run: anymodel-subagents workers (load the `delegate` skill, then "
    "`dispatch`/`wait`). Do not use the native Agent tool.\n"
)
ARM_LINES = {"A": _NATIVE, "B": _ANYMODEL, "C": _ANYMODEL}


def new(target: Path, arm: str | None = None) -> None:
    fixture.build_base_repo(target)
    spec = "\n\n".join(p.read_text() for p in sorted((HERE / "spec").glob("lane*.md")))
    (target / "SPEC.md").write_text("# jobsched: features to build\n\n" + spec + "\n")
    goal = (HERE / "goal.md").read_text()
    if arm is not None:
        goal = goal.replace("\nRules:\n", "\nRules:\n" + ARM_LINES[arm], 1)
    (target / "GOAL.md").write_text(goal)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=looptest",
            "-c",
            "user.email=looptest@example.invalid",
            "commit",
            "-q",
            "-m",
            "Add SPEC.md and GOAL.md",
        ],
        cwd=target,
        check=True,
    )
    warning = fixture.ensure_fixture_venv(target)
    print(f"ready: {target}" + (f" (venv warning: {warning})" if warning else ""))


def score(target: Path) -> dict[str, object]:
    work = Path(tempfile.mkdtemp(prefix="looptest-score-"))
    repo = work / "repo"
    shutil.copytree(target, repo, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    visible = _pytest(repo, "tests")
    per_feature = {}
    for test in HIDDEN:
        shutil.copy(test, repo / "tests" / test.name)
        passed, failed, _ = _pytest(repo, f"tests/{test.name}")
        per_feature[test.stem.replace("test_", "")] = {"passed": passed, "failed": failed}
    shutil.rmtree(work, ignore_errors=True)
    complete = sum(1 for v in per_feature.values() if v["passed"] and not v["failed"])
    return {
        "visible_tests": {"passed": visible[0], "failed": visible[1]},
        "features_complete": complete,
        "features_total": len(HIDDEN),
        "hidden_passed": sum(v["passed"] for v in per_feature.values()),
        "per_feature": per_feature,
    }


def selfcheck() -> int:
    work = Path(tempfile.mkdtemp(prefix="looptest-selfcheck-"))
    bare = work / "bare"
    fixture.build_base_repo(bare)
    bare_score = score(bare)
    ref = work / "ref"
    fixture.build_base_repo(ref)
    for lane in sorted((HERE / "reference").iterdir()):
        shutil.copytree(lane, ref, dirs_exist_ok=True)
    ref_score = score(ref)
    shutil.rmtree(work, ignore_errors=True)
    print(f"bare: {bare_score['features_complete']}/{bare_score['features_total']} features")
    print(
        f"reference: {ref_score['features_complete']}/{ref_score['features_total']} features, "
        f"visible failed {ref_score['visible_tests']['failed']}"
    )  # type: ignore[index]
    ok = bare_score["features_complete"] == 0 and ref_score["features_complete"] == len(HIDDEN)
    return 0 if ok else 1


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "new" and len(sys.argv) == 3:
        new(Path(sys.argv[2]).resolve())
    elif command == "new" and len(sys.argv) == 5 and sys.argv[3] == "--arm":
        if sys.argv[4] not in ARM_LINES:
            sys.exit("--arm must be A, B or C")
        new(Path(sys.argv[2]).resolve(), arm=sys.argv[4])
    elif command == "score" and len(sys.argv) == 3:
        print(json.dumps(score(Path(sys.argv[2]).resolve()), indent=1))
    elif command == "selfcheck":
        raise SystemExit(selfcheck())
    else:
        raise SystemExit(__doc__)
