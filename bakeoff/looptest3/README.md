# looptest3 — long, uneven worker jobs (cancel/straggler research)

Same `jobsched` base repo as `bakeoff/looptest`, six independent features, one worker job each:
X1–X3 small; X4, X5 large with a visible soak test that really sleeps (≈ 7 and ≈ 5 minutes per run);
X6 with a ≈ 8-minute soak whose last two tests contradict each other (see `X6_NOTES.md`) — its hidden
test passes, its soak never can. `goal.md` makes the worker run its feature's soak inside its job,
otherwise a lead simply runs the soaks itself and no long job exists.

```bash
uv run python bakeoff/looptest3/prepare.py selfcheck      # ≈ 20 min: bare 0/8, reference 8/8, X6 soak fails by design
uv run python bakeoff/looptest3/prepare.py new <dir>      # test repo with SPEC.md, GOAL.md, tests/soak/, .venv
uv run python bakeoff/looptest3/prepare.py score <dir>    # hidden tests + pristine soak files, JSON on stdout
```

Set `timeout_s: 1800` in config.yaml for a run (a legitimate job took 24 minutes). Results of the first
use: loop test 3 — no arm (cancel rule, no cancel sentence, per-task `timeout_s`) ever cancelled.
