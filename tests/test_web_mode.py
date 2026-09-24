"""Tests for `web` mode (docs/adr/0001-worker-web-access.md).

Covers jobs.py validation/wiring (mode resolution, optional cwd, isolation,
lazy web client/denylist, redaction), engine.py (system prompt shape,
`web_calls` counting, secret redaction reach), server.py (dispatch/list_workers
role visibility), ledger.py (`web_calls` totals), and a regression check that
the Bash tool's allowlist-built environment never carries the web provider
keys (tools/bash.py's `_build_env` never copies `os.environ`, so nothing
copies BRAVE_API_KEY/JINA_API_KEY into a worker's shell either).

Follows test_jobs.py's/test_engine.py's own fake patterns (FakeClient,
`make_cfg`, `stub_roles`, respx-mocked OpenRouter). `tools/web.py`,
`web_client.py`, and `web_denylist.py` are exercised for real here (they are
fully implemented, not stubs, as of this writing) except where a test
specifically wants to control their behavior (a fake denylist / a failing
factory), in which case the relevant `jobs.<name>` binding is monkeypatched,
exactly as test_jobs.py does for `create_worktree`/`validate_cwd`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx

from anymodel_subagents import jobs, ledger, server
from anymodel_subagents.config import Config
from anymodel_subagents.engine import build_system_prompt
from anymodel_subagents.openrouter import OpenRouterClient
from anymodel_subagents.roles import Role
from anymodel_subagents.tools.bash import _build_env as bash_build_env
from anymodel_subagents.tools.workspace import LocalWorkspace, NullWorkspace
from anymodel_subagents.types import PolicyError, Usage, WebHit, WebPage, WorkerResult

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = "sk-or-v1-webmodetestkey0000000000000"
WEB_KEY = "brave-sentinel-key-0000000000000000"


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeClient:
    """Stand-in OpenRouter client (mirrors test_jobs.py's own FakeClient)."""

    def __init__(self, key: str = "sk-or-v1-jobstestkey0000000000000") -> None:
        self._key = key

    def redaction_secrets(self) -> list[str]:
        return [self._key]

    async def aclose(self) -> None:
        pass


def make_client_factory(key: str | None = "sk-or-v1-jobstestkey0000000000000"):
    def factory() -> FakeClient:
        return FakeClient(key)

    return factory


def make_openrouter_client_factory(key: str = OPENROUTER_KEY):
    def factory() -> OpenRouterClient:
        return OpenRouterClient(key)

    return factory


class FakeWebClient:
    """A `WebClient` whose results can be made to "leak" the live key, for the
    redaction tests -- a real provider never echoes its own key back, but the
    pipeline must scrub it anyway (defense in depth)."""

    def __init__(self, key: str = WEB_KEY, *, leak_in_results: bool = False) -> None:
        self.key = key
        self.leak_in_results = leak_in_results
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def redaction_secrets(self) -> list[str]:
        return [self.key]

    async def search(self, query: str, max_results: int) -> list[WebHit]:
        self.searches.append(query)
        snippet = f"as seen in {self.key}" if self.leak_in_results else "an ordinary snippet"
        return [WebHit(url="https://example.com/a", title="Example", snippets=[snippet])]

    async def fetch(self, url: str) -> WebPage:
        self.fetches.append(url)
        content = f"page mentions {self.key}" if self.leak_in_results else "ordinary page content"
        return WebPage(url=url, title="Example page", content=content)

    async def aclose(self) -> None:
        pass


def make_web_client_factory(
    key: str = WEB_KEY,
    *,
    error: Exception | None = None,
    leak_in_results: bool = False,
):
    calls: list[int] = []

    def factory() -> FakeWebClient:
        calls.append(1)
        if error is not None:
            raise error
        return FakeWebClient(key, leak_in_results=leak_in_results)

    factory.calls = calls
    return factory


class FakeDenylist:
    def __init__(self, blocked: frozenset[str] = frozenset()) -> None:
        self._blocked = blocked

    def is_blocked(self, host: str) -> bool:
        return host in self._blocked


def fake_load_denylist(extra=()):
    return FakeDenylist()


def make_cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "default_model": "test/default-model",
        "max_concurrency": 2,
        "max_turns": 5,
        "timeout_s": 30.0,
        "max_tasks_per_dispatch": 5,
        "allowed_roots": (),
        "job_retention_days": 7,
        "web_enabled": False,
        "web_max_calls_per_job": 30,
    }
    base.update(overrides)
    return Config(**base)


