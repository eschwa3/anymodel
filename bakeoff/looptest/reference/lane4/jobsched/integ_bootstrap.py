"""Configured bootstrap import: env config -> dry run -> import -> listing.

A thin orchestrator over the lane 3 interfaces and the existing importer: the
injected environment decides the effective config, the dry run decides
whether the importer runs at all, and the report ends with a read-only
listing of the jobs table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from jobsched.iface_dryrun import dry_run_import
from jobsched.iface_envconfig import load_config_from_env
from jobsched.iface_joblist import list_jobs
from jobsched.importer import import_customers
from jobsched.models import Job


def _job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "customer_id": job.customer_id,
        "name": job.name,
        "status": job.status.value,
        "priority": job.priority,
        "retry_count": job.retry_count,
    }


def bootstrap_import(
    conn: sqlite3.Connection,
    source: str | Path,
    *,
    environ: Mapping[str, str],
    overrides: dict | None = None,
) -> dict:
    """Config, dry run, (maybe) import, job listing — in exactly that order."""
    cfg = load_config_from_env(environ=environ, overrides=overrides)
    report = dry_run_import(conn, source)
    imported = import_customers(conn, source).imported if report["ok"] else 0
    return {
        "config": asdict(cfg),
        "dry_run": report,
        "imported": imported,
        "jobs": [_job_to_dict(job) for job in list_jobs(conn)],
    }
