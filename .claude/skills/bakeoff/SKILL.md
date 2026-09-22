---
name: bakeoff
description: Run, read, or extend the model bake-off that picks worker models per role. Use when Eric mentions the bake-off, model defaults, comparing worker models, or pastes/points at bakeoff/runs results.
---

# Bake-off

Eric runs it (it needs his OpenRouter key, which you never handle); you prepare the command and
analyze results.

## Commands for Eric

```bash
uv run python bakeoff/run.py --suite real --models <a,b,c> --repeats 3 --yes
```
`--suite smoke` is the old 3-task tier (saturates; only good as a connectivity check).
`--tasks R1,R4` narrows; `--bash` gives codegen/test-writer tasks sandboxed Bash;
`--judge-model <id>` re-grades free-text tasks with an LLM (stored alongside heuristic scores).
Offline check: `--dry-run --models fake-a`. Don't `uv sync` or reinstall the package while a
run is in flight — it shares `.venv`.

## Reading results

Latest run: `ls -t bakeoff/runs | head -1`; read `summary_real.md` and `results_real.jsonl`.
- Use engine `duration_s`, never harness wall time.
- Rank per ROLE (reviewer = R1+R2, researcher = R3+R7, codegen = R4+R5+R8, test-writer = R6):
  score first, then cost, then invalid-tool-call rate and `max_turns` hits. With < 3 repeats,
  call small gaps noise.
- R1/R2 false positives and `report_tokens_est` matter: the orchestrator pays premium rates to
  read and chase worker reports.
- R7 obedience rate is a safety gate: a model that obeys planted injections should not be a
  default for any role, whatever its score.
- Heuristic scores on free-text tasks are rough; spot-read `final_message`s (or use a judge)
  before recommending a change.

## Applying a decision

Role defaults live in `src/anymodel_subagents/workers/*.md` (`model:`) and
`Config.default_model`; consensus models are named in `skills/delegate/SKILL.md`. Update tests
that assert defaults, and note the change + evidence (run dir, scores, cost) in the commit body.

## Extending

Fixture: `bakeoff/fixture_real/` (built from commit snapshots); hidden answers/tests/mutants:
`bakeoff/hidden_real/`. No comments or docstrings may give planted defects away. Every new
task needs a verification test in `bakeoff/tests/` proving it discriminates (reference solution
scores ~1.0, unmodified fixture / bad output scores low).
