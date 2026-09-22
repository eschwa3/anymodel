"""Bulk customer import from CSV."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from jobsched import service
from jobsched.errors import JobSchedError
from jobsched.utils.time import utcnow

REQUIRED_COLUMNS = ("name", "email", "plan_id")


@dataclass
class ImportError_:
    line_number: int
    message: str


@dataclass
class ImportResult:
    imported: int = 0
    errors: list[ImportError_] = field(default_factory=list)
    started_at: object = field(default_factory=lambda: utcnow())

    @property
    def ok(self) -> bool:
        return not self.errors


def import_customers(conn: sqlite3.Connection, source: str | Path | StringIO) -> ImportResult:
    if isinstance(source, (str, Path)) and not isinstance(source, StringIO):
        text = Path(source).read_text()
    else:
        text = source.read()

    result = ImportResult()
    if not text.strip():
        return result

    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None or any(col not in reader.fieldnames for col in REQUIRED_COLUMNS):
        raise ValueError(f"CSV must have columns: {', '.join(REQUIRED_COLUMNS)}")

    for line_number, row in enumerate(reader, start=2):
        try:
            plan_id = int(row["plan_id"])
        except (TypeError, ValueError):
            result.errors.append(ImportError_(line_number, f"invalid plan_id: {row.get('plan_id')!r}"))
            continue
        try:
            service.create_customer(conn, row.get("name", ""), row.get("email", ""), plan_id)
            result.imported += 1
        except JobSchedError as exc:
            result.errors.append(ImportError_(line_number, str(exc)))

    return result
