# Security

This document is an honest threat model, not a marketing page. It describes
what anymodel-subagents contains, what it doesn't, and known residual risks.
Read it before pointing a worker at anything you care about.

## What's contained

- **Filesystem access.** Every file tool (`Read`, `Grep`, `Glob`, and in
  `edit` mode `Edit`/`Write`) resolves the target path, follows symlinks, and
  refuses anything that resolves outside the worker's workspace root (its
  `cwd`, or a worktree when isolated). A fixed deny list blocks writes to
  files that execute automatically and outside your review — CI configs,
  shell rc files, editor/agent config, other assistants' config files — and a
  separate "sensitive" list (lockfiles, `Dockerfile*`, `conftest.py`,
  `Makefile`, `package.json`, `pyproject.toml`, and similar) is allowed but
  flagged in `results` so you review it deliberately rather than letting it
  blend into an ordinary diff. Secret-shaped paths (`.env*` except documented
  templates, private keys, `.ssh`, `.aws`, credential files) are denied
  outright for both reads and writes.
- **`edit+bash` writes are policy-checked twice.** `edit+bash` always runs
  under worktree isolation (`isolation: "none"` is rejected outright for that
  mode) precisely because a sandboxed Bash command can write anywhere its
  sandbox profile permits — see `tools/bash.py`/`tools/sandbox.py` for that
  containment — which bypasses the Edit/Write path policy above entirely.
  Before the worktree is committed, every path git considers changed is
  re-checked against the same deny policy (`LocalWorkspace.is_write_denied`):
  a denied path is reverted to its base content (or removed, if new); any
  symlink is removed unconditionally, since the file tools never create one
  themselves; and a newly-set setuid/setgid bit is refused. Reverted paths
  are reported back as `policy_reverted_files`, with `sensitive_changed_files`
  covering the same "flagged, not blocked" files as above. See "Known
  residual risks" below for what this scan does *not* cover.
- **What sandboxed Bash can read outside the workspace.** System and toolchain
  locations (read-only), and — for `edit+bash` jobs on macOS, unless
  `bash_repo_venv: false` — the source repository's `.venv`, read-only, so the
  project's tests can run. That path is derived by the server from the job's
  validated repository, never from a worker or orchestrator, and is re-checked
  on every call: a real directory (not a symlink) owned by the current user,
  with no overlap with any denied path; its `pip.conf` stays unreadable. The
  venv's interpreter and scripts run under the same sandbox as any other
  worker command. `PATH` is built from known directories, not inherited from
  the server, and `PYTHONPATH` points at the workspace so the code under test
  is the worktree's — the rest of the source repository stays unreadable.
  A workspace's own `.venv` is used the same way when there is no source one;
  workers cannot create or change one (`.venv/**` and `venv/**` are write-denied
  and reverted at finalize), so that is only ever a venv the repository shipped.
- **Network.** A worker's only network access is the OpenRouter chat
  completions endpoint the engine itself calls. There is no browsing tool.
  The one exception is of the user's own making: `allow_unsandboxed_bash: true`
  lets an explicitly requested `edit+bash` job run with no OS sandbox, and its
  commands then run as you -- your files, your credentials, your network, and
  nothing to revert what they write outside the worktree.
  `edit+bash`'s own network posture (sandbox-enforced, no network) is
  documented alongside the sandbox implementation itself, below.
- **Web mode (`mode: "web"`).** Off by default; a user turns it on with
  `web_enabled: true` in `config.yaml` (invariant 1: no MCP tool can do this
  for them). A `web` job has no file tools, no `cwd`, no workspace, and no
  worktree (`isolation` must be `"none"`) — it is never combined with
  another mode, so there is nothing sensitive in its context beyond its own
  task prompt. `WebSearch`/`WebFetch` calls are made by the server process
  itself (Brave for search, Jina Reader for fetch), never from inside the
  sandbox and never by the worker opening a socket, so the "no network" Bash
  sandbox posture above is unaffected by this feature. `WebFetch` URLs are
  validated (`https` only, no userinfo, no IP-literal or internal hostnames)
  and checked against a domain denylist before the call; see `tools/web.py`,
  `web_client.py`, and `web_denylist.py`. See "Known residual risks" below
  for what this containment does *not* cover — an exfiltration channel, a
  guessable-domain denylist, and third-party retention.
