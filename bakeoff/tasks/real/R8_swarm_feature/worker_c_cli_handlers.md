<!-- task-id: r8-worker-c -->
We're adding tags to jobs (free-form labels like "urgent" or "gpu") across
three parallel workstreams; you own the CLI and handler surface. The other
two workstreams (persistence, service layer) are being built in parallel
by different workers against the contract below -- they will not be
visible in your working tree, so code against the contract, not against
files you can't see yet.

You can assume, once everything is merged: `Job.tags: list[str]`
(`jobsched/models.py`), `service.create_job(conn, customer_id, name,
priority=0, tags=None)` (`jobsched/service.py`), `service.tag_job(conn,
job_id, tag) -> Job`, and `service.list_jobs_by_tag(conn, tag) ->
list[Job]`.

1. In `jobsched/cli.py`: add a repeatable `--tag` option to the
   `create-job` subcommand (`action="append"`, dest `tags`), passed
   through to `service.create_job(..., tags=args.tags)`. Add a new
   subcommand `jobs-by-tag --tag TAG` that calls
   `service.list_jobs_by_tag` and prints, in order: a summary line
   `f"{len(jobs)} job(s) tagged {tag!r}"`, then one line per matching job
   with its id, name, and status (exact wording of that per-job line is
   up to you).
2. In `jobsched/handlers.py`: extend `handle_create_job` to read an
   optional `"tags"` key from the payload and pass it through to
   `service.create_job`; include `"tags"` in the returned dict. Also
   include `job.tags` in `handle_get_job`'s returned dict.

Don't touch `jobsched/repository.py`, `jobsched/models.py`, or
`jobsched/service.py` -- those are owned by other workers.

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
