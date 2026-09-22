# LANE 3 — Interfaces features for jobsched

Four small, mutually independent features. Each lives in its own NEW module
`jobsched/iface_<name>.py`. Do not edit any existing file of the app; add
only these new modules (plus their test files). A module may import and call
the existing `jobsched.service`, `jobsched.repository`, `jobsched.importer`,
`jobsched.config` code and may run its own read-only, parameterized SQL
(`?` placeholders only) through the `sqlite3.Connection` it is given.
Follow the repo AGENTS.md conventions: `from __future__ import annotations`,
absolute imports (`from jobsched.x import y`), no third-party dependencies,
and raise the exceptions from `jobsched.errors` (`NotFoundError`,
`ValidationError`, `ConflictError`) rather than bare `ValueError`/`KeyError`.

Existing behaviour and all existing tests must keep passing.

## L3-F1 Importer dry-run validation report

Purpose: validate a customers CSV (same format `jobsched.importer` accepts)
and report every problem together with its source line number, without
writing anything to the database.

Module: `jobsched/iface_dryrun.py`, exactly this public function:

```python
def dry_run_import(conn: sqlite3.Connection, source: str | Path) -> dict
```

`source` is a path to a UTF-8 text CSV file whose header row names the
required columns `name,email,plan_id` (same required columns as
`jobsched.importer`). Return dict with exactly these keys:

- `"ok"`: `bool` — `True` iff `"errors"` is empty.
- `"row_count"`: `int` — number of data rows read (header row excluded).
- `"valid_rows"`: `list[dict]`, each `{"line": int, "name": str, "email": str, "plan_id": int}` where `name` and `email` are the stripped cell values and `plan_id` is an `int`.
- `"errors"`: `list[dict]`, each `{"line": int, "message": str}`.

Rules:
1. Rows are numbered exactly like `jobsched.importer`: the header is line 1
   and the first data row is line 2. Both `valid_rows` and `errors` are in
   ascending line order.
2. A row is valid iff `plan_id` parses as an `int`, the stripped `name` is
   non-empty, the stripped `email` contains `@`, and a plan with that id
   exists. The first failing check wins and is the whole error message,
   using exactly one of: `invalid plan_id: <repr of the raw plan_id cell>`,
   `customer name is required`, `invalid email: <repr of the stripped
   email>`, `plan <plan_id> not found`. A failing row contributes exactly
   one entry to `errors` and never appears in `valid_rows`.
3. An empty or whitespace-only file yields
   `{"ok": True, "row_count": 0, "valid_rows": [], "errors": []}`.
4. A header missing any of `name`, `email`, `plan_id` raises
   `jobsched.errors.ValidationError` (message must name the missing
   column(s)); no dict is returned and nothing is written.
5. Dry run only: after a successful or failing call the database must be
   unchanged — no customer rows created, no writes committed.

## L3-F2 Job listing with filters, limit, and a CLI entry point

Purpose: give callers (and an operator) a stable, filtered view of the jobs
table — by status and/or customer, with a limit and deterministic ordering —
plus a small `main` that prints one line per job.

Module: `jobsched/iface_joblist.py`, exactly these public functions:

```python
def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    customer_id: int | None = None,
    limit: int = 50,
) -> list[Job]
```

(`Job` is `jobsched.models.Job`; return repository-style dataclasses, one
per matching row.) And a command entry point:

```python
def main(argv: list[str]) -> int
```

Rules:
1. `status` filters on exact status value, `customer_id` on exact customer;
   both may be given and combine with AND. An unknown `status` (anything
   not in `pending`, `running`, `done`, `failed`, `dead`) raises
   `jobsched.errors.ValidationError` with message `invalid status: <repr>`.
   A `customer_id` that does not exist raises `jobsched.errors.NotFoundError`.
2. Ordering is `priority DESC, id ASC` (the same deterministic tie-break as
   `JobRepository.reserve_next`). `limit` is applied after ordering; the
   default is 50; `limit <= 0` raises `ValidationError` with message
   `limit must be >= 1`.
3. `main` accepts the flags `--db` (default `jobsched.db`), `--status`,
   `--customer-id` (int), `--limit` (int, default 50). It connects to the
   database, applies migrations (like `jobsched.cli.main` does), calls
   `list_jobs`, and prints exactly one line per job to stdout:
   `job {id} {status} {name}` — e.g. `job 3 done rebuild-index`. With no
   matching jobs it prints nothing and returns 0.
