# jobsched -- agent conventions

`jobsched` is a small job-scheduling and billing backend: customers on
subscription plans submit jobs to a queue, workers reserve and complete
(or fail) them, and invoices are generated per billing period. Storage
is sqlite3. Stdlib only -- do not add third-party dependencies.

- Run tests from the repo root: `python -m pytest tests -q`.
- Money is represented as **integer cents**, never float dollars.
  Round with `jobsched.utils.money.round_half_up` once, at the point a
  value is finalized for storage or display -- not on every
  intermediate step. Summing several already-rounded intermediate
  amounts and treating that sum as the final total is exactly the kind
  of intermediate rounding this rule exists to prevent.
- All SQL goes through `jobsched.repository`. Every query is
  parameterized (`?` placeholders); never format a value into a SQL
  string, including for `LIKE` patterns.
- Raise the exceptions in `jobsched.errors` (`NotFoundError`,
  `ValidationError`, `ConflictError`) instead of bare
  `ValueError`/`KeyError` for anything a caller might want to catch.
- Style: `dataclasses`, type hints, `from __future__ import
  annotations`, absolute imports (`from jobsched.x import y`, never
  relative imports) so tooling can always tell where a name is bound
  from.
- Tests live in `tests/`, one file per module (`test_<module>.py`).
  Keep edits scoped to what you were asked to do; don't reformat or
  rename things in passing.
- Billing periods are half-open: `[period_start, period_end)`. A plan
  change's `effective_at` is the first day the *new* plan applies.
- Job retry policy: `jobsched.scheduler.fail_job` retries a failed job
  until its retry count reaches `AppConfig.max_job_retries`, at which
  point the job is marked dead rather than retried again.
- Timestamps: use `jobsched.utils.time.utcnow()` (timezone-aware) for
  anything new. `jobsched.utils.time.now()` (naive local time) predates
  that convention and is being phased out -- new code must not call it.
  The one exception is `jobsched.reports.legacy_export`, whose
  `generated_at_local` column is intentionally naive local time because
  a downstream report consumer parses it that way; do not change that
  one call site to `utcnow()`.
- See `docs/ONCALL.md` for the retry/dead-letter policy from an
  operational angle.
