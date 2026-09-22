"""Tests for `run_worker`'s `on_cost` budget-stop hook.

The fakes are deliberately local copies of the ones in tests/test_engine.py (the
engine tests don't depend on the real tools package); keep them in sync if the
policy behavior the engine relies on changes.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import respx

from anymodel_subagents.engine import run_worker
from anymodel_subagents.openrouter import OpenRouterClient
from anymodel_subagents.types import PolicyError

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = "sk-or-v1-budgettestkey00000000000000"


class FakeWorkspace:
    def __init__(self, root: Path, *, denied: set[str] | None = None):
        self.root = Path(root)
        self._denied = denied or set()

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        if path is None or "\x00" in path:
            raise PolicyError("invalid path")
        raw = Path(path)
        candidate = raw if raw.is_absolute() else (self.root / raw)
        real = Path(os.path.realpath(candidate))
        root_real = Path(os.path.realpath(self.root))
        if not real.is_relative_to(root_real):
            raise PolicyError("path escapes workspace root")
        try:
            rel = real.relative_to(root_real).as_posix()
        except ValueError:
            rel = ""
        if rel in self._denied:
            raise PolicyError("access to this file is denied by policy")
        return real

    def is_denied(self, path: Path) -> bool:
        try:
            self.resolve(str(path))
        except (PolicyError, OSError, ValueError):
            return True
        return False

    def is_sensitive(self, path: Path) -> bool:
        return False


class ScriptedTool:
    """A minimal Tool whose `run` behavior is supplied by the test."""

    def __init__(self, name: str, fn):
        self.name = name
        self.schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": f"test tool {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        self._fn = fn

    async def run(self, args: dict[str, Any], ws: Any) -> str:
        return await self._fn(args, ws)


def make_tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def resp(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    usage: dict | None = None,
):
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    choice: dict[str, Any] = {"message": message}
    return httpx.Response(
        200, json={"choices": [choice], "usage": usage if usage is not None else {}}
    )


def make_client() -> OpenRouterClient:
    return OpenRouterClient(API_KEY)


# ---------------------------------------------------------------------------
# on_cost
# ---------------------------------------------------------------------------


@respx.mock
async def test_on_cost_called_once_per_response_with_that_response_cost(tmp_path):
    async def ok(args, ws):
        return "ok"

    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(
                tool_calls=[make_tool_call("1", "Ping", "{}")],
                usage={"prompt_tokens": 10, "cost": 0.001},
            ),
            resp(
                tool_calls=[make_tool_call("2", "Ping", "{}")],
                usage={"prompt_tokens": 20, "cost": 0.002},
            ),
            resp(content="done", usage={"prompt_tokens": 30}),  # no cost in the response
        ]
    )
    costs: list[float] = []
    client = make_client()
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[ScriptedTool("Ping", ok)],
            ws=FakeWorkspace(tmp_path),
            on_cost=costs.append,
        )
    finally:
        await client.aclose()

    assert result.status == "completed"
    assert costs == [0.001, 0.002, 0.0]
    assert len(respx.calls) == 3


@respx.mock
async def test_on_cost_reason_stops_worker_without_executing_tools(tmp_path):
    executed: list[dict[str, Any]] = []

    async def write(args, ws):
        executed.append(args)
        return "ok"

    # The reason text passes through the same redaction any other error text
    # gets, so a secret that sneaks into it must not reach the caller.
    reason = f"monthly budget exhausted: {API_KEY}"
    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(
                tool_calls=[make_tool_call("1", "Write", json.dumps({"file_path": "a.py"}))],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.05},
            ),
        ]
    )
    client = make_client()
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[ScriptedTool("Write", write)],
            ws=FakeWorkspace(tmp_path),
            on_cost=lambda cost: reason,
        )
    finally:
        await client.aclose()

    assert result.status == "budget_exceeded"
    assert "monthly budget exhausted" in result.error
    assert API_KEY not in result.error
    assert "[REDACTED]" in result.error
    assert executed == []
    assert result.tool_calls == 0
    assert result.invalid_tool_calls == 0
    assert result.changed_files == []
    assert result.sensitive_changed_files == []
    assert result.turns == 1
    assert len(respx.calls) == 1
    # Totals still include the response that triggered the stop.
    assert result.usage.requests == 1
    assert result.usage.prompt_tokens == 10
    assert abs(result.usage.cost - 0.05) < 1e-9


@respx.mock
async def test_on_cost_raising_is_swallowed(tmp_path):
    async def ok(args, ws):
        return "ok"

    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(tool_calls=[make_tool_call("1", "Ping", "{}")], usage={"cost": 0.001}),
            resp(content="done", usage={"cost": 0.002}),
        ]
    )

    def broken_on_cost(cost: float) -> None:
        raise RuntimeError("bookkeeping blew up")

    client = make_client()
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[ScriptedTool("Ping", ok)],
            ws=FakeWorkspace(tmp_path),
            on_cost=broken_on_cost,
        )
    finally:
        await client.aclose()

    assert result.status == "completed"
    assert result.final_message == "done"
    assert result.error is None
    assert abs(result.usage.cost - 0.003) < 1e-9
    assert len(respx.calls) == 2


@respx.mock
async def test_on_cost_empty_string_reason_continues(tmp_path):
    async def ok(args, ws):
        return "ok"

    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(tool_calls=[make_tool_call("1", "Ping", "{}")], usage={"cost": 0.001}),
            resp(content="done", usage={"cost": 0.002}),
        ]
    )
    client = make_client()
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[ScriptedTool("Ping", ok)],
            ws=FakeWorkspace(tmp_path),
            on_cost=lambda cost: "",
        )
    finally:
        await client.aclose()

    assert result.status == "completed"
    assert len(respx.calls) == 2


@respx.mock
async def test_on_cost_none_keeps_default_behaviour(tmp_path):
    async def ok(args, ws):
        return "ok"

    respx.post(CHAT_URL).mock(
        side_effect=[
            resp(tool_calls=[make_tool_call("1", "Ping", "{}")], usage={"cost": 0.001}),
            resp(content="done", usage={"cost": 0.002}),
        ]
    )
    client = make_client()
    try:
        result = await run_worker(
            client=client,
            model="m",
            system_prompt="sys",
            task_prompt="task",
            tools=[ScriptedTool("Ping", ok)],
            ws=FakeWorkspace(tmp_path),
            on_cost=None,
        )
    finally:
        await client.aclose()

    assert result.status == "completed"
    assert result.final_message == "done"
    assert result.tool_calls == 1
    assert result.usage.requests == 2
    assert abs(result.usage.cost - 0.003) < 1e-9
    assert len(respx.calls) == 2
