# Goal

This repository is `jobsched`, a small Python 3.12 job scheduler (stdlib + sqlite). `SPEC.md`
describes 6 independent features, X1 … X6. X1–X3 are small; X4 and X5 are large (three
sub-features each); X6 is medium. Build all of them.

Rules:
- You are the orchestrator. Delegate the building to anymodel-subagents workers (load the
  `delegate` skill first); do not use native subagents. Do the decomposition, verification and
  integration yourself. Keep your own context small: keep progress in `STATE.md` (done / in
  flight / next / problems), not in your head.
- One worker job per feature. Do not split a feature across jobs.
- Each feature lives in the new module(s) its spec section names. Do not change existing behaviour;
  `python -m pytest tests -q --ignore=tests/soak` must stay green. Add tests for what you build
  under `tests/`.
- X4, X5 and X6 each ship a slow soak test under `tests/soak/` (5 to 9 minutes per run). A feature
  with a soak test is done only when its soak test passes. Never edit anything under `tests/soak/`.
- The worker that builds X4, X5 or X6 must run that feature's soak test inside its own job and
  report the result. Do not run soak tests yourself until the final verification.
- Follow the repository's `AGENTS.md`.
- Verify before you integrate: run the tests yourself after every merge; never take a worker's
  word for it. Commit each integrated feature.
- You are done when all 6 features are integrated and the suite is green, or after 90 minutes.
  Finish by writing a short `REPORT.md`: features done, features not done and why, anything you
  are unsure about.

Acceptance tests you cannot see will be run against the final commit.
