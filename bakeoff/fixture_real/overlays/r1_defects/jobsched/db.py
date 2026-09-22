"""sqlite3 connection helper and migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobsched.utils.time import utcnow

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrations_dir() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def apply_migrations(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply every `NNNN_*.sql` file in `directory` not yet recorded as applied.

    Returns the list of newly applied version numbers, in order.
    """
    directory = directory or migrations_dir()
    conn.execute(_MIGRATIONS_TABLE)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    newly_applied: list[int] = []
    for path in sorted(directory.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utcnow().isoformat()),
        )
        newly_applied.append(version)
    conn.commit()
    return newly_applied


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Names of all user tables (excludes sqlite's own internal tables) --
    handy for one-off ops debugging sessions.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
