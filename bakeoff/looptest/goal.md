# Goal

This repository is `jobsched`, a small Python 3.12 job scheduler (stdlib + sqlite). `SPEC.md`
describes 16 features: 12 independent ones, L1-F1 … L3-F4, and 4 integration features,
L4-F1 … L4-F4, each of which builds on several of the first twelve. Build all of them.

Rules:
- You are the orchestrator. Delegate the building to subagents/workers; do the decomposition,
  verification and integration yourself. Work in batches and keep your own context small: keep
  progress in `STATE.md` (done / in flight / next / problems), not in your head.
- Each feature lives in the new module its spec section names. Do not change existing behaviour;
  `python -m pytest tests -q` must stay green. Add tests for what you build under `tests/`.
- Follow the repository's `AGENTS.md`.
- Verify before you integrate: run the tests yourself after every merge; never take a worker's
  word for it. Commit each integrated feature.
- You are done when all 16 features are integrated and the suite is green, or after 120 minutes.
  Finish by writing a short `REPORT.md`: features done, features not done and why, anything you
  are unsure about.

Acceptance tests you cannot see will be run against the final commit.
