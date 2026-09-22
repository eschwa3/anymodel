## L4-F4 Configured import

Purpose: one operator entry point that wires an operator-supplied environment
into the app before anything is written: resolve the effective `AppConfig`
from `JOBSCHED_*` variables (L3-F4), validate a customers CSV without writing
(L3-F1), and only when the CSV is clean import it with the existing
`jobsched.importer`, finishing with a read-only view of the jobs table
(L3-F2 — the listing that module actually offers).

Module: `jobsched/integ_bootstrap.py`, exactly this public function:

```python
def bootstrap_import(
    conn: sqlite3.Connection,
    source: str | Path,
    *,
    environ: Mapping[str, str],
    overrides: dict | None = None,
) -> dict
```

(`Mapping` is from `collections.abc`.) Must call — never re-implement:
`jobsched.iface_envconfig.load_config_from_env(environ, overrides)`,
`jobsched.iface_dryrun.dry_run_import(conn, source)`,
`jobsched.importer.import_customers(conn, source)`, and
`jobsched.iface_joblist.list_jobs(conn)` with every filter at its default.
`source` is a path to a UTF-8 customers CSV (header `name,email,plan_id`, the
format of `jobsched.importer`) and is passed unchanged to both L3-F1 and the
importer. `environ` is the operator's environment as a mapping (possibly
empty); this module never reads or mutates `os.environ`. `conn` is used for
every database access; the effective `db_path` is reported but never opened.

Return dict with exactly these keys:

- `"config"`: `dict` — the effective `AppConfig` under the L3-F4 precedence
  (defaults < `JOBSCHED_*` variables < `overrides`), as plain values with
  keys `currency` (str), `tax_rate_bp` (int), `max_job_retries` (int),
  `reservation_lease_seconds` (int), `db_path` (str).
  Defaults are those of `jobsched.config.AppConfig` (so `currency` is `"USD"`
  when neither the environment nor `overrides` sets it).
- `"dry_run"`: `dict` — the L3-F1 report unchanged: `ok` (bool),
  `row_count` (int), `valid_rows` (list of dicts), `errors` (list of dicts).
- `"imported"`: `int` — the number of customers `jobsched.importer` actually
  created (its `ImportResult.imported`) when the dry run was ok; `0` when the
  dry run failed (the importer is then never called).
- `"jobs"`: `list[dict]` — `list_jobs(conn)` (status `None`, customer
  `None`, limit `50`), each `Job` mapped to a plain dict with keys `id`
  (int), `customer_id` (int), `name` (str), `status` (str, the
  `JobStatus.value`), `priority` (int), `retry_count` (int), in L3-F2's
  order (`priority DESC, id ASC`).

Behaviour rules:
1. Order of operations is exactly: (a) load the config from `environ` /
   `overrides`; (b) dry-run-validate `source`; (c) if and only if
   `"dry_run"["ok"]`, import `source` with the importer; (d) list jobs. The
   config step runs first, so a bad environment value fails before the CSV
   is read and before any row is imported.
2. Environment validation is inherited, never repeated: any
   `jobsched.errors.ValidationError` raised by L3-F4 propagates unchanged —
   the message names the offending `JOBSCHED_*` variable (e.g.
   `JOBSCHED_MAX_JOB_RETRIES must be an integer, got 'abc'`), unknown
   `JOBSCHED_*` variables are ignored, and an empty or absent `overrides`
   dict means "no extra overrides".
3. A failed dry run must not raise and must leave the database unchanged:
   the per-row problems stay in `"dry_run"["errors"]`, `"imported"` is `0`,
   no customer row exists afterwards, and `"jobs"` is still produced.
4. An empty or whitespace-only file is not a header error: as in L3-F1 rule 3 it
   yields an ok dry run with `row_count` 0, and `"imported"` is `0`.
   Header errors are the exception to "must not raise": a CSV whose header
   is missing `name`, `email` or `plan_id` raises the
   `jobsched.errors.ValidationError` of L3-F1 (message names the missing
   column(s)); nothing is written and no dict is returned.
5. Per-row validation messages and line numbers are L3-F1's, first failing
   check wins: `invalid plan_id: <repr>`, `customer name is required`,
   `invalid email: <repr>`, `plan <id> not found`; the header is line 1, the
   first data row is line 2, and both `valid_rows` and `errors` ascend.
6. A dry-run-ok CSV creates one customer per data row (L3-F1 accepts exactly
   what `service.create_customer` accepts), so on a clean import
   `"imported"` equals `"dry_run"["row_count"]`; an ok dry run with 0 rows
   imports nothing (`"imported"` is `0`).
7. `"jobs"` and `"dry_run"` are read-only: `bootstrap_import` writes only
   through `jobsched.importer`, and only when the dry run was ok.

Worked example — db state: plan 1 exists; customer 1 is `Acme`; jobs
`(id 1, name "sync", priority 0)` and `(id 2, name "render", priority 5)`,
both pending for customer 1. `customers.csv` holds the single data row
`Acme,ops@acme.test,1` (line 2).

```python
result = bootstrap_import(
    conn,
    "customers.csv",
    environ={"JOBSCHED_CURRENCY": "EUR", "JOBSCHED_MAX_JOB_RETRIES": "7"},
    overrides={"currency": "GBP"},
)
assert result == {
    "config": {
        "currency": "GBP",  # overrides beat the JOBSCHED_CURRENCY env var
        "tax_rate_bp": 750,  # default: variable not set
        "max_job_retries": 7,  # from JOBSCHED_MAX_JOB_RETRIES
        "reservation_lease_seconds": 300,  # default
        "db_path": "jobsched.db",  # default
    },
    "dry_run": {
        "ok": True,
        "row_count": 1,
        "valid_rows": [{"line": 2, "name": "Acme", "email": "ops@acme.test", "plan_id": 1}],
        "errors": [],
    },
    "imported": 1,  # the importer created customer 2 (Acme, ops@acme.test)
    "jobs": [  # list_jobs defaults: no status filter, no customer filter, limit 50
        {
            "id": 2,
            "customer_id": 1,
            "name": "render",
            "status": "pending",
            "priority": 5,
            "retry_count": 0,
        },
        {
            "id": 1,
            "customer_id": 1,
            "name": "sync",
            "status": "pending",
            "priority": 0,
            "retry_count": 0,
        },
    ],
}
```

Existing behaviour and all existing tests must keep passing.