def fake_validate_cwd(cwd: str, cfg: Config) -> Path:
    p = Path(cwd)
    if not p.is_absolute():
        raise ValueError("cwd must be an absolute path")
    return p


async def immediate_run_worker(**kwargs: Any) -> WorkerResult:
    return WorkerResult(
        status="completed",
        final_message="done",
        model=kwargs["model"],
        turns=1,
        usage=Usage(cost=0.001),
    )


def make_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_worker=immediate_run_worker,
    cfg: Config | None = None,
    client_factory=None,
    web_client_factory=None,
) -> jobs.JobManager:
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)
    monkeypatch.setattr(jobs, "load_denylist", fake_load_denylist)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return jobs.JobManager(
        cfg or make_cfg(),
        state,
        client_factory or make_client_factory(),
        run_worker=run_worker,
        web_client_factory=web_client_factory,
    )


def make_role(
    name: str = "web-researcher",
    *,
    description: str = "Researches the web.",
    model: str = "role/model",
    mode: str = "web",
    isolation: str | None = "none",
    max_turns: int | None = 20,
    prompt: str = "You are a web research subagent.",
    source: str = "bundled",
) -> Role:
    return Role(
        name=name,
        description=description,
        model=model,
        mode=mode,  # type: ignore[arg-type]
        isolation=isolation,
        max_turns=max_turns,
        prompt=prompt,
        source=source,  # type: ignore[arg-type]
        path=Path(f"/bundled/{name}.md"),
    )


def stub_roles(monkeypatch: pytest.MonkeyPatch, roles: dict[str, Role]) -> None:
    def fake_load_roles_with_warnings(*, project_dir=None, cfg=None):
        return dict(roles), []

    monkeypatch.setattr(jobs, "load_roles_with_warnings", fake_load_roles_with_warnings)


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    start = time.monotonic()
    while True:
        if predicate():
            return True
        if time.monotonic() - start > timeout:
            return False
        await asyncio.sleep(interval)


def make_tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def resp(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    usage: dict | None = None,
    finish_reason: str | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    choice: dict[str, Any] = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return httpx.Response(
        200, json={"choices": [choice], "usage": usage if usage is not None else {}}
    )


class MinimalManager:
    """Just enough of JobManager's surface for `server.build_server` (no tool is called)."""


# ---------------------------------------------------------------------------
# jobs.py: validation
# ---------------------------------------------------------------------------


async def test_web_mode_disabled_rejected(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(web_enabled=False))
    with pytest.raises(ValueError, match="web mode is disabled"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])


async def test_web_mode_enabled_without_factory_rejected(tmp_path, monkeypatch):
    mgr = make_manager(
        tmp_path, monkeypatch, cfg=make_cfg(web_enabled=True), web_client_factory=None
    )
    with pytest.raises(ValueError, match="web mode is disabled"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])


async def test_web_mode_accepts_missing_cwd(tmp_path, monkeypatch):
    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])
    assert created[0].spec.cwd == ""
    assert await _wait_until(lambda: mgr.get(created[0].job_id).status == "completed")


async def test_cwd_still_required_for_non_web_modes_same_message(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=r"task 0: cwd is required"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="")])


async def test_web_mode_rejects_worktree_isolation(tmp_path, monkeypatch):
    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    with pytest.raises(ValueError, match="isolation"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web", isolation="worktree")])


async def test_web_mode_accepts_explicit_isolation_none(tmp_path, monkeypatch):
    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd="", mode="web", isolation="none")]
    )
    assert created[0].spec.isolation == "none"


async def test_role_with_web_mode_resolves(tmp_path, monkeypatch):
    role = make_role()
    stub_roles(monkeypatch, {"web-researcher": role})
    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch(
        [jobs.TaskSpec(prompt="p", cwd="", role="web-researcher")]
    )
    assert created[0].spec.mode == "web"
    assert created[0].spec.isolation == "none"
    assert await _wait_until(lambda: mgr.get(created[0].job_id).status == "completed")