4. `main` returns 1 and prints `error: {exc}` to stderr (stdout stays
   empty of job lines) when `list_jobs` raises a `jobsched.errors`
   exception, mirroring `jobsched.cli.main`. An unknown flag exits with
   argparse's usual code 2.
5. Existing behaviour and all existing tests must keep passing.

## L3-F3 Offset/limit pagination handler for jobs

Purpose: a plain-function handler, in the style of `jobsched.handlers`, that
returns one page of jobs plus the offset of the next page, so a client can
walk the whole table.

Module: `jobsched/iface_paging.py`, exactly this public function:

```python
def handle_list_jobs_page(conn: sqlite3.Connection, payload: dict) -> dict
```

`payload` keys (both optional): `"offset"` (default `0`) and `"limit"`
(default `10`). Values are coerced with `int()`. Success response dict with
exactly these keys:

- `"ok"`: `bool`, always `True` on success.
- `"items"`: `list[dict]`, each `{"job_id": int, "name": str, "status": str, "priority": int}` — one page of jobs.
- `"next_offset"`: `int | None` — `offset + len(items)` when at least one more row exists after this page, else `None`.

Failure response (same style as `jobsched.handlers`): exactly
`{"ok": False, "error": str}` with no `"items"` or `"next_offset"` keys.

Rules:
1. Items are the jobs ordered by `id ASC` — job ids `offset..offset+limit-1`
   of that ordering. With an empty jobs table the response is
   `{"ok": True, "items": [], "next_offset": None}`.
2. A page that consumes the remaining rows (exactly, or because `offset` is
   past the end of the table) has `next_offset: None`; only a page followed
   by more rows reports the next offset.
3. Bad payloads return the failure dict, never raise: values that `int()`
   cannot coerce produce error `offset and limit must be integers`;
   `offset < 0` produces `offset must be >= 0`; `limit <= 0` produces
   `limit must be >= 1`. Checks run in that order (coercion, then offset,
   then limit).
4. The handler reads only; it never inserts or updates anything.
5. Existing behaviour and all existing tests must keep passing.

## L3-F4 Configuration overrides from JOBSCHED_* environment variables

Purpose: let an operator override `AppConfig` fields through environment
variables, with validation, without touching `jobsched/config.py` or any
config file.

Module: `jobsched/iface_envconfig.py`, exactly this public function:

```python
def load_config_from_env(
    environ: Mapping[str, str] | None = None,
    overrides: dict | None = None,
) -> AppConfig
```

(`AppConfig` is `jobsched.config.AppConfig`; `Mapping` is from
`collections.abc`.) Recognized variables — name maps to field:

`JOBSCHED_CURRENCY` → `currency`, `JOBSCHED_TAX_RATE_BP` → `tax_rate_bp`,
`JOBSCHED_MAX_JOB_RETRIES` → `max_job_retries`,
`JOBSCHED_RESERVATION_LEASE_SECONDS` → `reservation_lease_seconds`,
`JOBSCHED_DB_PATH` → `db_path`.

Rules:
1. A set variable overrides that one field of the default `AppConfig`;
   unset variables leave the default in place. Any other `JOBSCHED_*`
   variable is ignored. `environ=None` means `os.environ`; the function
   never mutates `os.environ` either way.
2. Validation, raising `jobsched.errors.ValidationError` with a message
   that names the offending variable: an int field whose value `int()`
   cannot parse; `tax_rate_bp` or `max_job_retries` below 0;
   `reservation_lease_seconds` below 1; an empty-string value for
   `currency` or `db_path`. For bad values the function raises only
   `ValidationError`, never `ValueError`/`TypeError`.
3. Precedence is: `AppConfig` defaults < recognized environment variables
   < the `overrides` dict (whose keys are plain `AppConfig` field names and
   which wins over the environment). The result is an `AppConfig`; build it
   with `jobsched.config.load_config` / `dataclasses.replace`, not by
   mutating `DEFAULT_CONFIG`.
4. An empty or absent `overrides` dict means "no extra overrides".
5. Existing behaviour and all existing tests must keep passing.



