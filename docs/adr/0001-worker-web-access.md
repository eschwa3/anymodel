# ADR 0001: Web search and fetch for workers

- **Status:** Accepted (2026-09-24). Providers: Brave (search) + Jina Reader (fetch); see Research findings
- **Date:** 2026-09-24
- **Decider:** Eric Schwartz
- **Touches:** `types.py` (`Mode`), `tools/` (new `web.py`), `jobs.py`, `config.py`, `ledger.py`,
  `redact.py`, `server.py` (dispatch description), `workers/`, `.claude-plugin/plugin.json`,
  `skills/delegate`, SPEC.md, SECURITY.md, docs/configuration.md

## Context

Orchestrators hand workers research tasks ("what changed in library X 3.0", "how do other projects
configure Y"). Right now workers can only read the workspace, so the orchestrator ends up doing
web research itself. It either uses Claude Code's built-in `WebSearch`/`WebFetch` or a Brave,
Tavily or Firecrawl MCP server, and pays frontier tokens for page text. That cuts against the
project's goal.

Workers can't use MCP servers configured in Claude Code or Codex. They run in our own engine loop,
and SPEC.md puts "MCP tools inside workers" out of scope for v1. So connecting a search MCP server
to the orchestrator helps native subagents, not anymodel workers.

The main risk is exfiltration. A worker that has sensitive data in its context and can make
outbound requests can leak that data through search queries or fetch URLs. Fetched pages are
attacker-controlled text, so prompt injection is expected, not hypothetical.

## Decision

1. Add a new worker **mode `web`** with exactly two tools, `WebSearch` and `WebFetch`. It gets
   **no** file, edit, or bash tools, and no workspace. `web` is never combined with another mode.
2. Add a bundled **`web-researcher`** role (`mode: web`). When the orchestrator needs web findings
   applied to code, it chains the jobs itself: web job first, then a code job with the findings
   pasted into its prompt.
3. Calls go through a small **provider interface**. v1 ships **Brave LLM Context**
   (`GET api.search.brave.com/res/v1/llm/context`) for `WebSearch` and **Jina Reader**
   (`r.jina.ai`, `DNT: 1` on every request) for `WebFetch`. Other adapters (Firecrawl, Parallel)
   can come later with no change to the tool surface.
4. The feature is **off by default**. It turns on only when the user sets `web_enabled: true` in
   `config.yaml` and a Brave key is present. No MCP tool can enable it (invariant 1).
5. The **Brave key** (required) and **Jina key** (optional; keyless Reader is rate-limited) are
   handled like the OpenRouter key. They come from plugin `userConfig` (`sensitive: true`, so they
   live in the Keychain) or `BRAVE_API_KEY` / `JINA_API_KEY`, and they are added to
   `redact`'s live-secret list. It never reaches a worker, a transcript, meta.json, the ledger, or
   an error message.
6. **Hard limits** apply per call and per job (see below). Every call is logged in the ledger as a
   call count and is **not** converted to USD.
7. A **portable launch denylist**, a plain-text domain list, blocks known exfiltration sinks. Its
   format is simple enough that other harnesses can load the same file.

## Design

### Mode and job flow

- `Mode = Literal["read-only", "edit", "edit+bash", "web"]`.
- `tools_for_mode("web", web=WebClient)` returns `[WebSearch(client), WebFetch(client)]`.
- In `web` mode, `cwd` is optional and ignored: there's no `validate_cwd`, no worktree, no
  `LocalWorkspace`. `isolation` must be `none`, and any other value is rejected. `changed_files`
  is always empty.
- If a `web` task is dispatched while web access is disabled, it fails validation with a short
  message ("web mode is disabled; enable it in config.yaml"). Nothing silently falls back.
- The `dispatch` description lists `web-researcher` only when web access is enabled, so
  orchestrators never route to a mode that would fail.
- The worker's report is wrapped `<worker_report trust="untrusted">` as usual. The role prompt
  asks for a URL citation on every claim.

### Tools (what the model sees)

| Tool | Args | Returns |
|---|---|---|
| `WebSearch` | `query` (≤ 400 chars), `max_results` (1–10, default 5) | title, URL, extracted snippet/content per result, total ≤ 12k chars |
| `WebFetch` | `url` (≤ 2048 chars) | page as markdown, truncated to 40k chars, with a truncation marker |

Tool results are data. Each one is wrapped in a `<web_content source="…" trust="untrusted">`
envelope so the role prompt can tell the model not to follow instructions found inside.

### Request rules

The server process calls the provider API. The worker never opens a socket, the OS sandbox stays
network-denied (invariant 5), and it isn't involved because `web` mode has no Bash.

- **URL validation before `WebFetch`:** `https` only. No userinfo, no IP-literal hosts, no
  `localhost`/`.local`/`.internal`/single-label hosts. The host is IDNA-normalized and checked
  against the denylist. With Jina, the provider does the fetch, so SSRF against the user's
  network doesn't apply. The IP checks stay in place for a future direct-fetch provider, which
  must also resolve the host and re-check it on every redirect (at most 5).
- **Provider HTTP:** GET-equivalent semantics only. The model can't set methods, headers,
  cookies, or bodies. Timeouts are 15 s per call and the response is capped at 2 MB before
  extraction.
- **Budgets:** `web_max_calls_per_job` (default 30). Over the cap, the tool returns an error and
  the worker has to finish with what it has. The existing turn, time, and USD budgets still apply
  to model spend.
- **Errors:** short and generic ("fetch failed: blocked domain", "search failed: provider
  error 429"). Provider text goes through `_clean_provider_text`-style scrubbing.

### Config (user-edited only)

```yaml
web_enabled: false            # master switch
web_max_calls_per_job: 30     # clamped 1..200
web_denylist_extra: []        # domains added to the bundled + user denylist files
```

Keys come from env: `BRAVE_API_KEY` (required when enabled), `JINA_API_KEY` (optional). The plugin
injects them from `userConfig.brave_api_key` / `userConfig.jina_api_key`. Codex users pass them with
`codex mcp add … --env BRAVE_API_KEY=…`. Flat keys match the rest of `config.yaml`.

**Anti-bot stance.** We add no bypass logic and expose no proxy, header, cookie, or engine knobs to
the model. A blocked page returns "fetch failed: blocked by site" and the worker moves on. Jina may
retry through its own proxy provider (maintainer statement, 2026-05); that is outside our control
and documented. Jina's robots.txt check stays off for single-page fetches (browser-equivalent).

### Portable launch denylist

This is a **speed bump for well-known exfiltration sinks**, not a boundary: an attacker can always
register a fresh domain. The real controls are "no secrets in a web worker's context" and the
call/length caps.

**Format (`web-denylist.txt`).** It stays deliberately minimal so any harness can parse it in a
few lines:

- UTF-8, one entry per line. `#` starts a comment. Blank lines are ignored.
- Each entry is a lowercase domain in IDNA A-label form (`xn--…`). There are no schemes, paths,
  ports, wildcards, or regex.
- **An entry matches the domain itself and every subdomain.** `example.com` blocks
  `a.b.example.com`. It does not block `notexample.com`.
- A leading `!` marks an exception that re-allows a subdomain of a blocked entry, e.g.
  `!raw.githubusercontent.com` under a blocked parent. The longest match wins.

**Sources, merged:**
1. Bundled `src/anymodel_subagents/web-denylist.txt`. The starter set covers request catchers
   and webhook testers, paste sites, URL shorteners, tunnel/ingress services, and
   anonymous file drops. It is reviewed like code.
2. The user file `${XDG_CONFIG_HOME:-~/.config}/anymodel-subagents/web-denylist.txt`.
3. `web_denylist_extra` in config.yaml.

**Reuse by other harnesses.** The file is the contract, and the matcher is ~15 lines, with a
reference implementation and test vectors (`tests/data/denylist_vectors.txt`: `host → allow|deny`)
shipped for porting. A CLI export turns it into native rules:
`anymodel-worker denylist --format claude-code` prints Claude Code `permissions.deny` entries
(`WebFetch(domain:…)`) to paste into settings. That way the same list protects native subagents'
built-in `WebFetch`. Before shipping the exporter, check whether Claude Code's `domain:` rule
matches subdomains. If it doesn't, the exporter has to emit both the apex and a `*.` form.
Other harnesses can read the `.txt` directly.

### Ledger

Each web call appends `{job_id, tool, provider, ok, chars_returned}`. Query and URL text are
**not** stored: they may carry sensitive text, and the job transcript already holds them in
redacted form. `usage_report` shows per-job and total web call counts next to USD.

### Orchestrator guidance (`skills/delegate`, AGENTS snippet)

- Use `web-researcher` for external facts: docs, changelogs, CVEs, API behavior, prior art.
- **Never paste secrets, credentials, customer data, or proprietary code into a web task.**
  The prompt is the only sensitive data a web worker has, so this rule is the main exfiltration
  control.
- To apply findings to code, chain a web job, then a code job. Don't ask a code worker to "look
  it up".

## Security analysis

| Threat | Mitigation |
|---|---|
| Repo or secret exfiltration via query/URL under injection | Web mode has no file tools and no workspace. Nothing sensitive is in context beyond the orchestrator's prompt (see guidance). Length caps, call cap, denylist. |
| Brave/Jina key leak | Kept out of worker env, argv, and tool results. Added to `redact` live secrets. Errors scrubbed. Regression test mirrors the OpenRouter key test. |
| SSRF / internal network probing | The provider does the fetch. `https` only, no IP literals, no internal hostnames. A future direct fetcher must re-check resolved IPs on every redirect. |
| Prompt injection from pages steering the orchestrator | Tool output wrapped `trust="untrusted"`. The report is already wrapped untrusted. The orchestrator skill treats reports as data. |
| Cost runaway | `max_calls_per_job`, existing turn/time/USD budgets, off by default. |
| Config tampering to enable web | Only the user-edited `config.yaml` enables it (invariant 1). Project `.workers/` roles can declare `mode: web`, but that doesn't enable anything. |
| Third-party retention of queries | Not covered by OpenRouter ZDR. Brave keeps queries ≤ 90 days (billing/troubleshooting); Jina fetches sent with `DNT` are not cached or logged. Documented in SECURITY.md and configuration.md. |

**Invariant changes (proposed for AGENTS.md):**
- 3 becomes: "The OpenRouter **and web-provider** keys never reach a worker…"
- New 9: "`web` mode has no workspace tools, and no other mode has network tools. Web requests are
  made by the server process, never from the sandbox; every URL passes `validate_web_url` and the
  denylist."

## Alternatives considered

- **OpenRouter web plugin (`plugins: [{id: "web"}]` / `:online`).** No new key, but a third-party
  search backend gets the queries outside our control. Its interaction with `zdr: true` and
  `require_parameters` is unverified, and it changes request bodies that invariant 2 protects.
  Rejected for v1. Could be revisited as a provider behind the same interface.
- **MCP client inside workers (connect Brave/Tavily/Firecrawl MCP).** Most general, but it opens
  every MCP server to cheap models and conflicts with SPEC's v1 non-goal. Rejected.
- **Orchestrator-side search, results pasted into prompts.** No code, but it spends frontier
  tokens, which is what we're trying to cut. Rejected.
- **Web tools alongside read-only repo tools.** More useful, but it puts repo contents and a
  network channel in one context. Rejected. Chaining covers the same use case.
- **Direct `httpx` fetch from the server.** Needs a full DNS-rebinding-safe SSRF guard. Deferred
  until a provider needs it.

## Consequences

- Research tasks move off the frontier model. That is expected to pay off most on
  changelog/docs lookups, where the page text is large and the answer is small.
- Two new third-party data processors (Brave, Jina/Elastic) and up to two more secrets.
- Two-step chains add latency for "look it up and apply it" tasks.
- The denylist file becomes a small public artifact that needs curating.

## Scope

**v1:** mode `web`, `WebSearch`/`WebFetch`, Brave + Jina providers, `web-researcher` role, config +
userConfig key, limits, denylist (bundled/user/config) with the reference matcher and test
vectors, `denylist --format claude-code` export, ledger counts, docs, delegate-skill guidance,
Codex registration docs.

**Later:** Brave and Firecrawl providers, a direct fetcher with a full SSRF guard, bake-off
`--suite web`, per-role provider choice, USD conversion of web credits.

**Not planned:** web tools in any mode that has workspace access, MCP inside workers.

## Implementation and verification

1. Lead: `Mode` + `tools_for_mode` signature, config schema, the `WebClient` protocol.
2. Builders, disjoint files: `tools/web.py` + provider, denylist + matcher + vectors, jobs/server
   wiring, ledger/usage, role + skill + docs.
3. Tests (respx for Brave/Jina, no real calls): tool arg caps, URL validation matrix, denylist
   vectors, key never in transcript/meta/ledger/errors, `web` rejects `isolation: worktree`,
   disabled-web dispatch fails, call cap, untrusted wrapping.
4. `security-pass` with PoCs: key extraction, exfil via query/URL, denylist bypass (case, IDNA,
   trailing dot, userinfo), injection-driven tool abuse.
5. Eric runs a live smoke test with his own Brave (and optionally Jina) keys.

## Research findings (2026-09-24)

1. **Provider retention.** Tavily's docs FAQ and marketing claim "zero data retention". Its
   [privacy policy](https://www.tavily.com/privacy) (updated 2025-11-24) says something different.
   Unless a contract says otherwise, Tavily "may use certain portions of your query data to
   improve our responses", "may also share your query data with third-party search index
   providers", and retains data for as long as the account exists. So for self-serve keys we treat
   queries as retained. ZDR has to be negotiated in an enterprise contract. For comparison,
   [Brave's API privacy notice](https://api-dashboard.search.brave.com/documentation/resources/privacy-notice)
   (updated 2026-08-25) keeps a query record for at most 90 days, for billing and troubleshooting.
   The notice doesn't mention training, and Brave runs its own index, so no third-party index sees
   queries. ZDR is enterprise-only. Firecrawl's ZDR (`zeroDataRetention`) is also enterprise-only.
2. **Extraction quality.** Tavily `/extract` has `extract_depth: basic|advanced`. Tavily's own
   skill docs say `advanced` handles JS-rendered pages. It costs 2 credits per 5 URLs instead of 1,
   with a 30 s default timeout instead of 10 s. The only benchmarks are run by vendors: Firecrawl
   reports 77–81% page coverage vs Tavily's 68%. (Superseded by the choice of Jina Reader, which
   renders JS in headless Chrome by default. A Firecrawl fetch adapter, pinned to
   `proxy: basic`, is the fallback if the live smoke test shows gaps.)
3. **Claude Code rule matching** ([permissions docs](https://code.claude.com/docs/en/permissions)).
   `WebFetch(domain:example.com)` matches that host **only**. `WebFetch(domain:*.example.com)`
   matches subdomains at any depth, but not the apex. Matching is case-insensitive and ignores a
   trailing dot. A `domain:` rule is also added to the sandbox's network deny list, so an exported
   rule blocks sandboxed Bash too. The exporter therefore emits **two** rules per entry (`d` and
   `*.d`). `!` exceptions have no Claude Code equivalent (deny beats allow), so the exporter skips
   the parent entry and prints a warning.

4. **Wider provider survey.** Primary-source policy text, checked 2026-09-24. No provider offers
   ZDR on a self-serve key by default. In several cases the marketing says "ZDR" and the policy
   or docs say something narrower.

| Provider | Search index (who sees the query) | Self-serve retention / training | URL fetch | Price / 1k |
|---|---|---|---|---|
| **Brave** | Own | ≤ 90 days, billing and troubleshooting only; training not mentioned | No | $5 search |
| **Jina** (Elastic since 2025-10) | Proxies Brave | "does not use Customer request data… to train"; per-request `DNT` header: "not cached or logged" | Yes, headless Chrome | Token-metered |
| **Parallel** | Not stated | EU Search endpoint: "we do not retain request or response content"; otherwise unspecified | Yes (Extract, JS, PDF) | $1–5 search, $1 fetch |
| **Perplexity Search** | Own | No-training/ZDR pledge covers Chat Completions only; Search API not stated | No | $5 search |
| **Firecrawl** | Undisclosed upstream | Not stated; ZDR is enterprise-only (+1 credit/page) | Yes, best coverage in vendor benchmarks | ~$1.2–6.4 search, $0.6–3.2 fetch |
| **Tavily** | Own + third-party fallback | May use queries to improve; may share with third-party indexes | Yes | credit-based |
| **Linkup** | Own (claimed) | ZDR "not enabled by default", enterprise on request | Yes (JS opt.) | $5 search, $1–10 fetch |
| **You.com** | Not stated | Retained "as necessary"; de-identified content used; ZDR enterprise-only | Yes | $5 search, $1 fetch |
| **Exa** | Own | "Query Data is used… including by training and fine-tuning models" | Yes | $7 search, $1 fetch |

   Google-scraping APIs (SerpApi etc.) were ruled out because of legal exposure.

## Open questions

- Should web-worker transcripts be kept for a shorter time than code-worker transcripts?
