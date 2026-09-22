# R2 (review-clean) answer key -- not shown to workers

The reviewed commit is `bakeoff/fixture_real/overlays/r2_clean/` applied on
top of the base app (`bakeoff/fixture_real/history/0003_handlers_cli`): it
adds a read-only `service.get_customer_summary()` rollup, a `customer-summary`
CLI subcommand, and a test for it.

**This commit has no planted defects.** It:

- uses only existing, already-parameterized repository methods (no new SQL);
- doesn't mutate any state (pure read);
- handles the `job_quota == 0` ("unlimited") case in its own CLI print
  (`summary['job_quota'] or 'unlimited'`), consistent with how
  `service.create_job` already treats a zero quota elsewhere;
- is covered by a new test (`tests/test_customer_summary.py`) that exercises
  both the quota field and the active/total job counts;
- doesn't touch billing, scheduling, or anything security-sensitive.

## Scoring

- **1.0**: the worker reports no significant issues (may still mention
  genuinely optional nits -- e.g. "could cache the plan lookup," "the CLI
  print line is a little dense" -- as long as it doesn't call any of them a
  bug or a must-fix).
- **0.5**: the worker raises only minor/stylistic nits without asserting a
  real bug or security issue.
- **0.0**: the worker asserts a fabricated bug (e.g. claims the SQL is
  unparameterized, claims a crash on `job_quota == 0`, claims a missing
  null-check that isn't actually reachable, or otherwise invents a defect
  that isn't present in this diff).
