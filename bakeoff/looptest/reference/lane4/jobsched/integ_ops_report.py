"""Operations report: queue health, stuck reservations, and the first jobs page.

Lane 4 integration feature; it only combines the public functions of the lane
1-3 modules (sched_health, sched_stuck, iface_paging) and owns no logic of
its own. Read-only: nothing here writes to the connection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from jobsched.errors import ValidationError
from jobsched.iface_paging import handle_list_jobs_page
from jobsched.sched_health import format_report, queue_report
from jobsched.sched_stuck import find_stuck

STUCK_MAX_AGE_S = 1800
_HEADER = "jobsched ops report"


def ops_report(conn: sqlite3.Connection, *, now: datetime, page_size: int = 5) -> dict:
    """Combine L2-F4 health, L2-F3 stuck ids, and the L3-F3 first page."""
    health = queue_report(conn, now=now)
    stuck_ids = [job.id for job in find_stuck(conn, now=now, max_age_s=STUCK_MAX_AGE_S)]
    page = handle_list_jobs_page(conn, {"offset": 0, "limit": page_size})
    if not page["ok"]:
        # The handler never raises; translate its failure dict.
        raise ValidationError(page["error"])
    return {
        "health": health,
        "stuck_ids": stuck_ids,
        "jobs": page["items"],
        "next_offset": page["next_offset"],
    }


def render_ops_report(report: dict) -> str:
    """Fixed-order text rendering; the health block is L2-F4's own renderer."""
    lines = [_HEADER, format_report(report["health"])]
    ids = report["stuck_ids"]
    lines.append(
        "stuck reservations: none"
        if not ids
        else f"stuck reservations: {len(ids)} ids: {', '.join(str(i) for i in ids)}"
    )
    if report["jobs"]:
        lines += [f"job {item['job_id']} {item['status']} {item['name']}" for item in report["jobs"]]
    else:
        lines.append("first page: none")
    offset = report["next_offset"]
    lines.append("next page offset: none" if offset is None else f"next page offset: {offset}")
    return "\n".join(lines)