- **Secrets in the worker's own process.** `OPENROUTER_API_KEY` is read by
  the server/engine, never placed in a worker's own environment or tool
  surface. The same holds for `BRAVE_API_KEY`/`JINA_API_KEY`: read by the
  server, used only for the outbound Brave/Jina HTTP call, and never placed
  in a worker's environment, tool results, or a web task's own transcript.
  Transcripts and the usage ledger run every string field through a
  redactor that strips both key-shaped patterns and the live key value; this
  is unit-tested, because equivalent redaction was found to be dead code in a
  prior, unrelated project. The web-provider keys are added to the same
  redactor live-secret list and covered by the same regression test as the
  OpenRouter key.
- **Config integrity.** No MCP tool can write `config.yaml`, allowlists, or
  anything else that changes the server's own policy. `config.yaml` is
  user-edited only.
- **Injection posture.** A worker's final report is returned to the
  orchestrator wrapped and explicitly labelled as untrusted data
  (`<worker_report trust="untrusted" boundary="…">`). The reader is a model,
  not an XML parser, so the wrapper does not rely on one exact closing tag:
  each report gets a random boundary generated after the worker has finished,
  repeated on the closing tag, and anything inside the report that looks like
  the wrapper's tags (any case, spacing, opening or closing) has its `<`
  replaced. A worker cannot forge a tag carrying a boundary it never saw.
  This raises the bar; it does not make instruction-like text inside a report
  safe to follow. File contents a worker reads are treated as data by that
  worker's own loop, the same way.

## Known residual risks

- **A report can lie about how its job ended.** A response cut off at the
  output-token cap is returned with `status: "error"`, an `error` saying so,
  and a `[truncated: ...]` note at the top of the report. The note is a
  convenience: a worker that finished normally can print the same line (or
  claim success it didn't have). `status` and `error` are set by the engine
  and are the only trustworthy signal; the report is untrusted text either
  way. The `max_turns` summary is the one case where truncation shows only as
  that note.

- **TOCTOU window in atomic writes.** Path resolution happens once per tool
  call; the actual write happens slightly later. `files.py` re-resolves the
  parent directory immediately before the atomic `os.replace()` and refuses
  if it changed, and refuses to write through a symlinked path component at
  open time — but full closure would need `O_NOFOLLOW` + component-by-component
  `openat()`, which isn't portably available from the Python standard
  library. An attacker with concurrent filesystem access to the same
  workspace (e.g. racing a symlink swap into a parent directory) could in
  principle exploit the gap. This is a documented, accepted residual risk,
  not a design goal met in full.
- **"Sensitive but allowed" files execute later, not now.** Files like
  `conftest.py`, `Makefile`, or `Dockerfile*` are flagged, not blocked,
  because a worker legitimately needs to edit them sometimes. A malicious or
  compromised worker can still write code into one of these that runs the
  next time *you* run tests, build, or otherwise invoke it — after the
  worker itself is done and its sandbox (if any) no longer applies. This is
  exactly why worktree isolation plus reviewing the diff before merging
  matters more for edit-mode jobs than for read-only ones: don't merge a
  branch you haven't looked at.
- **The spend budget is a soft cap.** It is checked between model requests, so
  the day cap can be overshot by at most one response per running job
  (`max_concurrency` of them). The per-swarm cap bounds one `dispatch` call
  only -- every call is a new swarm -- so the per-day cap is the backstop
  against a runaway orchestrator, and a credit limit on the OpenRouter key
  itself is the hard limit.
- **File names in `changed_files` are worker-chosen.** Tag characters and
  invisible characters are neutralised and prose-like names are withheld, but
  this is a courtesy filter, not a boundary: treat those lists as untrusted.
- **Repository history is within an `edit+bash` worker's reach.** A worktree
  needs the repository's object store to run `git status`/`git diff`, so it is
  readable inside the sandbox. The Bash allowlist no longer offers `git show`
  or `git log -p`, but the allowlist is not the boundary: a script the worker
  writes can run git itself. A secret that was committed once and later
  removed, or that lives on another branch, can therefore reach the outside
  model. Rotate such secrets; do not rely on their absence from HEAD.
- **Secret-shaped files in the workspace** (`.env`, `*.pem`, `id_rsa`,
  `.git-credentials`, ...) are denied to the file tools and, on macOS, to
  sandboxed commands by name in the Seatbelt profile. The list is by name: a
  secret in `config.yaml` or `settings.json` is just a file. Credential- and
  token-shaped data files (`aws_credentials.csv`, `*.token`, `token.txt`) are
  covered; source files with those words (`credentials.py`, `tokenizer.py`) stay
  readable. On Linux, bwrap masks the files that exist when a command starts.
- **A process that detaches from a Bash call** is found and killed when the
  call returns (parent-chain sampling plus the call's unique TMPDIR in its
  environment). On macOS a child that detaches, scrubs its environment and
  outlives its parent within one sampling interval can survive until the job
  ends; it stays inside the sandbox. On Linux the pid namespace dies with the
  call.
- **Git filter drivers.** A worktree is refused when the repository's local git
  config defines filter/textconv/merge drivers, `core.sshCommand`,
  `core.hooksPath`, an fsmonitor or an include, because finalize's `git add -A`
  would run them unsandboxed. Stock git-lfs filters are allowed. A driver set
  in the user's global git config is the user's own and is not audited.
- **`<state dir>/jobs/<job_id>/report.md`** is untrusted worker output at rest:
  wrapped and redacted like the returned report, and to be treated as data,
  never instructions, when read from disk.
- **The Linux (`bwrap`) sandbox has never had real escape tests.** The macOS
  Seatbelt profile is exercised by real `sandbox-exec` escape tests and has
  been attacked adversarially. The bwrap path has only tests of the argument
  list it builds, written on a Mac where bwrap cannot run; the sandbox test
  module is skipped entirely off macOS. `tests/test_sandbox_bwrap.py` is a real
  escape suite that CI's Ubuntu jobs are required to run (it fails rather than
  skips there); until it has run green, treat `edit+bash` on Linux as unproven.
