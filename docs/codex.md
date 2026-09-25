# Codex CLI registration

anymodel-subagents is a plain stdio MCP server, so Codex CLI talks to it the
same way it talks to any other MCP server — no plugin system involved.

## Register it

macOS and Linux only; on Windows, use WSL2 (the server refuses to start on
native Windows).

```bash
codex mcp add anymodel -- uvx --from git+https://github.com/eschwa3/anymodel@v1.1.0 anymodel-subagents
```

The `@v1.1.0` tag pin is deliberate, not cosmetic: an unpinned
`git+https://...` reference resolves to whatever the default branch's HEAD
is *at install time*, so the same command run today and next month (or by
two different people) could silently fetch different, unreviewed code. Use
the current tag from this repo's releases page — never a bare branch name.

(Once the package is on PyPI, this becomes
`codex mcp add anymodel -- uvx anymodel-subagents==1.1.0`, pinned the same
way by an exact version instead of a tag.)

`codex mcp add`'s `--env KEY=VALUE` flag only writes a literal value into
`~/.codex/config.toml`. To avoid putting your OpenRouter key in a config file,
add the server with `codex mcp add` and then hand-edit the resulting block to
forward the variable **by name** from your shell environment instead of
writing its value — keeping the `@v1.1.0` pin in `args`:

```toml
[mcp_servers.anymodel]
command = "uvx"
args = ["--from", "git+https://github.com/eschwa3/anymodel@v1.1.0", "anymodel-subagents"]
env_vars = ["OPENROUTER_API_KEY"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

`env_vars` forwards named variables from Codex's own process environment into
the server's — set `OPENROUTER_API_KEY` in your shell profile (or wherever you
keep it) and Codex passes it through without it ever touching `config.toml`.

Fields used above (see the Codex MCP docs for the complete set):

| Field | Meaning |
|---|---|
| `command` / `args` | How to launch the server (same as `dispatch`'s underlying process) |
| `env_vars` | Names of environment variables to forward from Codex's own environment, unmodified |
| `env` | Literal `KEY = "value"` pairs written directly into config.toml (avoid for secrets) |
| `startup_timeout_sec` | How long Codex waits for the server to come up. Use **60**: the first `uvx --from git+…` start builds the environment and can exceed the 10 s default, after which the tools are reported as unavailable |
| `tool_timeout_sec` | How long Codex waits for one tool call to return. Codex kills an MCP tool call at 60 s by default, which is what `max_wait_s`'s 45 s default is sized for — leave `max_wait_s` at 45 under Codex, and raise this to **120** to give `wait` headroom rather than have Codex time out mid-poll |

Verify with `codex mcp list` / `codex mcp get anymodel`.

## Web research (optional)

`web-researcher` (`mode: web`) is off until `config.yaml` sets
`web_enabled: true` (see [configuration.md](configuration.md#web-settings))
and a Brave key is present. Forward it the same way as
`OPENROUTER_API_KEY`, by name rather than as a literal value in
`config.toml` — add it to the `env_vars` list from the hand-edited block
above:

```bash
codex mcp add anymodel --env BRAVE_API_KEY=<your-brave-key> -- uvx --from git+https://github.com/eschwa3/anymodel@v1.1.0 anymodel-subagents
```

```toml
env_vars = ["OPENROUTER_API_KEY", "BRAVE_API_KEY", "JINA_API_KEY"]
```

`JINA_API_KEY` is optional — omit it from `env_vars` and `WebFetch` still
works, keyless, at a lower rate limit. Read SECURITY.md before turning this
on: a web task's own prompt is the only thing that can leak through it.

## One config file per client

`max_wait_s` is the one setting that conflicts between clients: Claude Code
wants a long `wait` (600 s), Codex kills any tool call at `tool_timeout_sec`.
The server reads `$ANYMODEL_CONFIG` before the default path, so give Codex its
own file — the variable is a path, not a secret, so a literal `env` entry is fine:

```toml
[mcp_servers.anymodel]
command = "uvx"
args = ["--from", "git+https://github.com/eschwa3/anymodel@v1.1.0", "anymodel-subagents"]
env_vars = ["OPENROUTER_API_KEY"]
env = { ANYMODEL_CONFIG = "~/.config/anymodel-subagents/config-codex.yaml" }
startup_timeout_sec = 60
tool_timeout_sec = 120
```

```yaml
# ~/.config/anymodel-subagents/config-codex.yaml
max_wait_s: 100   # under tool_timeout_sec; wait needs no timeout_s and the AGENTS.md loop works as written
```

`list_workers` shows which file the server read (`server.config_path`) and the
effective `max_wait_s`. The ledgers are already separate: the Claude Code
plugin sets `ANYMODEL_STATE_DIR` to its plugin data directory, Codex uses the
default state directory, so `usage_report` and the daily budget cap are per
client. To share one ledger and one cap, add `ANYMODEL_STATE_DIR` to the same
`env` table (two servers running at once each seed their day total at startup,
so a shared cap can overshoot by what the other spent since then).

## Local development variant

To register a working copy of the repo instead of the `git+https` install:

```toml
[mcp_servers.anymodel]
command = "uv"
args = ["run", "--project", "/path/to/clone/anymodel", "anymodel-subagents"]
env_vars = ["OPENROUTER_API_KEY"]
tool_timeout_sec = 120
```

## AGENTS.md

Paste [`docs/AGENTS-snippet.md`](./AGENTS-snippet.md) into your project's
`AGENTS.md` so Codex knows when to delegate to workers without you repeating
the guidance every session.

## Verified behaviour (Codex 0.155)

- Keep `tool_timeout_sec = 120` and `max_wait_s: 100`. Raising them for one long `wait` per batch
  does not work: with `tool_timeout_sec = 600` / `max_wait_s: 590` Codex 0.155 abandoned the `wait`
  call after about 5 minutes without a result (the lead recovered with `results` and short waits).
  A 100 s `wait` loop is reliable.
- "Always approve" on an MCP tool writes `[mcp_servers.anymodel.tools.<tool>] approval_mode =
  "approve"` into the config file that defines the server. If that is a project's tracked
  `.codex/config.toml`, move those sections to `~/.codex/config.toml` — they merge with the
  project's server definition.
- Workers are verified end to end under Codex: read-only jobs, an `edit+bash` job in a worktree,
  `cancel`, `cwd` rejection and `usage_report`.
- A project-level `.codex/config.toml` with the `[mcp_servers.anymodel]` block works; the tools
  appear as `mcp__anymodel__dispatch`, `…__wait`, `…__results`, `…__cancel`, `…__list_workers`,
  `…__usage_report`.
- `env_vars = ["OPENROUTER_API_KEY"]` forwards the variable from the environment Codex was
  **launched from**. Export the key in that terminal first; if it is missing, `dispatch`
  returns `OPENROUTER_API_KEY is not set`.
- Set `startup_timeout_sec = 60`. The first `uvx --from git+…` start builds the package and can
  exceed Codex's 10 s default, after which the tools are reported as unavailable.
- Interactive sessions ask you to approve each MCP call. Headless `codex exec` runs with
  approvals disabled, so MCP calls fail unless you pass `--approve-for-me`.
- With `--approve-for-me`, Codex's automatic reviewer rejects `dispatch` as an unauthorized
  export of repository contents unless the authorization is explicit — put the authorization
  line from [AGENTS-snippet.md](AGENTS-snippet.md) in your `AGENTS.md`, or state it in the prompt.
  This is a sensible guardrail; do not bypass it with `--dangerously-bypass-approvals-and-sandbox`.
