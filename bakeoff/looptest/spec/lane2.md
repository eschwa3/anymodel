# Lane 2 spec — scheduler feature

## L2-F1 Retry backoff schedule

Purpose: a failed job put back in the queue must wait an exponentially
growing delay (doubled per retry, capped) before it can be reserved again;
these functions compute that delay and list due jobs.

New module: `jobsched/sched_backoff.py` (new file; existing files untouched).
It may import existing jobsched code and run its own read-only, parameterized
SQL on the passed connection; it must never read the clock — time is injected.

Public contract (exact signatures):

```python
def backoff_delay_s(retry_count: int, *, base_s: int, cap_s: int) -> int
def next_eligible_at(job: Job, *, base_s: int, cap_s: int) -> datetime
def eligible_jobs(conn: sqlite3.Connection, *, now: datetime, base_s: int, cap_s: int) -> list[Job]
```

- `Job` is `jobsched.models.Job`; `datetime` is `datetime.datetime`.
- A job is **failed-awaiting-retry** iff `status == JobStatus.PENDING`
  (`"pending"`) AND `retry_count >= 1` — exactly the state
  `jobsched.scheduler.fail_job` leaves a retriable job in via
  `JobRepository.mark_failed_retry`, which stamps `jobs.updated_at` with
  `jobsched.utils.time.now().isoformat()` (naive ISO). Fresh pending jobs
  have `retry_count == 0`.

Behaviour rules:
1. `backoff_delay_s` = `min(base_s * 2 ** (retry_count - 1), cap_s)`: retry 1
   waits `base_s`, retry 2 `2*base_s`, retry 3 `4*base_s`, never above
   `cap_s`. `cap_s == base_s` is allowed (then every delay is `base_s`).
2. `next_eligible_at` = `datetime.fromisoformat(job.updated_at)` plus
   `timedelta(seconds=backoff_delay_s(job.retry_count, ...))`. Stored stamps
   are naive, so it returns a naive datetime; `now` must be naive in the same
   clock as `jobs.updated_at`.
3. `eligible_jobs` returns the failed-awaiting-retry jobs with
   `next_eligible_at(job, ...) <= now`. Boundary is inclusive: elapsed
   exactly at `now` IS eligible, one second before is not. Dead, done,
   running, and fresh-pending (`retry_count == 0`) jobs are never returned.
4. `eligible_jobs` orders by `next_eligible_at` ascending, ties by `id`
   ascending; it is read-only and returns `[]` when nothing is due.
5. Validation — every function raises `jobsched.errors.ValidationError` when
   `base_s <= 0` or `cap_s < base_s`, checked before any query (so
   `eligible_jobs` raises even when no job matches); `backoff_delay_s` and
   `next_eligible_at` also raise it when `retry_count < 1`.

Existing behaviour and existing tests must keep passing; this feature only
adds a new module and changes nothing else.
# Lane 2 spec — scheduler features

Each `## L2-FK` section below is a self-contained contract for one small
scheduling feature. Implement exactly the named public API in the named NEW
module; do not modify any existing jobsched file. New modules may import
existing jobsched code and run their own parameterized SQL on the sqlite
connection the caller passes. Follow AGENTS.md: stdlib only, absolute
imports, `jobsched.errors` exceptions, and time always injected, never read.

## L2-F2 Priority aging

Purpose: let pending jobs that have waited in the queue overtake freshly
submitted ones by raising their effective priority with their age, without
changing anything that is stored.

New module: `jobsched/sched_aging.py` (new file; existing files untouched).

Public contract (exact signatures):

```python
def effective_priority(job: Job, *, now: datetime, step_s: int, boost: int, max_boost: int) -> int
def pick_next(conn: sqlite3.Connection, *, now: datetime, step_s: int, boost: int, max_boost: int) -> Job | None
```

- `job` is a `jobsched.models.Job`; `now` is the injected current `datetime`.
- `step_s` is how many seconds of age make one boost step, `boost` is the
  priority added per completed step, `max_boost` caps the total added priority.