- **Bash sandboxing is not fully portable.** `edit+bash` requires an OS
  sandbox (macOS Seatbelt via `sandbox-exec`, or Linux `bwrap` when present)
  and refuses to run without one, unless a user explicitly sets
  `allow_unsandboxed_bash: true` in their own `config.yaml` — no MCP tool can
  set that for them. See `tools/sandbox.py`/`tools/bash.py` for the sandbox
  profile itself (network posture, writable paths, denied reads) — that
  implementation is this document's other main section to keep current, and
  is intentionally not duplicated here. A command allowlist alone was
  deliberately not treated as sufficient containment: a worker can write an
  innocuous-looking test file and then run it via the test runner, so the
  allowlist is a second layer on top of sandboxing, not a replacement for it.
- **Redaction is pattern-based, best effort.** The live API key is scrubbed
  verbatim and in base64/JSON-escaped form, and provider error text is
  withheld outright if the key reappears once punctuation or invisible
  characters are removed. Other key-shaped strings are matched by pattern
  only: one split by an inserted character, a PEM block with more than 16 KB
  between its markers, or a key-shaped *file name* containing a non-ASCII
  character (in the dispatch dirty-tree warning) is not recognised. A
  provider message can also be withheld by mistake when it merely looks
  key-shaped. File names in that warning are escaped and labelled untrusted,
  but instruction-like text in a file name still reaches the orchestrator as
  quoted data.
- **Token/cost waste, not data exposure.** A worker with a bad or adversarial
  prompt (or a role file you didn't write yourself) can still spend up to its
  `max_turns` and wall-clock `timeout_s` doing useless or repetitive work.
  There's no enforcement of spend beyond the credit limit you set on your own
  OpenRouter key — `usage_report` is reporting, not a circuit breaker.
- **ZDR depends on OpenRouter honoring it.** Every request sets
  `provider: {zdr: true, data_collection: "deny", require_parameters: true}`,
  which restricts routing to providers OpenRouter has verified as
  zero-data-retention for that model. This is enforcement on OpenRouter's
  side, not something this codebase can independently verify per-request;
  it also requires your OpenRouter account to have ZDR enabled in its
  privacy settings, since a per-request flag can't override an
  account-level policy that disallows it.
- **Worktree cleanup is best-effort.** An isolated job's worktree and branch
  are removed automatically only when the worker leaves no changes; anything
  else is left for you to review and merge or discard deliberately. State-dir
  sweeping on startup only ever removes worktrees/jobs it can identify as its
  own (by path, inside its own state dir) and only past the configured
  retention window — never a general filesystem cleanup.
- **The write-policy scan on worktree jobs is post-hoc, not preventive.** It
  runs once, right before the worktree commit, against whatever git considers
  changed at that point — it stops a denied write from ever being committed
  or reaching `changed_files`, but it cannot undo an *effect* the write
  already had during the job itself (e.g. a script that read its own
  newly-written `.envrc` before the job ended, or that made a network call
  using the sandbox's own network posture rather than this scan, which never
  touches the network at all). It also runs only for worktree jobs; a
  sandboxed script's writes are, by construction, confined to the worktree
  checkout, so there is nothing outside it for the scan to reach.
- **In-place (`isolation: "none"`) edit jobs have no equivalent backstop.**
  There is no post-hoc scan or revert for a job editing the caller's own
  working tree directly — deliberately: the point of `isolation: "none"` is
  to let a worker touch files you may not have committed yet, so silently
  reverting anything would be its own hazard. Those jobs rely entirely on
  the Edit/Write tools' own path policy (and, in `edit+bash`, the sandbox
  profile) at write time; `results` does merge in any file changed by a
  sandboxed script that the tool-call bookkeeping alone would have missed
  (via a before/after `git status` snapshot), so you at least *see* it, but
  nothing is reverted for you. Prefer worktree isolation whenever you can.