async def test_jobs_rejects_project_sourced_web_role_defense_in_depth(tmp_path, monkeypatch):
    """roles.py already refuses to load a project-sourced role file declaring
    `mode: web` (see test_roles.py), so a project-sourced `Role` object with
    `mode == "web"` should never actually reach `_validate_task`. This
    exercises jobs.py's own defense-in-depth guard directly by monkeypatching
    the role loader to hand one back anyway.
    """
    role = make_role(source="project")
    stub_roles(monkeypatch, {"web-researcher": role})
    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True, allow_project_roles=True),
        web_client_factory=make_web_client_factory(),
    )
    with pytest.raises(ValueError, match="project-sourced role"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", role="web-researcher")])


async def test_web_role_still_rejected_when_disabled(tmp_path, monkeypatch):
    role = make_role()
    stub_roles(monkeypatch, {"web-researcher": role})
    mgr = make_manager(tmp_path, monkeypatch, cfg=make_cfg(web_enabled=False))
    with pytest.raises(ValueError, match="web mode is disabled"):
        await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", role="web-researcher")])


# ---------------------------------------------------------------------------
# jobs.py: tool wiring (real tools_for_mode / web_tools)
# ---------------------------------------------------------------------------


async def test_web_job_gets_only_web_tools_and_null_workspace(tmp_path, monkeypatch):
    captured: list[dict[str, Any]] = []

    async def capturing_run_worker(**kwargs: Any) -> WorkerResult:
        captured.append(kwargs)
        return WorkerResult(
            status="completed", final_message="done", model=kwargs["model"], turns=1, usage=Usage()
        )

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        run_worker=capturing_run_worker,
        cfg=make_cfg(web_enabled=True, web_max_calls_per_job=7),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])
    assert await _wait_until(lambda: mgr.get(created[0].job_id).status == "completed")

    assert len(captured) == 1
    tool_names = {t.name for t in captured[0]["tools"]}
    assert tool_names == {"WebSearch", "WebFetch"}
    assert isinstance(captured[0]["ws"], NullWorkspace)


async def test_web_job_workspace_root_is_not_the_callers_repo(tmp_path, monkeypatch):
    captured: list[dict[str, Any]] = []

    async def capturing_run_worker(**kwargs: Any) -> WorkerResult:
        captured.append(kwargs)
        return WorkerResult(
            status="completed", final_message="done", model=kwargs["model"], turns=1, usage=Usage()
        )

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        run_worker=capturing_run_worker,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd=str(tmp_path), mode="web")])
    assert await _wait_until(lambda: len(captured) == 1)
    ws = captured[0]["ws"]
    assert ws.root != tmp_path
    with pytest.raises(PolicyError, match="no workspace"):
        ws.resolve("anything")


# ---------------------------------------------------------------------------
# jobs.py: denylist failures are scoped to web jobs only
# ---------------------------------------------------------------------------


async def test_bad_denylist_fails_only_web_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "validate_cwd", fake_validate_cwd)

    def bad_load_denylist(extra=()):
        raise ValueError("bad user denylist: line 3: domain must not include a scheme or path")

    monkeypatch.setattr(jobs, "load_denylist", bad_load_denylist)

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    mgr = jobs.JobManager(
        make_cfg(web_enabled=True),
        state,
        make_client_factory(),
        run_worker=immediate_run_worker,
        web_client_factory=make_web_client_factory(),
    )

    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="web task", cwd="", mode="web"),
            jobs.TaskSpec(prompt="normal task", cwd=str(tmp_path), mode="read-only"),
        ]
    )
    assert await _wait_until(
        lambda: all(mgr.get(j.job_id).status in ("completed", "error") for j in created)
    )

    web_job = mgr.get(created[0].job_id)
    normal_job = mgr.get(created[1].job_id)
    assert web_job.status == "error"
    assert web_job.error is not None and "web mode unavailable" in web_job.error
    assert normal_job.status == "completed"


async def test_missing_web_key_fails_only_that_job(tmp_path, monkeypatch):
    from anymodel_subagents.web_client import MissingWebKeyError

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(
            error=MissingWebKeyError("BRAVE_API_KEY is not set")
        ),
    )
    _swarm_id, created = await mgr.dispatch(
        [
            jobs.TaskSpec(prompt="web task", cwd="", mode="web"),
            jobs.TaskSpec(prompt="normal task", cwd=str(tmp_path), mode="read-only"),
        ]
    )
    assert await _wait_until(
        lambda: all(mgr.get(j.job_id).status in ("completed", "error") for j in created)
    )
    web_job = mgr.get(created[0].job_id)
    normal_job = mgr.get(created[1].job_id)
    assert web_job.status == "error"
    assert web_job.error is not None and "web mode unavailable" in web_job.error
    assert normal_job.status == "completed"