- `effective_priority` returns `job.priority + min(max_boost,
  floor(age_seconds / step_s) * boost)` as an `int`, where `age_seconds` is
  measured from `job.created_at` to `now`.
- `pick_next` returns the pending `Job` with the highest effective priority,
  or `None` when there is no pending job.

Behaviour rules:
1. Aging formula: age is clamped at 0 first — a job whose `created_at` is
   after `now` has age 0 and gains nothing; steps are
   `floor(age_seconds / step_s)`, computed exactly on the age in microseconds
   rather than with float division; the added priority is
   `min(max_boost, steps * boost)`, never negative.
2. Timestamps: `job.created_at` (stored ISO text) and `now` are both
   normalized to timezone-aware UTC before subtraction — naive values mean
   UTC, aware values are converted — so mixed naive/aware inputs compare.
3. `pick_next` is read-only: it considers only `pending` jobs, ranks each
   with `effective_priority`'s formula, ties break by oldest `created_at`
   then lowest `id`; it must not reserve, update, or commit anything — the
   job stays `pending`, reservation columns stay NULL, `updated_at` is
   unchanged, and calling it twice returns the same job.
4. Validation: both functions raise `jobsched.errors.ValidationError`
   before doing any other work when `step_s <= 0`, `boost < 0`, or
   `max_boost < 0`.
5. Edge cases: age of exactly one `step_s` adds exactly one `boost`, while
   age one microsecond short of a step adds none; with enough age a
   priority-1 job outranks a priority-3 job submitted later; `pick_next`
   returns `None` on an empty queue and when the only jobs are `running`,
   `done`, `failed`, or `dead`.

Existing behaviour and existing tests must keep passing; this feature only
adds a new module and changes nothing else.# Lane 2 spec — scheduler feature

Implement the feature below in ONE NEW module; do not modify any existing
`jobsched` file. The module may import existing code and run its own
parameterized SQL on the connection the caller passes, against the existing
tables only. Follow `AGENTS.md`: stdlib only, `from __future__ import
annotations`, absolute imports, `jobsched.errors` exceptions for anything a
caller catches. All time is injected as a parameter; never call
`jobsched.utils.time.now()` / `utcnow()` or otherwise read the wall clock.

## L2-F3 Stuck reservation finder

Purpose: surface running jobs whose worker reservation has gone stale
(worker died mid-job) and put them back in the pending queue.
New module: `jobsched/sched_stuck.py` (new file; existing files untouched).

Public contract (exact signatures):

```python
def find_stuck(conn: sqlite3.Connection, *, now: datetime, max_age_s: int) -> list[Job]
def release_stuck(conn: sqlite3.Connection, *, now: datetime, max_age_s: int) -> int
```

- `now` is the injected current time: a naive `datetime` on the same clock the repository writes timestamps with (`jobsched.utils.time.now().isoformat()`).
- A running job's reservation time is its `jobs.updated_at` value: `JobRepository.reserve_next` sets the status to running and stamps `updated_at` in the same UPDATE; do not use `reserved_until` (that is the lease deadline, not the age).
- `find_stuck` returns `jobsched.models.Job` objects for every stuck job, oldest reservation first, ties broken by ascending id.
- `release_stuck` returns each stuck job to pending and returns the number of jobs it released.

Behaviour rules:
1. Stuck = `status` is exactly the running status value (`jobsched.models.JobStatus.RUNNING`, i.e. `"running"`) AND the job has been in that state for strictly more than `max_age_s` seconds at `now` — an age of exactly `max_age_s` is not stuck. Compare ISO-8601 timestamps as strings, the way `PlanChangeRepository.list_for_period` compares `effective_at`: with `cutoff = (now - timedelta(seconds=max_age_s)).isoformat()`, a job is stuck iff `updated_at < cutoff`.
2. Jobs in any other status (pending, done, failed, dead) are never returned by `find_stuck` and never modified by `release_stuck`, however old their `updated_at` is.
3. `release_stuck` runs one parameterized UPDATE setting `status` to `JobStatus.PENDING.value`, clearing the stale reservation (`reserved_by = NULL, reserved_until = NULL`), and bumping `updated_at` to `now.isoformat()`; no other column changes (in particular not `retry_count`). Commit, then return `cursor.rowcount`. (`JobRepository.mark_failed_retry` stamps the wall clock, so it cannot be used here.)
4. Both functions raise `jobsched.errors.ValidationError` when `max_age_s <= 0`, before reading or writing any rows.
5. Edge cases: nothing stuck → `find_stuck` returns `[]` and `release_stuck` returns 0 and changes nothing; a running job reserved exactly `max_age_s` before `now` is neither found nor released, while one reserved a microsecond earlier is both found and released.

