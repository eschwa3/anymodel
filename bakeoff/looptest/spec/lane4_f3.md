## L4-F3 Operations report

Purpose: one operator-facing snapshot that combines the queue health of L2-F4,
the ids of stuck reservations from L2-F3, and the first page of jobs from the
pagination handler of L3-F3 — delivered as a data dict and as a fixed-format
plain-text report.

Module: `jobsched/integ_ops_report.py` (new file; no existing or lane 1-3 file
may be edited). It MUST call — not re-implement — these public functions:

- `jobsched.sched_health.queue_report(conn, *, now)` (L2-F4)
- `jobsched.sched_health.format_report(report)` (L2-F4, for the text block)
- `jobsched.sched_stuck.find_stuck(conn, *, now, max_age_s)` (L2-F3)
- `jobsched.iface_paging.handle_list_jobs_page(conn, payload)` (L3-F3)

Public contract (exact signatures):

```python
def ops_report(conn: sqlite3.Connection, *, now: datetime, page_size: int = 5) -> dict
def render_ops_report(report: dict) -> str
```

- `now` is the single injected clock, a timezone-aware `datetime`; the same
  `now` goes to `queue_report` and to `find_stuck`. The module never reads the
  wall clock.
- The stuck threshold passed to `find_stuck` is the module constant
  `STUCK_MAX_AGE_S = 1800` (seconds).
- `page_size` is handed to the L3-F3 handler as
  `{"offset": 0, "limit": page_size}`; the handler's own coercion and
  validation stay in force.

`ops_report` returns a dict with exactly these four keys:

- `"health"`: dict — exactly what L2-F4's `queue_report` returned:
  `{"counts": {str: int}, "total": int, "dead_count": int,
  "oldest_pending_age_s": int | None}`.
- `"stuck_ids"`: `list[int]` — the `.id` of every job `find_stuck` returns,
  in find_stuck's own order (oldest reservation first, ties by ascending id).
- `"jobs"`: `list[dict]` — the handler's `"items"` verbatim: each
  `{"job_id": int, "name": str, "status": str, "priority": int}`, ordered by
  id ascending, at most `page_size` of them.
- `"next_offset"`: `int | None` — the handler's `"next_offset"`
  (`0 + len(items)` when more rows follow the page, else `None`).

Behaviour rules:
1. `ops_report` calls `queue_report` first, then `find_stuck`, then the
   handler, and raises whatever they raise unchanged. A naive (not
   timezone-aware) `now` therefore raises `jobsched.errors.ValidationError`
   (L2-F4's own message, unchanged).
2. Error translation: `handle_list_jobs_page` never raises — on bad input it
   returns `{"ok": False, "error": <message>}`. When the response has
   `ok == False`, `ops_report` raises `jobsched.errors.ValidationError` whose
   message is that error string verbatim; `ops_report` performs no page_size
   check of its own. So `page_size <= 0` raises
   `ValidationError("limit must be >= 1")` and a `page_size` that `int()`
   cannot coerce (e.g. `"abc"`) raises `ValidationError("offset and limit
   must be integers")` — the L3-F3 handler's exact rules and messages.
3. Read-only: `ops_report` never writes any row and never commits; statuses,
   reservation columns and timestamps are exactly as before the call.
4. `render_ops_report(report)` takes the dict `ops_report` returned and gives
   the following lines joined by `"\n"` — no trailing newline, no blank
   lines, in exactly this order:
   a. `jobsched ops report`
   b. the health block: exactly the output of L2-F4's
      `format_report(report["health"])` (its seven lines: `total: <n>`, then
      `pending: <n>`, `running: <n>`, `done: <n>`, `failed: <n>`, `dead: <n>`
      in `JobStatus` declaration order, then `oldest pending: <age>` or
      `oldest pending: none`); render_ops_report must not recompute or
      reformat the health numbers itself.
   c. `stuck reservations: none` when `stuck_ids` is empty, else
      `stuck reservations: <n> ids: <ids joined by ", ">` — e.g.
      `stuck reservations: 2 ids: 1, 2`.
   d. one line per page item, `job <job_id> <status> <name>` (no padding);
      when `jobs` is empty, the single line `first page: none`.
   e. last line: `next page offset: none` when `next_offset` is `None`, else
      `next page offset: <next_offset>`.

Worked example — seeded with plan `starter` (999 cents) and customer `Acme`,
jobs created in this order (ids 1-4) and pinned to the given states, then
`ops_report(conn, now=NOW, page_size=2)` with
`NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)`:

| id | name       | state |
|----|------------|-------|
| 1  | job-0      | pending, `created_at = "2024-06-01T11:58:29.500000"` — age 90.5 s, floored to 90 (L2-F4's rule) |
| 2  | job-1      | done |
| 3  | job-2      | dead |
| 4  | worker-job | running, reserved (`updated_at`) at `"2024-06-01T10:00:00"` — 2 h before NOW, so stuck at 1800 s |

Result dict:

```python
{
    "health": {
        "counts": {"pending": 1, "running": 1, "done": 1, "failed": 0, "dead": 1},
        "total": 4,
        "dead_count": 1,
        "oldest_pending_age_s": 90,
    },
    "stuck_ids": [4],
    "jobs": [
        {"job_id": 1, "name": "job-0", "status": "pending", "priority": 0},
        {"job_id": 2, "name": "job-1", "status": "done", "priority": 0},
    ],
    "next_offset": 2,
}
```

`render_ops_report` of that dict is exactly:

```text
jobsched ops report
total: 4
pending: 1
running: 1
done: 1
failed: 0
dead: 1
oldest pending: 90
stuck reservations: 1 ids: 4
job 1 pending job-0
job 2 done job-1
next page offset: 2
```

Existing behaviour and all existing tests must keep passing; this feature
only adds a new module and changes nothing else.
