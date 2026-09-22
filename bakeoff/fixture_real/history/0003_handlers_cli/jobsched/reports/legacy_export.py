"""CSV export consumed by a downstream reporting tool that parses its
`generated_at_local` column as naive local time (see AGENTS.md).
"""

from __future__ import annotations

import csv
import sqlite3
from io import StringIO

from jobsched.repository import JobRepository
from jobsched.utils.time import now


def generate_legacy_job_report(conn: sqlite3.Connection, customer_id: int) -> str:
    jobs = JobRepository(conn).list_for_customer(customer_id)
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["job_id", "status", "retry_count", "generated_at_local"])
    generated_at_local = now().isoformat()
    for job in jobs:
        writer.writerow([job.id, job.status.value, job.retry_count, generated_at_local])
    return buf.getvalue()
