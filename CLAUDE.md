@AGENTS.md

## Claude Code specifics

- Do what was asked. Do not refactor, rename, or add files beyond the request.
- Commits and PRs are authored by Eric alone. Never add `Co-Authored-By: Claude …` (or any
  Claude/Anthropic co-author, "Generated with Claude Code", or similar attribution) to commit
  messages, tags, or PR descriptions, and never commit as or with Claude.
- Orchestration model: Fable leads; Sonnet subagents build (`.claude/agents/builder.md`), an Opus
  subagent does adversarial review (`.claude/agents/security-reviewer.md`). Verify subagent claims
  yourself (rerun the suite, rerun the PoCs) before reporting them as done.
- Project skills live in `.claude/skills/`: `release`, `bakeoff`, `security-pass`. The root
  `skills/delegate` is the shipped plugin skill — edit it as product, not as tooling.
- Never handle Eric's OpenRouter key. Anything needing it is a command for him to run.
- Pushing workflow files needs the `workflow` scope on the gh token (already granted).
- Be terse: use the fewest words and tokens that carry the point. No preamble, no recap, no restating the question.