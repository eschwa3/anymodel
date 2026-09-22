"""Dry-run validation report for the customers CSV importer.

Read-only companion to `jobsched.importer`: same CSV format, required
columns, and row numbering, but nothing is written to the database.
"""

from __future__ import annotations

import csv
import sqlite3
from io import StringIO
from pathlib import Path

from jobsched.errors import NotFoundError, ValidationError
from jobsched.repository import PlanRepository

REQUIRED_COLUMNS = ("name", "email", "plan_id")


def _row_problem(conn: sqlite3.Connection, row: dict) -> str | None:
    """Return the first validation problem with the row, or None if valid."""
    raw_plan = row.get("plan_id")
    try:
        plan_id = int(raw_plan)
    except (TypeError, ValueError):
        return f"invalid plan_id: {raw_plan!r}"
    name = (row.get("name") or "").strip()
    if not name:
        return "customer name is required"
    email = (row.get("email") or "").strip()
    if "@" not in email:
        return f"invalid email: {email!r}"
    try:
        PlanRepository(conn).get(plan_id)
    except NotFoundError as exc:
        return str(exc)  # "plan <id> not found"
    return None


def dry_run_import(conn: sqlite3.Connection, source: str | Path) -> dict:
    """Validate a customers CSV and report problems per line, writing nothing."""
    text = Path(source).read_text()
    if not text.strip():
        return {"ok": True, "row_count": 0, "valid_rows": [], "errors": []}

    reader = csv.DictReader(StringIO(text))
    fieldnames = reader.fieldnames or ()
    missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
    if missing:
        raise ValidationError(f"CSV is missing required columns: {', '.join(missing)}")

    valid_rows: list[dict] = []
    errors: list[dict] = []
    for line_number, row in enumerate(reader, start=2):
        problem = _row_problem(conn, row)
        if problem is None:
            valid_rows.append(
                {
                    "line": line_number,
                    "name": row["name"].strip(),
                    "email": row["email"].strip(),
                    "plan_id": int(row["plan_id"]),
                }
            )
        else:
            errors.append({"line": line_number, "message": problem})

    return {
        "ok": not errors,
        "row_count": len(valid_rows) + len(errors),
        "valid_rows": valid_rows,
        "errors": errors,
    }
