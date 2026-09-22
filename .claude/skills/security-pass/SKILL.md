---
name: security-pass
description: Run the review → fix → verify security loop on anymodel-subagents. Use after changes to tools/, the sandbox, jobs, worktree, server, engine, or redaction; before a release; or when Eric asks for a security review of this project.
---

# Security pass

Two reviews of this codebase each found proven Critical issues that unit tests had missed
(regex-bomb event-loop freeze; keychain readable from inside the sandbox via mach services;
persistence planted by sandboxed scripts bypassing the file-tool deny list). Assume the next
change has one too.

1. **Scope.** `git diff <last tag>..HEAD --stat`; list the changed boundary files and what each
   change could weaken (see the invariants in AGENTS.md).
2. **Review.** Launch the `security-reviewer` agent (Opus) with the scope, the scratchpad path for
   PoCs, and any area you are specifically worried about. Do not review your own wave instead of it.
3. **Triage.** Critical/High block the release. For each finding decide: fix now, or accept and
   document in SECURITY.md with the reason. Prefer structural fixes (deny-default, force
   isolation, derive from git) over growing blocklists.
4. **Fix.** `builder` agents with disjoint file ownership; every finding gets a regression test
   that reproduces the PoC (real `sandbox-exec` where relevant, skipped when unavailable).
5. **Verify yourself.** Rerun the reviewer's PoCs against the fixed code from the scratchpad —
   don't rely on the fixer's report. Then full suite + lint. Watch for probes that "pass" for the
   wrong reason (syntax error, nonexistent decoy).
6. **Record.** Update SECURITY.md residual risks; commit with the findings summarized in the body.

Linux/bwrap cannot be exercised on this Mac: CI's Ubuntu jobs are the only execution of it.
Until a real bwrap escape suite runs there, SECURITY.md must say so.
