"""Next jobs to run: due retries minus stuck reservations, ranked by aging.

Integration module; owns no storage and modifies no existing module. It
composes L2-F1 (`sched_backoff.eligible_jobs`), L2-F3 (`sched_stuck.find_stuck`)
and L2-F2 (`sched_aging.effective_priority`) instead of re-implementing them,
and is strictly read-only: a stuck job is dropped from the plan, never released.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from jobsched.errors import ValidationError
from jobsched.sched_aging import effective_priority
from jobsched.sched_backoff import eligible_jobs
from jobsched.sched_stuck import find_stuck

DEFAULT_STUCK_MAX_AGE_S = 300


def next_jobs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
    base_s: int,
    cap_s: int,
    step_s: int,
    boost: int,
    max_boost: int,
    stuck_max_age_s: int = DEFAULT_STUCK_MAX_AGE_S,
) -> list[dict[str, int]]:
    """The jobs to run next at `now`, best first, at most `limit` of them.

    Candidates are the due retries L2-F1 reports; any id that L2-F3 reports
    as a stuck reservation is dropped (without releasing it). The rest are
    ranked by L2-F2's aged priority, highest first, ties by ascending id.
    Each entry is `{"id": ..., "priority": <base>, "aged_priority": ...}`.
    """
    if limit < 1:
        raise ValidationError("limit must be >= 1")
    stuck_ids = {job.id for job in find_stuck(conn, now=now, max_age_s=stuck_max_age_s)}
    ranked = sorted(
        (
            (job, effective_priority(job, now=now, step_s=step_s, boost=boost, max_boost=max_boost))
            for job in eligible_jobs(conn, now=now, base_s=base_s, cap_s=cap_s)
            if job.id not in stuck_ids
        ),
        key=lambda pair: (-pair[1], pair[0].id),
    )
    return [
        {"id": job.id, "priority": job.priority, "aged_priority": aged}
        for job, aged in ranked[:limit]
    ]
