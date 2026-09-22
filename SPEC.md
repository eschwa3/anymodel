# anymodel-subagents — v1 Spec (draft)


An MCP server that gives Claude Code and Codex CLI **subagents that run on outside models** (OpenRouter first). Orchestrator spends a few hundred tokens per task; the worker burns cheap tokens doing the reading, searching, editing and testing, and hands back only a final message.

## Goals / non-goals

- Mimic native subagent semantics: fresh context, own tool loop, caller's cwd, optional worktree isolation, only the final message returns.
- One server, both clients, day 1. Published, so: no personal paths, secure defaults, clear docs.
- ZDR-only routing. Cost reporting (no enforcement; limits live on the OpenRouter key).
- **Not** in v1: local models (just a base-URL swap later), wrapping other agent CLIs, SSH/remote hosts, nested workers, MCP tools inside workers, config-mutating tools of any kind.

## MCP tools (6)

| Tool | Purpose |
|---|---|
| `list_workers` | Roles available (name, description, model, mode) + tool-capable ZDR models with prices. |
| `dispatch` | `tasks[]` of `{prompt, cwd, role?, model?, mode?, isolation?, role_prompt?, max_turns?, label?}` → job ids immediately. One task or a swarm. |
| `wait` | Long-poll ≤ `max_wait_s` (default 45 s: Codex `tool_timeout_sec` default is 60) for any/all of given jobs; returns statuses. |
| `results` | Per job: final message, changed files, branch/worktree path, turns, tokens, cost. Full transcript stays on disk (path returned), never in the response. |
| `cancel` | Stop jobs. |
| `usage_report` | Ledger roll-up by day / swarm / role / model. |

## Worker engine (own loop)

- `httpx` → `POST https://openrouter.ai/api/v1/chat/completions` with tool calling; asyncio fan-out, global concurrency cap (default 8), per-job `max_turns` (default 40) and wall-clock timeout, retry on 429/5xx.
- Every request carries `provider: {zdr: true, data_collection: "deny", require_parameters: true}` — hardcoded on; nothing in config or any MCP tool can turn it off.
- Cost from `usage.cost` (always present in OpenRouter responses) + cached/reasoning token details → append-only `ledger.jsonl`.
- Context given to a worker: role prompt + task prompt + the project's `AGENTS.md`/`CLAUDE.md` (size-capped) + cwd listing. Never the orchestrator's conversation.
- Context hygiene inside the loop: tool outputs truncated, simple oldest-tool-result eviction when near the model's window.

## Worker tools & modes

Names and shapes mirror Claude Code's tools (models are well-trained on them).

| Mode | Tools |
|---|---|
| `read-only` (default) | `Read`, `Grep`, `Glob` |
| `edit` | + `Edit`, `Write` |
| `edit+bash` | + `Bash` |

**Path policy (all file tools):** realpath must be inside the workspace root (cwd, or the worktree when isolated); symlinks resolved before the check; deny `.git/` internals, `.env*`, key/credential patterns; size caps on reads and writes.

**Isolation:** `isolation: "worktree"` creates `git worktree add` on a new branch from the caller's HEAD under the plugin's state dir; removed automatically if the worker leaves no changes, otherwise branch + path returned for the orchestrator to diff/merge. Default for `edit` when >1 task in a dispatch writes to the same repo; `edit+bash` always runs in a worktree (`isolation: "none"` is rejected for it); `results` flags overlapping changed files across a swarm.

**Bash (v1, on by request via mode):**
- A command allowlist alone is not containment — a worker can write a test that does anything, then run `pytest`. So Bash is **sandboxed**: macOS Seatbelt (`sandbox-exec`, same mechanism Codex uses), Linux `bwrap` when present. Profile: writes only inside workspace + a temp dir, **no network**, reads denied for `~/.ssh`, `~/.aws`, keychains, the plugin's own config/state.
- Environment scrubbed to a minimal set; the OpenRouter key is never in a worker's env.
- Allowlist of command prefixes (config file, user-edited only; ships with common test/lint/build/`git status|diff|log` entries) as a second layer; argv exec, no shell string unless the sandbox is active.
- No sandbox available → Bash refuses, unless the user sets `allow_unsandboxed_bash: true` in the config file. No MCP tool can change any of this.
- Per-command timeout and output cap.

**Injection posture:** worker output returned to the orchestrator is wrapped and labelled as untrusted worker output; file contents read by workers are data. Secrets redacted in transcripts (key patterns + the live key value) — and tested, since this was dead code in unlimited-mcp.

## Roles (routing)

`workers/*.md`, same shape as native agent files:

```markdown
---
name: reviewer
description: Reviews a diff or files for bugs; read-only.
model: deepseek/deepseek-v4.1-flash
mode: read-only
max_turns: 25
---
<system prompt>
```

Ships with `reviewer`, `researcher`, `codegen` (edit, worktree), `test-writer`. Search order (later overrides earlier by `name`): bundled → user config dir → project `.workers/` (project roles load only when the config sets `allow_project_roles: true`). Role list is rendered into the `dispatch` tool description so both orchestrators see identical routing; CLAUDE.md/AGENTS.md shrink to one policy line ("delegate X/Y/Z to workers"). `model` override allowed per task. Multi-model consensus = dispatch the same prompt under N models.

## Packaging

- Python 3.12+ (3.11 has an asyncio subprocess-cancellation hang), MCP SDK, `httpx`; run via `uvx` (system Python on macOS is 3.9 — uv manages it).
- **Claude Code:** plugin with `.claude-plugin/plugin.json` → `mcpServers` entry; key via `userConfig` (`sensitive: true` → Keychain) injected as env; `skills/delegate/SKILL.md` (when/how to fan out, dispatch→wait→results, review diffs not prose); state in `${CLAUDE_PLUGIN_DATA}`.
- **Codex:** `codex mcp add … --env OPENROUTER_API_KEY=…` + AGENTS.md snippet; optional `~/.codex/agents/*.toml` wrapper later.
- State/config: XDG dirs, `0700`; jobs dir holds transcripts + meta; startup sweep removes clean worktrees and jobs older than N days only inside its own state dir.
- Publishing hygiene: CI runs tests + lint on PRs, pinned lockfile, PyPI trusted publishing, SECURITY.md describing the threat model honestly.

## Build order

1. Engine: OpenRouter client + loop + read-only tools + ledger; CLI harness to run one worker without MCP. Tests for path policy.
2. MCP layer: dispatch/wait/results/cancel, job store, concurrency.
3. Edit tools + worktree isolation + conflict flagging.
4. Sandboxed Bash (Seatbelt first, bwrap second) + redactor, with escape tests (Seatbelt only so far; bwrap has argv-construction tests only).
5. Roles, `list_workers`, `usage_report`.
6. Claude Code plugin wrapper + skill; Codex registration docs; verify both clients end-to-end.
7. CI, SECURITY.md, README, publish.

## Open items

- Resolved — default model IDs: `deepseek/deepseek-v4.1-flash` (config/server default, and the `reviewer`/`researcher`/`test-writer` roles) and `z-ai/glm-5.3-flash` (`codegen`); model discovery uses OpenRouter's public, keyless `/endpoints/zdr` listing.
- Verify whether Codex runs stdio MCP servers outside its sandbox (assumed yes; design doesn't depend on it).
- Resolved — name: `anymodel-subagents`.