Existing behaviour and existing tests must keep passing; this feature only
adds a new module and changes nothing else.
# Lane 2 spec — scheduler/queue features

Each `## L2-FK` section below is a self-contained contract for one small
scheduler feature. Implement exactly the named public API in the named NEW
module; do not modify any existing jobsched file. New modules may import and
call existing code (`jobsched.repository`, `jobsched.models`,
`jobsched.errors`, ...), and may run their own read-only parameterized SQL on
the passed sqlite connection against the existing tables. Follow the repo
conventions in AGENTS.md: stdlib only, `from __future__ import annotations`,
absolute imports, raise `jobsched.errors` exceptions for anything a caller
catches. Time is always injected — never call the clock from inside the
feature, and never sleep in tests.

## L2-F4 Queue health report

Purpose: give operators a snapshot of the job queue — how many jobs sit in
each status, and how long the oldest pending job has been waiting — plus a
fixed-order text rendering of that snapshot.

New module: `jobsched/sched_health.py` (new file; existing files untouched).

Public contract (exact signatures; the report is a plain dict with exactly
the keys `counts`, `total`, `dead_count`, `oldest_pending_age_s`):

```python
def queue_report(conn: sqlite3.Connection, *, now: datetime) -> dict
def format_report(report: dict) -> str
```

- `now` is a timezone-aware `datetime` (in tests, a fixed UTC datetime); the
  module itself never reads the clock.
- `counts` maps every `jobsched.models.JobStatus` value string — `pending`,
  `running`, `done`, `failed`, `dead` — to an int, zeros included.
- `total` is the number of jobs in the `jobs` table (equal to
  `sum(counts.values())`); `dead_count` is the count in the dead-letter
  status `JobStatus.DEAD` (`"dead"`).
- `oldest_pending_age_s` is the age in whole seconds of the oldest pending
  job, from its stored `created_at` to `now`, floored down, never negative;
  `None` when no job is pending.

Behaviour rules:
1. `counts` has an entry for every `JobStatus` value in declaration order,
   `0` when no job holds that status; `dead_count` always equals
   `counts["dead"]`, even when zero.
2. `oldest_pending_age_s`: among jobs with status `pending`, take the one
   with the earliest `created_at` (compared as parsed datetimes, not raw
   text); the age is `now` minus that timestamp, floored down to whole
   seconds and clamped at 0 — a `now` earlier than `created_at` yields 0,
   never negative. Parse `created_at` with `datetime.fromisoformat`; a
   stored value with no timezone info is interpreted as UTC so it compares
   against the aware `now`. No pending job → `None`.
3. Validation — raise `jobsched.errors.ValidationError` when `now` is not
   timezone-aware, or when a pending job's stored `created_at` cannot be
   parsed by `datetime.fromisoformat`.
4. `format_report(report)` returns exactly these lines joined by `"\n"` —
   no trailing newline, no blank lines, each value a plain int: `total:
   <total>`, then one line per status in `JobStatus` declaration order
   (`pending: <n>`, `running: <n>`, `done: <n>`, `failed: <n>`,
   `dead: <n>`), then `oldest pending: <age>` (integer seconds) or
   `oldest pending: none` when `oldest_pending_age_s` is None.
5. `queue_report` is read-only: it never writes to the connection or
   mutates any row. Edge cases: an empty queue reports all-zero counts,
   `total: 0`, `oldest pending: none`; a fractional-second age floors
   (90.9 s → `90`).

Existing behaviour and existing tests must keep passing; this feature only
adds a new module and changes nothing else.
