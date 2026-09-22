# Claude Code plugin

## Install

macOS and Linux. On Windows, run Claude Code and the plugin inside WSL2 and
install `bubblewrap` there; the server refuses to start on native Windows.

```
/plugin marketplace add eschwa3/anymodel
/plugin install anymodel-subagents@anymodel
```

You'll be prompted for an OpenRouter API key (the `openrouter_api_key`
`userConfig` field declared in `.claude-plugin/plugin.json`). It's marked
`sensitive`, so Claude Code stores it in your OS keychain (or
`~/.claude/.credentials.json` as a fallback) rather than in `settings.json`,
and injects it into the MCP server's environment as `OPENROUTER_API_KEY`.

Before you paste in a key: create a **dedicated** OpenRouter key for this
plugin with a credit limit set (openrouter.ai/keys), and turn on **Zero Data
Retention** in your OpenRouter account's privacy settings
(openrouter.ai/settings/privacy) — every worker request already sets
`provider: {zdr: true, data_collection: "deny"}`, but that only has effect if
your account-level setting allows ZDR routing in the first place.

`plugin.json`'s `mcpServers` entry pins that install to a tagged release
(`git+https://github.com/eschwa3/anymodel@v1.0.0`) rather than a moving branch
HEAD, so a plugin install today and one next month run the exact same,
reviewed code — see SECURITY.md's supply-chain note. New tags are cut only by
the maintainer.

## What gets installed

- The `anymodel` MCP server (`dispatch`, `wait`, `results`, `cancel`,
  `list_workers`, `usage_report`) — run via `uvx` from this repo (the package
  isn't on PyPI yet; see below).
- The `delegate` skill, which tells Claude Code when to fan work out to
  workers instead of doing it inline.
- `/swarm` and `/workers-usage` commands.

## Future default: PyPI

Today `plugin.json` runs the server from a tagged GitHub release:

```json
"args": ["--from", "git+https://github.com/eschwa3/anymodel@v1.0.0", "anymodel-subagents"]
```

The `@v1.0.0` pin matters: without it, `uvx --from git+...` resolves to
whatever the default branch's HEAD happens to be *at install time*, which
means two people installing the same plugin on different days (or the same
person reinstalling later) could silently run different code — including
code introduced after a security review. Pinning to a tag makes every
install of a given plugin version byte-for-byte the same, reviewed commit.
PyPI (`uvx anymodel-subagents==X.Y.Z`) is the intended long-term channel —
also pinned by an exact version — once the package is published there; until
then, only use a tagged `git+https://...@vX.Y.Z` reference, never a bare
branch name or an unpinned repo URL.

Once `anymodel-subagents` is published to PyPI, this collapses to plain
`uvx anymodel-subagents==1.0.0` (`"command": "uvx"`), which is faster to
start and doesn't require git. Watch the CHANGELOG / releases for when that
switch lands.

## Local development

To iterate on the server itself, point the MCP entry at your working copy
instead of installing the plugin. Either:

**a) Run Claude Code against a local plugin directory:**

```bash
claude --plugin-dir /path/to/clone/anymodel
```

This loads `plugin.json` from your checkout directly — edit-test-repeat
without reinstalling. Run `/reload-plugins` after changes to pick them up in
the same session.

**b) Point `mcpServers` at `uv run` instead of `uvx`**, e.g. in a project's
own `.mcp.json` while testing:

```json
{
  "mcpServers": {
    "anymodel": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/clone/anymodel", "anymodel-subagents"],
      "env": {
        "OPENROUTER_API_KEY": "${user_config.openrouter_api_key}",
        "ANYMODEL_STATE_DIR": "${CLAUDE_PLUGIN_DATA}"
      }
    }
  }
}
```

`uv run --project <path>` runs the package from that checkout using its own
lockfile/venv, so changes to `src/anymodel_subagents/` take effect on the next
server restart without a reinstall.
