# Configuration

anymodel-subagents is configured by a single, user-edited YAML file. Nothing
in the product writes it — there is no config-mutating tool, by design (see
SECURITY.md).

## `config.yaml`

Location precedence: `$ANYMODEL_CONFIG` if set, else
`$XDG_CONFIG_HOME/anymodel-subagents/config.yaml`, else
`~/.config/anymodel-subagents/config.yaml`.

Unknown keys are rejected (the file fails to load with an error naming them)
rather than silently ignored.

The file is read once, at server startup: restart your client after editing
it. Nothing re-reads it mid-session, and nothing in the product can change a
value at runtime.
A `config.yaml` in the wrong directory behaves exactly like no file at all, so
check what a running server actually loaded with `list_workers` -- its `server`
block reports `config_path`, `config_found` and the effective `max_wait_s` (also
logged once at startup). `wait` likewise returns the `timeout_s` it really
applied.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_model` | string | `deepseek/deepseek-v4.1-flash` | Model used when a task doesn't set `model` |
| `max_concurrency` | int (1-32) | `8` | Global cap on simultaneously running worker jobs |
| `max_turns` | int (1-200) | `40` | Default per-job turn cap (a task's `max_turns` can lower it, not raise it past the config's clamp) |
| `timeout_s` | number | `900.0` | Wall-clock timeout per job, in seconds (clamped to 10–86400). A `dispatch` task may pass its own `timeout_s` to shorten this for that task, never to extend it; unrelated to `wait`'s `timeout_s` |
| `max_wait_s` | number (5-600) | `45` | Cap on how long one `wait` long-poll call may block. Leave it at `45` under Codex CLI, which kills an MCP tool call after 60 s; clients without such a limit (Claude Code) can raise it to block until jobs finish in a single call |
| `max_tasks_per_dispatch` | int | `20` | Cap on tasks in a single `dispatch` call; a larger call is rejected, not truncated |
| `max_output_tokens` | int (256-200000) or `null` | `16384` | Per-request `max_tokens` cap sent to OpenRouter; `null` disables the cap |
| `max_live_jobs` | int (1-1000) | `64` | Global cap on queued+running jobs at once; `dispatch` rejects a call that would push the total past it. Finished jobs never count against this. |
| `budget_per_swarm_usd` | number (USD) or `null` | `null` | Spend cap per swarm (`null` = off). See "Budget caps" below |
| `budget_per_day_usd` | number (USD) or `null` | `null` | Spend cap per local day (`null` = off). See "Budget caps" below |
| `allowed_roots` | list of strings | `[]` (any git work tree) | If non-empty, every task's `cwd` must be inside one of these paths |
| `job_retention_days` | int | `7` | Finished jobs older than this (meta, transcript, report) are swept from the state dir on startup; a job whose worktree still exists is kept. `0` disables the sweep |
| `bash_allow` | list of strings | built-in default (see below) | Command-prefix allowlist for the Bash tool. See "Bash settings" below |
| `allow_unsandboxed_bash` | bool | `false` | Lets an explicitly requested `edit+bash` task run without an OS sandbox. See "Bash settings" below |
| `bash_repo_venv` | bool | `true` | For edit+bash jobs, expose the source repository's `.venv` read-only inside the sandbox. See "Bash settings" below |
| `allow_project_roles` | bool | `false` | Whether a project's own `.workers/` role files are honored. See "Roles" below |
| `provider_sort` | string or `null` | `null` | OpenRouter provider sort: one of `throughput`, `latency`, `price`, or `null` (off). Setting it turns off OpenRouter's default price-weighted load balancing among ZDR-eligible providers, so it can pick a pricier one; leave it `null` unless you want that trade-off |

`max_concurrency`, `max_turns`, `max_live_jobs`, `max_output_tokens`, and
`max_wait_s` are clamped into their valid ranges rather than rejected if a
value outside them is supplied; every other key is type-checked and rejected
outright on mismatch. The budget caps are deliberately rejected too, never
clamped: a silently-raised cap could spend past what you configured.

`wait`, `results`, and `cancel` additionally accept at most 200 `job_ids` per
call (not configurable) — a single MCP call can't force an unbounded
scan/response, no matter how large `max_live_jobs` or `job_retention_days`
are set. Finished jobs are also capped in the server's own memory (oldest
finished first); anything past that bound stays fully queryable, just read
back from its `meta.json` on disk instead of an in-memory cache.

### Budget caps

`budget_per_swarm_usd` and `budget_per_day_usd` are caps on OpenRouter
cost in USD: a positive number, or `null` to turn the cap off. `0`, a
negative value, or a non-finite one is rejected at load.

- The per-day total is seeded from `ledger.jsonl` at server start (today's
  local-day entries), so restarting doesn't reset what the day has already
  spent; it rolls over at your local midnight.
- The day cap is checked once per `dispatch`: once it is reached, new
  dispatches are refused outright and nothing is queued.
- The per-swarm cap is checked when each job is about to start (a queued job
  whose siblings spent past the cap is stopped before ever running).
- A running job is stopped between model requests, as soon as a response's
  cost pushes the total past a cap. It ends with status `budget_exceeded` —
  terminal exactly like `max_turns`: its worktree (if any) is kept and
  policy-scanned, its ledger entry is written, and its report is retrievable.

Caps are read from this file only. No MCP tool can raise, lower, or reset
one; the only way to change a cap is to edit `config.yaml` and restart the
server. `list_workers`'s `server` block reports the live snapshot (caps and
today's running spend).

### Bash settings

These three keys configure the Bash tool, which only `edit+bash` mode has.
They are operator policy: a worker can never change them.

- `bash_allow` is a list of command prefixes. A command must start with one
  of them to run at all, and chaining or piping is allowed only between
  allowlisted commands. The default list covers ordinary test/lint/build/
  inspection work (`pytest`, `uv run pytest`, `cargo test`, `make test`,
  `ruff`, `git diff`, `rg`, ...). This is defense in depth only — the OS
  sandbox is the real boundary; an allowlisted command can still write files
  via the workspace, which is why `edit+bash` always runs in a worktree.
- `allow_unsandboxed_bash` (default `false`) is an escape hatch for machines
  with no OS sandbox (macOS Seatbelt or Linux bwrap). Without one, an
  explicitly requested `mode: "edit+bash"` is refused. Setting this to `true`
  lets such a task run its Bash tool unsandboxed: the OS sandbox is removed
  entirely, so the worker's commands run as your user, with network access
  and unrestricted file access — only the `bash_allow` prefix list still
  applies, and only a single simple command is ever executed (no shell
  chaining or piping). It never serves a role's default mode: a role whose
  default is `edit+bash` silently runs in `edit` on a sandbox-less machine
  even with this set, and the dispatch response says so.
- `bash_repo_venv` (default `true`) makes the source repository's `.venv`
  readable (read-only) inside the sandbox and puts its `bin` first on PATH,
  so a worker can run the project's own tests. macOS (Seatbelt) only for
  now. The path is derived server-side from the job's worktree metadata and
  re-validated at call time — never from a worker or orchestrator. Set
  `false` to disable.

## State directory

Precedence: `$ANYMODEL_STATE_DIR` if set, else
`$XDG_STATE_HOME/anymodel-subagents`, else `~/.local/state/anymodel-subagents`.
Created `0700` if missing. The Claude Code plugin sets
`ANYMODEL_STATE_DIR=${CLAUDE_PLUGIN_DATA}` explicitly in `plugin.json` — an
ambient `$CLAUDE_PLUGIN_DATA` is otherwise ignored, since it could belong to a
different plugin.

Layout:

```
<state_dir>/
  jobs/<job_id>/        transcript + metadata for one job
  worktrees/<job_id>/   git worktree for an isolated edit job (removed
                        automatically if the worker leaves no changes)
  ledger.jsonl          append-only usage/cost log, 0600
```

## Roles

A role is a Markdown file with frontmatter, in the same shape as a native
subagent definition:

```markdown
---
name: reviewer
description: Reviews a diff or files for bugs; read-only.
model: deepseek/<id>
mode: read-only
max_turns: 25
---
<system prompt for this role>
```

Roles are loaded from bundled defaults shipped with the package (`reviewer`,
`researcher`, `codegen` — `edit+bash`, worktree isolation — and `test-writer`),
then from the user config dir (alongside `config.yaml`), then — only if
`allow_project_roles: true` — from the task's own repository's `.workers/`
directory, which is off by default because those files come from a possibly-
untrusted repo. A later source overrides an earlier one by name, so a project
role shadows a user or bundled role with the same name. The rendered role
list (name, description, model, mode) is what both `list_workers` and the
`dispatch` tool description show, so the two clients stay in sync
automatically.

A task's `model` field overrides a role's default model for that one task —
useful for one-off swaps or for running the same prompt under several models
as a consensus check.
