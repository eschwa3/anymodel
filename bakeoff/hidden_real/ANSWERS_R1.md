# R1 (review-diff) answer key -- not shown to workers

The reviewed commit is `bakeoff/fixture_real/overlays/r1_defects/` applied
on top of the base app (`bakeoff/fixture_real/history/0003_handlers_cli`).
It mixes benign refactors (extracting `_ACTIVE_STATUSES`, adding
`PlanRepository.exists`, wiring a new `bulk-complete` CLI command) with
three planted, behavior-affecting defects.

## Bug 1 -- logic regression: off-by-one in the retry limit (edge case)

- **File:** `jobsched/scheduler.py`
- **Line:** ~49, `fail_job`: `if next_retry_count > cfg.max_job_retries:`
- **Was:** `if next_retry_count >= cfg.max_job_retries:` (base/0002).
- **Description:** With the default `max_job_retries = 3`, a job now
  survives a *fourth* failed attempt (3 -> not `> 3` -> retried again)
  before being marked dead, instead of being dead-lettered after its
  third. `AGENTS.md` and `docs/ONCALL.md` both describe the limit as the
  point at which the job stops being retried; the changed comparison
  contradicts that spec.
- **Keywords:** "off-by-one", "off by one", "retry limit", "max_job_retries",
  "> cfg.max_job_retries", ">= cfg.max_job_retries", "one extra retry",
  "fourth attempt", "never reaches dead", "dead-letter".

## Bug 2 -- resource/transaction handling: uncommitted bulk update

- **File:** `jobsched/repository.py` (`JobRepository.mark_many_done`,
  ~lines 166-180) and its caller `jobsched/scheduler.py`
  (`complete_many_jobs`, ~lines 24-32), reached via the new
  `bulk-complete` CLI subcommand in `jobsched/cli.py`.
- **Description:** Every other write in `JobRepository` calls
  `self.conn.commit()` itself. `mark_many_done` does not, with a comment
  claiming "commit handled by the caller" -- but `complete_many_jobs`
  never commits either, and `cli.main`'s `finally: conn.close()` closes
  the connection unconditionally right after the command runs. sqlite3
  requires an explicit commit; closing a connection with a pending
  transaction discards it. Net effect: `bulk-complete` prints "completed
  N jobs" but the database rows are silently unchanged.
- **Keywords:** "mark_many_done", "commit", "no commit", "never commits",
  "conn.close()", "transaction", "silently lost", "discarded", "rolled back",
  "bulk-complete", "complete_many_jobs".

## Bug 3 -- SQL injection via string-built LIKE pattern

- **File:** `jobsched/repository.py`, `JobRepository.search_by_name`,
  ~lines 202-209.
- **Was:** `"SELECT * FROM jobs WHERE name LIKE ?", (pattern,)` (base/0002)
  -- fully parameterized.
- **Description:** The refactor replaces the parameterized query with an
  f-string (`f"name LIKE '%{fragment}%'"`) interpolated directly into the
  SQL text and executed with no parameters. `fragment` is attacker-
  controlled free text (per its own docstring, from an ops dashboard
  search box), so a value like `x' OR '1'='1` or one containing a `UNION
  SELECT` returns arbitrary rows, and a value containing `'; DROP TABLE
  jobs; --` is a straightforward injection (sqlite3's `execute()` allows
  only one statement, but the `OR`/`UNION` read-side injection alone is
  a real vulnerability). This directly violates AGENTS.md's "every query
  is parameterized... never format a value into a SQL string, including
  for LIKE patterns" rule.
- **Keywords:** "SQL injection", "injection", "f-string", "string
  formatting", "string-built", "parameterized", "search_by_name",
  "LIKE '%", "not parameterized", "unsanitized".

## Scoring notes

- **Recall:** each bug counts once, matched by file name + a nearby line
  number (within ~8 lines) OR any of its keywords appearing in the
  worker's report.
- **False positives:** any additional numbered/bulleted finding beyond
  these three is treated as a false positive and penalized -- the
  refactor-only changes (the `_ACTIVE_STATUSES` extraction,
  `PlanRepository.exists`, the CLI wiring itself) are not bugs, and a
  worker citing one of those as a "bug" should lose precision credit.