# ---------------------------------------------------------------------------
# engine.py: system prompt shape
# ---------------------------------------------------------------------------


def test_web_mode_system_prompt_has_no_workspace_content(tmp_path):
    (tmp_path / "AGENTS.md").write_text("SECRET PROJECT INSTRUCTIONS", encoding="utf-8")
    (tmp_path / "some_file.py").write_text("x = 1\n", encoding="utf-8")
    ws = LocalWorkspace(tmp_path)

    web_prompt = build_system_prompt("role prompt", ws, "web")
    assert "AGENTS.md" not in web_prompt
    assert "SECRET PROJECT INSTRUCTIONS" not in web_prompt
    assert "Workspace contents" not in web_prompt
    assert "some_file.py" not in web_prompt
    assert "WebSearch" in web_prompt
    assert "WebFetch" in web_prompt

    # Contrast: the same workspace under a different mode DOES surface it --
    # confirms the omission above is mode-specific, not an accident of `ws`.
    edit_prompt = build_system_prompt("role prompt", ws, "edit")
    assert "AGENTS.md" in edit_prompt
    assert "SECRET PROJECT INSTRUCTIONS" in edit_prompt


def test_web_mode_system_prompt_works_with_null_workspace(tmp_path):
    ws = NullWorkspace()
    prompt = build_system_prompt("You are a web researcher.", ws, "web")
    assert "You are a web researcher." in prompt
    assert "WebSearch" in prompt


# ---------------------------------------------------------------------------
# engine.py + jobs.py: web keys redacted everywhere
# ---------------------------------------------------------------------------


@respx.mock
async def test_web_key_redacted_from_transcript_and_final_message(tmp_path, monkeypatch):
    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(tool_calls=[make_tool_call("1", "WebSearch", json.dumps({"query": "anything"}))]),
            resp(content=f"Here is what I found: {WEB_KEY}"),
        ]
    )
    from anymodel_subagents import engine

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        run_worker=engine.run_worker,
        client_factory=make_openrouter_client_factory(),
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(leak_in_results=True),
    )
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])
    job_id = created[0].job_id
    assert await _wait_until(lambda: mgr.get(job_id).status == "completed", timeout=5.0)

    job = mgr.get(job_id)
    assert WEB_KEY not in job.result.final_message

    state_dir = tmp_path / "state"
    for path in state_dir.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            assert WEB_KEY not in content, f"leaked web key in {path}"


async def test_web_key_redacted_from_meta_and_ledger_on_error(tmp_path, monkeypatch):
    async def leaky_run_worker(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="error",
            final_message=f"partial result mentioning {WEB_KEY}",
            model=kwargs["model"],
            turns=1,
            usage=Usage(),
            error=f"oops: {WEB_KEY}",
        )

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        run_worker=leaky_run_worker,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])
    job_id = created[0].job_id
    assert await _wait_until(lambda: mgr.get(job_id).status == "error")

    state_dir = tmp_path / "state"
    for path in state_dir.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            assert WEB_KEY not in content, f"leaked web key in {path}"


# ---------------------------------------------------------------------------
# web_calls: counted by engine.py, recorded by ledger.py
# ---------------------------------------------------------------------------


async def test_web_calls_recorded_in_ledger(tmp_path, monkeypatch):
    async def worker_with_web_calls(**kwargs: Any) -> WorkerResult:
        return WorkerResult(
            status="completed",
            final_message="ok",
            model=kwargs["model"],
            turns=1,
            usage=Usage(),
            web_calls=4,
        )

    mgr = make_manager(
        tmp_path,
        monkeypatch,
        run_worker=worker_with_web_calls,
        cfg=make_cfg(web_enabled=True),
        web_client_factory=make_web_client_factory(),
    )
    _swarm_id, created = await mgr.dispatch([jobs.TaskSpec(prompt="p", cwd="", mode="web")])
    job_id = created[0].job_id
    assert await _wait_until(lambda: mgr.get(job_id).status == "completed")

    lines = mgr.ledger_path.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["web_calls"] == 4

    meta = json.loads((tmp_path / "state" / "jobs" / job_id / "meta.json").read_text())
    assert meta["result"]["web_calls"] == 4


