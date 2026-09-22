<!-- task-id: r8-worker-b -->
We're adding tags to jobs (free-form labels like "urgent" or "gpu") across
three parallel workstreams; you own the service layer. The other two
workstreams (persistence, CLI/handlers) are being built in parallel by
different workers against the contract below -- they will not be visible
in your working tree, so code against the contract, not against files you
can't see yet.

The persistence workstream is adding (you can assume these exist once
everything is merged, even though they don't exist in your working copy
yet): a `tags: list[str]` field on the `Job` dataclass
(`jobsched/models.py`), a `tags` parameter on
`JobRepository.create(customer_id, name, priority=0, tags=None)`
(`jobsched/repository.py`), `JobRepository.add_tag(job_id, tag) -> Job`,
and `JobRepository.jobs_with_tag(tag) -> list[Job]`.

In `jobsched/service.py`:

1. Extend `create_job(conn, customer_id, name, priority=0)` to accept an
   optional `tags: list[str] | None = None` parameter, validate each tag
   matches `^[a-z0-9-]{1,30}$` (raise `ValidationError` naming the bad tag
   if not), and pass the validated list through to
   `JobRepository.create(...)`.
2. Add `tag_job(conn, job_id: int, tag: str) -> Job`: validates the tag
   with the same rule, then calls `JobRepository.add_tag`.
3. Add `list_jobs_by_tag(conn, tag: str) -> list[Job]`: calls
   `JobRepository.jobs_with_tag`.

Don't touch `jobsched/repository.py`, `jobsched/models.py`,
`jobsched/cli.py`, or `jobsched/handlers.py` -- those are owned by other
workers.

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
