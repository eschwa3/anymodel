## L4-F2 Next jobs to run

Purpose: one scheduler-facing call answering "which jobs should run
next" — due retries, minus stuck reservations, ranked by aged priority.

New module: `jobsched/integ_next_jobs.py`. It MUST call the lane 1-3
functions `jobsched.sched_backoff.eligible_jobs` (L2-F1),
`jobsched.sched_stuck.find_stuck` (L2-F3) and
`jobsched.sched_aging.effective_priority` (L2-F2); it must not
re-implement backoff, stuck detection, or aging.

Public contract (exact signature):

```python
def next_jobs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
    base_s: int,
    cap_s: int,
    step_s: int,
    boost: int,
    max_boost: int,
    stuck_max_age_s: int = 300,
) -> list[dict[str, int]]
```

- `conn` is the caller's sqlite connection; `now` is the injected time,
  naive, on the same clock the repository writes `updated_at` with (it is
  compared against stored stamps by L2-F1 and L2-F3, and normalized by
  L2-F2). Time is never read inside the module.
- `base_s`/`cap_s` are L2-F1's backoff parameters and `step_s`/`boost`/
  `max_boost` are L2-F2's aging parameters; both are passed through
  unchanged. `stuck_max_age_s` is L2-F3's stuck threshold, default 300 s,
  forwarded as `find_stuck`'s `max_age_s`.

Return shape: a list of at most `limit` dicts, each with EXACTLY the keys
`id` (int, the job id), `priority` (int, the job's stored base priority)
and `aged_priority` (int, L2-F2's `effective_priority` for that job at
`now`). No other keys; a job appears at most once.

Behaviour rules:
1. Candidate set: exactly the jobs `eligible_jobs(conn, now=now,
   base_s=base_s, cap_s=cap_s)` returns — failed-awaiting-retry jobs
   whose backoff has elapsed by `now` (elapsed exactly at `now` counts).
   Fresh pending jobs (`retry_count == 0`) and jobs whose backoff has not
   yet elapsed are never candidates, however high their priority or age.
2. Stuck drop: compute `find_stuck(conn, now=now,
   max_age_s=stuck_max_age_s)` and drop every candidate whose id is in
   that result. Stuck jobs are never released: no `release_stuck` call
   and no UPDATE of any kind — a stuck job must still be `running` with
   its reservation untouched after the call.
3. Ordering: the surviving candidates are ranked by aged priority
   (L2-F2's `effective_priority` with the given `step_s`/`boost`/
   `max_boost` at `now`), highest first, ties by job id ascending. The
   aging formula is inherited from L2-F2: the added priority is capped at
   `max_boost`, earned one `boost` per whole `step_s` of age (floored,
   computed on exact microseconds), and an age ≤ 0 (future `created_at`)
   adds nothing.
4. Truncation: at most `limit` entries are returned, taken from the front
   of the ordered list; with fewer surviving candidates the list is just
   shorter.
5. Validation: `limit < 1` raises `jobsched.errors.ValidationError`,
   checked before any query. Every other parameter is validated by the
   dependency it feeds and that `ValidationError` surfaces unchanged
   from `next_jobs`: `base_s <= 0` or `cap_s < base_s` (L2-F1),
   `step_s <= 0`, `boost < 0` or `max_boost < 0` (L2-F2),
   `stuck_max_age_s <= 0` (L2-F3).
6. Read-only: no INSERT/UPDATE/DELETE and no commit; every row of `jobs`
   is unchanged after a call, and repeated calls with the same arguments
   return the same list.

Worked example. `now = 2024-06-01T12:00:00`, `limit = 10`, `base_s = 60`,
`cap_s = 300`, `step_s = 3600`, `boost = 1`, `max_boost = 5`,
`stuck_max_age_s = 300`; jobs seeded with these stored values:

- J1: pending, priority 1, retry_count 2, created_at
  `2024-05-30T00:00:00`, updated_at `2024-06-01T11:57:00` → delay
  min(60·2, 300) = 120 s, eligible at 11:59:00 ≤ now; age 216000 s →
  60 steps → boost min(5, 60) = 5 → aged priority 6.
- J2: pending, priority 0, retry_count 1, created_at
  `2024-06-01T00:00:00`, updated_at `2024-06-01T11:59:00` → delay 60 s,
  eligible exactly at now (inclusive); age 43200 s → 12 steps → boost
  min(5, 12) = 5 → aged priority 5.
- J3: pending, priority 3, retry_count 3, created_at
  `2024-05-25T00:00:00`, updated_at `2024-06-01T11:59:30` → delay 240 s,
  eligible 12:03:30 — after now, so not a candidate.
- J4: pending, priority 5, retry_count 0, created_at
  `2024-06-01T11:00:00`, updated_at `2024-06-01T11:30:00` → fresh
  pending, so not a candidate.
- J5: running, reserved_by `worker-1`, updated_at
  `2024-06-01T10:00:00` → stuck (reserved more than 300 s ago) → never a
  candidate and never released.

`next_jobs(...)` returns exactly
`[{"id": <J1 id>, "priority": 1, "aged_priority": 6},
  {"id": <J2 id>, "priority": 0, "aged_priority": 5}]`
— J1 first: aged 6 beats 5 despite the lower base priority. With
`limit = 1` only J1's dict is returned. With `limit = 0` →
ValidationError; with `stuck_max_age_s = 0` → ValidationError raised by
`find_stuck`; with `base_s = 0` → ValidationError raised by
`eligible_jobs`.

Existing behaviour and all existing tests must keep passing; this feature
only adds a new module and changes nothing else.