- **Global caps (`max_live_jobs`, the 200-`job_ids`-per-call limit on
  `wait`/`results`/`cancel`, and the bound on finished jobs kept in memory)
  are availability protections, not confidentiality or integrity boundaries.**
  They stop one dispatch call from exhausting the concurrency semaphore or
  server memory; they don't change what any individual job can read, write,
  or spend.
- **A web task's own prompt is the exfiltration channel.** Web mode has no
  file tools and no workspace, so the only sensitive material a web query or
  fetch URL can carry is whatever the orchestrator put in that job's task
  prompt. There is no code-level control for this beyond the call/length
  caps and the denylist; it relies on the orchestrator (`skills/delegate`,
  the `AGENTS.md` snippet) never pasting secrets, credentials, customer
  data, or proprietary code into a web task. Treat that guidance as a
  process control, not a boundary this codebase enforces.
- **The launch denylist is a speed bump, not a boundary.** It blocks
  well-known exfiltration sinks (request catchers, paste sites, URL
  shorteners, tunnel services) by domain, merged from the bundled list, the
  user's `web-denylist.txt`, and `web_denylist_extra`. An attacker who
  controls what a worker fetches or searches for can register a fresh
  domain the list has never seen; the list stops casual/known sinks, not a
  targeted one. It also only ever applies to `WebFetch` URLs, not to what a
  `WebSearch` query itself contains. It checks only the first URL: the
  fetch provider follows redirects on its side, so an open redirector on an
  allowed site reaches a denied one.
- **Third-party retention of web queries and pages is real, and outside
  OpenRouter's ZDR.** OpenRouter's `zdr: true` covers only the chat
  completions call; it says nothing about Brave or Jina. Per each
  provider's own privacy notice (checked 2026-09-24): Brave keeps a query
  record for up to 90 days for billing and troubleshooting, and doesn't
  mention training. Jina Reader fetches are sent with `DNT: 1` on every
  request, which per Jina's own documentation means the request is not
  cached or logged — but that only covers the fetch call this codebase
  makes; a keyless (unauthenticated) Reader request is rate-limited but not
  otherwise different in this respect. Neither guarantee is a contractual
  ZDR term the way the OpenRouter routing preference is; both are the
  vendor's stated policy at the time this was written, not something this
  codebase can verify per-request. See ADR 0001 for the wider provider
  survey this decision was based on.
- **No anti-bot bypass, by design — a blocked fetch just fails.** The Jina
  Reader integration exposes no proxy, header, cookie, or rendering-engine
  knob to the model, and this codebase adds none of its own. A page that
  blocks the fetch returns a short "fetch failed: blocked by site" error to
  the worker, which is expected to move on to another source, not to retry
  around the block. Jina may itself retry a blocked fetch through its own
  proxy infrastructure (per the vendor's own public statement, 2026-05);
  that retry path is outside this codebase's control.
- **Supply-chain: pin, don't float.** Both the Claude Code plugin manifest
  and the Codex registration instructions install from
  `git+https://github.com/eschwa3/anymodel@<tag>` — a specific tagged commit,
  not a branch — because an unpinned `git+https://...` reference resolves to
  whatever that branch's HEAD happens to be at install time, which silently
  changes what code you're running between installs (or between two people's
  installs on different days) with no version anyone reviewed. Only install
  from a tagged release; PyPI (`uvx anymodel-subagents==X.Y.Z`, likewise
  pinned by exact version) is the intended long-term channel once the
  package is published there.

## Reporting a vulnerability

Please report security issues via a
[GitHub private security advisory](https://github.com/eschwa3/anymodel/security/advisories/new)
on this repository rather than a public issue. Include reproduction steps and
the affected version; expect an initial response within a few days while this
project is at alpha status (there is no formal SLA yet).
