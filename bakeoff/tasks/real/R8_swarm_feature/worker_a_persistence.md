<!-- task-id: r8-worker-a -->
We're adding tags to jobs (free-form labels like "urgent" or "gpu") across
three parallel workstreams; you own persistence. The other two workstreams
(service layer, CLI/handlers) are being built in parallel by different
workers against the contract below -- they will not be visible in your
working tree. Implement exactly this contract so the pieces integrate
cleanly once merged:

1. Add a new migration `jobsched/migrations/0002_add_job_tags.sql` that
   adds a `tags TEXT NOT NULL DEFAULT ''` column to the `jobs` table
   (comma-joined tag list, no spaces around commas).
2. Add a `tags: list[str]` field (default empty list) to the `Job`
   dataclass in `jobsched/models.py`.
3. In `jobsched/repository.py`:
   - `JobRepository.create(...)` gains an optional `tags: list[str] | None
     = None` parameter and persists it (empty string if not given).
   - `_row_to_job` (or equivalent) populates `Job.tags` by splitting the
     stored comma-joined string (empty list if the column is empty or,
     for backward compatibility, absent from the row).
   - Add `JobRepository.add_tag(job_id: int, tag: str) -> Job`: adds `tag`
     to the job's tag list if not already present, persists it, and
     returns the updated `Job`.
   - Add `JobRepository.jobs_with_tag(tag: str) -> list[Job]`: returns
     every job whose tag list contains `tag`.

Don't touch `jobsched/service.py`, `jobsched/cli.py`, or
`jobsched/handlers.py` -- those are owned by other workers. Keep every
query parameterized, per AGENTS.md.

Reply with what you implemented and any assumption you made about the
contract above.

## Shared contract (identical in all three worker prompts)

- `Job.tags: list[str]` (default `[]`), comma-joined in storage.
- `JobRepository.create(customer_id, name, priority=0, tags=None)`;
  `JobRepository.add_tag(job_id, tag) -> Job`;
  `JobRepository.jobs_with_tag(tag) -> list[Job]`.
- `service.create_job(conn, customer_id, name, priority=0, tags=None)`
  (validates each tag against `^[a-z0-9-]{1,30}$`);
  `service.tag_job(conn, job_id, tag) -> Job`;
  `service.list_jobs_by_tag(conn, tag) -> list[Job]`.
- CLI: `create-job` gains a repeatable `--tag`; `jobs-by-tag --tag TAG`
  prints `f"{len(jobs)} job(s) tagged {tag!r}"` followed by one line per
  matching job.
