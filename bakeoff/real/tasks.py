"""Task registry for the `--suite real` bake-off (R1-R8). See bakeoff/README.md."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TASKS_REAL_DIR = Path(__file__).resolve().parent.parent / "tasks" / "real"


@dataclass(frozen=True)
class TaskDef:
    task_id: str
    key: str
    role: str  # one of the bundled roles (reviewer/researcher/codegen/test-writer)
    mode: str  # "read-only" or "edit" -- passed to the CLI regardless of the role's own default
    prompt_file: Path
    scorer: str  # name of the bakeoff.real.scoring function that scores this task
    swarm: bool = False  # True only for R8, handled by real.swarm instead of the CLI


REAL_TASKS: dict[str, TaskDef] = {
    "R1": TaskDef(
        task_id="R1",
        key="review_diff",
        role="reviewer",
        mode="read-only",
        prompt_file=TASKS_REAL_DIR / "R1_review_diff.md",
        scorer="score_r1",
    ),
    "R2": TaskDef(
        task_id="R2",
        key="review_clean",
        role="reviewer",
        mode="read-only",
        prompt_file=TASKS_REAL_DIR / "R2_review_clean.md",
        scorer="score_r2",
    ),
    "R3": TaskDef(
        task_id="R3",
        key="research_trace",
        role="researcher",
        mode="read-only",
        prompt_file=TASKS_REAL_DIR / "R3_research_trace.md",
        scorer="score_r3",
    ),
    "R4": TaskDef(
        task_id="R4",
        key="bugfix_from_symptom",
        role="codegen",
        mode="edit",
        prompt_file=TASKS_REAL_DIR / "R4_bugfix_from_symptom.md",
        scorer="score_r4",
    ),
    "R5": TaskDef(
        task_id="R5",
        key="bulk_migration",
        role="codegen",
        mode="edit",
        prompt_file=TASKS_REAL_DIR / "R5_bulk_migration.md",
        scorer="score_r5",
    ),
    "R6": TaskDef(
        task_id="R6",
        key="write_tests",
        role="test-writer",
        mode="edit",
        prompt_file=TASKS_REAL_DIR / "R6_write_tests.md",
        scorer="score_r6",
    ),
    "R7": TaskDef(
        task_id="R7",
        key="injection_resistance",
        role="researcher",
        mode="read-only",
        prompt_file=TASKS_REAL_DIR / "R7_injection_resistance.md",
        scorer="score_r7",
    ),
    "R8": TaskDef(
        task_id="R8",
        key="swarm_feature",
        role="codegen",
        mode="edit",
        prompt_file=TASKS_REAL_DIR / "R8_swarm_feature",  # a directory; see real.swarm
        scorer="score_r8",
        swarm=True,
    ),
}

# Role membership for the per-role rankings in the summary (spec: reviewer =
# R1+R2, researcher = R3+R7, codegen = R4+R5+R8, test-writer = R6).
ROLE_GROUPS: dict[str, tuple[str, ...]] = {
    "reviewer": ("R1", "R2"),
    "researcher": ("R3", "R7"),
    "codegen": ("R4", "R5", "R8"),
    "test-writer": ("R6",),
}

ALL_TASK_IDS: tuple[str, ...] = tuple(REAL_TASKS)
