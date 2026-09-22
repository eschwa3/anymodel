---
name: release
description: Cut a tagged release of anymodel-subagents. Use when asked to release, tag, bump the version, or publish, or after changes that users of the pinned plugin install need.
---

# Releasing anymodel-subagents

Users install from a pinned tag (`uvx --from git+https://github.com/eschwa3/anymodel@vX.Y.Z`), so
the version string must move in lockstep everywhere. Confirm with Eric before tagging or pushing.

1. Green first: `uv run pytest -q`, `uv run pytest bakeoff/tests -q`,
   `uv run ruff check src tests bakeoff`, `uv run ruff format --check src tests`,
   `claude plugin validate . --strict`. CI on `main` must be green (`gh run list -L 1`).
2. If `tools/`, sandbox, `jobs.py`, `worktree.py`, `server.py`, `engine.py`, or `redact.py`
   changed since the last tag, run the `security-pass` skill first.
3. Bump the version in every place — find them rather than trusting this list:
   `grep -rn "0\.1\.0\|@v0" --include="*.toml" --include="*.json" --include="*.md" --include="*.py" . | grep -v uv.lock`
   Expected: `pyproject.toml`, `src/anymodel_subagents/__init__.py`, `.claude-plugin/plugin.json`
   (version + the `@vX.Y.Z` in `mcpServers` args), `.claude-plugin/marketplace.json`,
   `docs/claude-code.md`, `docs/codex.md`, `README.md`. Then `uv lock`.
4. Commit, push, wait for CI green, then `git tag -a vX.Y.Z -m "vX.Y.Z"` and `git push origin vX.Y.Z`.
   Never move an existing tag — users are pinned to it.
5. Verify the pinned install from a clean state dir: start
   `uvx --from git+https://github.com/eschwa3/anymodel@vX.Y.Z anymodel-subagents` over stdio,
   `initialize`, `list_tools` (expect dispatch, wait, results, cancel, list_workers, usage_report).
6. PyPI (once set up) uses trusted publishing from the tag; then docs switch to
   `uvx anymodel-subagents==X.Y.Z`.