@respx.mock
async def test_engine_counts_web_calls_including_a_raising_one(tmp_path):
    from anymodel_subagents.engine import run_worker

    class RaisingWebFetch:
        name = "WebFetch"
        schema: ClassVar[dict[str, Any]] = {
            "type": "function",
            "function": {
                "name": "WebFetch",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        async def run(self, args: dict[str, Any], ws: Any) -> str:
            from anymodel_subagents.types import ToolError

            raise ToolError("fetch failed: blocked domain")

    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(tool_calls=[make_tool_call("1", "WebFetch", json.dumps({"url": "https://x"}))]),
            resp(content="done"),
        ]
    )
    client = OpenRouterClient(OPENROUTER_KEY)
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[RaisingWebFetch()],
            ws=NullWorkspace(),
        )
    finally:
        await client.aclose()

    assert result.web_calls == 1
    assert result.status == "completed"


def test_ledger_summarize_includes_web_calls(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(
        path,
        job_id="j-1",
        swarm_id="s-1",
        role="web-researcher",
        model="m",
        result=WorkerResult(
            status="completed", final_message="", model="m", turns=1, usage=Usage(), web_calls=3
        ),
    )
    ledger.record(
        path,
        job_id="j-2",
        swarm_id="s-1",
        role="web-researcher",
        model="m",
        result=WorkerResult(
            status="completed", final_message="", model="m", turns=1, usage=Usage(), web_calls=2
        ),
    )
    groups = ledger.summarize(path, group_by="role")
    assert groups["web-researcher"]["web_calls"] == 5


async def test_usage_report_totals_include_web_calls(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(
        path,
        job_id="j-1",
        swarm_id="s-1",
        role="web-researcher",
        model="m",
        result=WorkerResult(
            status="completed", final_message="", model="m", turns=1, usage=Usage(), web_calls=5
        ),
    )

    class FakeManagerWithLedger:
        ledger_path = path

    usage_report = server._make_usage_report(FakeManagerWithLedger())
    out = await usage_report(since_days=0)
    assert out["totals"]["web_calls"] == 5


# ---------------------------------------------------------------------------
# server.py: role/description visibility
# ---------------------------------------------------------------------------


def test_visible_roles_hides_web_role_when_disabled():
    web_role = make_role()
    other = make_role("reviewer", mode="read-only", isolation=None)
    roles = {"web-researcher": web_role, "reviewer": other}

    assert set(server._visible_roles(roles, False)) == {"reviewer"}
    assert set(server._visible_roles(roles, True)) == {"web-researcher", "reviewer"}


def test_dispatch_description_hides_and_shows_web_role_and_line():
    web_role = make_role()

    disabled = server._dispatch_description(
        server._visible_roles({"web-researcher": web_role}, False), False
    )
    assert "web-researcher" not in disabled
    assert "Web access is enabled" not in disabled

    enabled = server._dispatch_description(
        server._visible_roles({"web-researcher": web_role}, True), True
    )
    assert "web-researcher" in enabled
    assert "no workspace" in enabled.lower()


async def test_build_server_hides_web_role_when_disabled_and_shows_when_enabled():
    web_role = make_role()
    roles = {"web-researcher": web_role}

    mcp_disabled = server.build_server(MinimalManager(), roles=roles, web_enabled=False)
    tools = await mcp_disabled.list_tools()
    dispatch_tool = next(t for t in tools if t.name == "dispatch")
    assert "web-researcher" not in (dispatch_tool.description or "")

    mcp_enabled = server.build_server(MinimalManager(), roles=roles, web_enabled=True)
    tools2 = await mcp_enabled.list_tools()
    dispatch_tool2 = next(t for t in tools2 if t.name == "dispatch")
    assert "web-researcher" in (dispatch_tool2.description or "")


# ---------------------------------------------------------------------------
# Key isolation: BRAVE_API_KEY/JINA_API_KEY never reach a Bash subprocess env.
#
# tools/bash.py's `_build_env` builds the worker Bash env field by field from
# nothing (never `dict(os.environ)`; see its docstring) -- an allowlist, not a
# denylist -- so this is a straightforward regression test, mirroring
# test_bash.py's own `test_build_env_path_is_built_not_inherited`.
# ---------------------------------------------------------------------------


def test_bash_env_never_includes_web_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-secret-should-not-leak")
    monkeypatch.setenv("JINA_API_KEY", "jina-secret-should-not-leak")

    env = bash_build_env(tmp_path)

    assert "BRAVE_API_KEY" not in env
    assert "JINA_API_KEY" not in env
    joined = " ".join(env.values())
    assert "brave-secret-should-not-leak" not in joined
    assert "jina-secret-should-not-leak" not in joined
