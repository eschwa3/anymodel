# anymodel-subagents

Subagents for Claude Code and Codex CLI that run on cheap outside models.

Your orchestrator writes the prompt and reads the report. A worker on a
model that costs cents per million tokens does the reading, editing and
testing. **Same results, about a third of the Claude usage, for a few dimes
of OpenRouter spend.**

- Works like a native subagent: fresh context, own tool loop, your repo, only
  the final report comes back.
- Edits land on a branch in an isolated git worktree; nothing touches your
  working tree until you merge.
- Every request routes with Zero Data Retention. Worker Bash runs in an OS
  sandbox with no network.
- One `dispatch` per batch, one `wait`, then `results`. Ledger and spend caps
  built in.

## Install

**Requirements**

- macOS or Linux. On Windows use WSL2 and keep your repos on the Linux
  filesystem (`~/…`, not `/mnt/c`).
- [uv](https://docs.astral.sh/uv/) and `git` on your `PATH`. Python is not
  needed: `uvx` brings its own (3.12+).
- Worker Bash needs an OS sandbox: built in on macOS; on Linux/WSL2
  `sudo apt install bubblewrap`. Ubuntu 24.04+ also needs
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (bwrap uses
  user namespaces). Without a sandbox workers still read and edit, but do not
  run commands.
- An OpenRouter key: make a **dedicated key with a credit limit**
  (openrouter.ai/keys) and switch on **Zero Data Retention**
  (openrouter.ai/settings/privacy) — without it, no request finds a provider.

**Claude Code**

```
/plugin marketplace add eschwa3/anymodel
/plugin install anymodel-subagents@anymodel
```

Paste the key when prompted (it goes to your OS keychain). Done: the
`delegate` skill now steers delegation to workers, and `/swarm <task>` fans a
task out by hand. Details in [`docs/claude-code.md`](docs/claude-code.md).

**Codex CLI** (plain stdio MCP server; no plugin system)

1. Register it:

   ```bash
   codex mcp add anymodel -- uvx --from git+https://github.com/eschwa3/anymodel@v1.0.0 anymodel-subagents
   ```

2. Forward the key by name, not by value. Edit the block this wrote to
   `~/.codex/config.toml` (or put it in the project's `.codex/config.toml`):

   ```toml
   [mcp_servers.anymodel]
   command = "uvx"
   args = ["--from", "git+https://github.com/eschwa3/anymodel@v1.0.0", "anymodel-subagents"]
   env_vars = ["OPENROUTER_API_KEY"]
   startup_timeout_sec = 60   # the first uvx start builds the environment; Codex's default is 10 s
   tool_timeout_sec = 120
   ```

   Then `export OPENROUTER_API_KEY=…` in the shell you launch Codex from;
   the key never touches the file.

3. Paste [`docs/AGENTS-snippet.md`](docs/AGENTS-snippet.md) into your
   project's `AGENTS.md`. Keep its authorization line if it is true for the
   repo: headless `codex exec --approve-for-me` otherwise rejects `dispatch`
   as an export of repository contents.

4. Check with `codex mcp list`; the tools appear as `mcp__anymodel__dispatch`,
   `…wait`, `…results`, `…cancel`, `…list_workers`, `…usage_report`.
   Interactive Codex asks you to approve each MCP call.

Codex kills a tool call after `tool_timeout_sec`, so `max_wait_s` must stay
below it there, while Claude Code wants 600. Use one config file per client:
add `env = { ANYMODEL_CONFIG = "~/.config/anymodel-subagents/config-codex.yaml" }`
to the block above and put `max_wait_s: 100` in that file. Details and verified
behaviour: [`docs/codex.md`](docs/codex.md).

**Try it** (inside a git repository):

> Use anymodel workers: have a reviewer check this branch's diff against main
> and a researcher list every place we read environment variables. Report the
> cost.

## Numbers

An orchestrator builds 16 specified features in a small Python project,
delegating all the coding; hidden tests score the result. Same score in every
run (`bakeoff/looptest/`, 2026-09-20):

| Orchestrator + who codes | Claude usage | OpenRouter | Wall time |
|---|---|---|---|
| Fable 5.1 + native Sonnet 5 subagents | 100 % | — | 6–8 min |
| Fable 5.1 + anymodel workers (glm-5.3-flash) | 35–45 % | ≈ $0.30 | 17 min |
| Sonnet 5 + anymodel workers (glm-5.3-flash) | 35–54 % | ≈ $0.20 | 16–18 min |

Usage is session-log tokens weighted into Sonnet-token units; the range covers
lead-token weights of 3–5x and cache-read weights of 0.1–0.25x. Workers trade
wall time for usage: a batch is as slow as its slowest job. On long, uneven
jobs (2–24 min each) the orchestrator's own cost stayed flat at ≈ $3 per run —
waiting is nearly free.

Delegation has to earn its overhead. The same 16 small features, built by the
orchestrator alone, took 6 minutes and less usage than any delegating run.
Workers pay off on work that is big, read-heavy or mechanical: diff review,
codebase research, test writing, several modules at once.

## Configure

Optional. The file is yours; nothing in the product writes it. Restart the
client after editing.

```yaml
# ~/.config/anymodel-subagents/config.yaml
max_wait_s: 600           # Claude Code: one long wait per batch (leave 45 under Codex)
budget_per_day_usd: 5.00  # spend backstop, off by default
# timeout_s: 1800         # per-job wall clock, default 900; raise for slow test suites
# provider_sort: throughput  # fastest ZDR provider, possibly pricier; default off
```

All keys, roles and the state directory: [`docs/configuration.md`](docs/configuration.md).

## Tools

| Tool | What it does |
|---|---|
| `dispatch` | Start a batch of tasks (`role` or `model`+`mode`, `prompt`, `cwd`; optional `max_turns`, `timeout_s`). Returns job ids at once |
| `wait` | Block until the jobs finish, up to `max_wait_s`; `settle_s` returns shortly after the first completion so you merge while stragglers run |
| `results` | Status, cost, changed files, branch and report per job |
| `cancel` | Stop queued or running jobs (partial work stays on the branch) |
| `list_workers` | Roles, server info, and with `include_models=true` the ZDR-capable models and prices |
| `usage_report` | Ledger roll-up by day, model, role or swarm |

Roles ship as Markdown with native-subagent frontmatter: `reviewer` and
`researcher` (read-only), `codegen` and `test-writer` (edit+bash in a
worktree). Add your own in the user config dir.

## Security

- ZDR on every request: `provider: {zdr: true, data_collection: "deny",
  require_parameters: true}`, hardcoded, nothing can turn it off.
- File tools are confined to the worker's workspace, symlinks included;
  CI configs, shell rc files and agent config are refused, lockfiles and
  `Dockerfile` are flagged.
- Bash runs only inside macOS Seatbelt or Linux `bwrap`: no network, writes
  confined to the worktree. No sandbox, no Bash.
- Worker reports come back labelled as untrusted data. No tool can change
  config or spend caps.

Threat model and reporting: [SECURITY.md](SECURITY.md). Design:
[SPEC.md](SPEC.md). Model choices come from [`bakeoff/`](bakeoff/README.md).

## License

MIT — see [LICENSE](LICENSE).
