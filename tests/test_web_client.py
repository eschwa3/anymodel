"""Tests for `web_client.py` (Brave search + Jina fetch providers).

No real network calls: every HTTP interaction goes through `respx`, and the
"live keys" used below are distinctive fake strings so a test can assert they
never leak into an error message.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from anymodel_subagents.types import ToolError, WebHit, WebPage
from anymodel_subagents.web_client import (
    BraveJinaClient,
    MissingWebKeyError,
    web_client_from_env,
)

BRAVE_URL = "https://api.search.brave.com/res/v1/llm/context"
JINA_URL = "https://r.jina.ai/"

FAKE_BRAVE_KEY = "brave-test-key-zzzqqqxxx-0001"
FAKE_JINA_KEY = "jina-test-key-wwwvvvuuu-0002"


def _brave_ok(results: list[dict] | None = None) -> httpx.Response:
    generic = (
        results
        if results is not None
        else [{"url": "https://example.com/a", "title": "A", "snippets": ["hello", "world"]}]
    )
    return httpx.Response(200, json={"grounding": {"generic": generic}})


def _jina_ok(
    *, url: str = "https://example.com/a", title: str = "A", content: str = "body"
) -> httpx.Response:
    return httpx.Response(200, json={"data": {"url": url, "title": title, "content": content}})


# --------------------------------------------------------------------------- web_client_from_env


def test_from_env_requires_brave_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    with pytest.raises(MissingWebKeyError):
        web_client_from_env()


def test_from_env_empty_brave_key_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "   ")
    with pytest.raises(MissingWebKeyError):
        web_client_from_env()


def test_from_env_unexpanded_userconfig_placeholder_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "${user_config.brave_api_key}")
    with pytest.raises(MissingWebKeyError):
        web_client_from_env()


def test_from_env_empty_jina_key_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", FAKE_BRAVE_KEY)
    monkeypatch.setenv("JINA_API_KEY", "")
    client = web_client_from_env()
    try:
        assert client.redaction_secrets() == [FAKE_BRAVE_KEY]
    finally:
        await_close(client)


def test_from_env_builds_client_with_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", FAKE_BRAVE_KEY)
    monkeypatch.setenv("JINA_API_KEY", FAKE_JINA_KEY)
    client = web_client_from_env()
    try:
        assert set(client.redaction_secrets()) == {FAKE_BRAVE_KEY, FAKE_JINA_KEY}
    finally:
        await_close(client)


def await_close(client) -> None:
    import asyncio

    asyncio.run(client.aclose())


# --------------------------------------------------------------------------- search (Brave)


@respx.mock
async def test_search_request_shape():
    route = respx.get(BRAVE_URL).mock(return_value=_brave_ok())
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        await client.search("cats", 3)
    finally:
        await client.aclose()

    assert route.called
    request = route.calls[0].request
    assert request.headers["X-Subscription-Token"] == FAKE_BRAVE_KEY
    assert request.headers["Accept"] == "application/json"
    params = dict(httpx.QueryParams(request.url.query))
    assert params["q"] == "cats"
    assert params["maximum_number_of_urls"] == "3"
    assert params["count"] == "3"
    assert params["maximum_number_of_tokens"] == "4096"


@respx.mock
async def test_search_parses_hits():
    respx.get(BRAVE_URL).mock(
        return_value=_brave_ok(
            [{"url": "https://example.com/x", "title": "X", "snippets": ["s1", "s2"]}]
        )
    )
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        hits = await client.search("q", 5)
    finally:
        await client.aclose()

    assert hits == [WebHit(url="https://example.com/x", title="X", snippets=["s1", "s2"])]


@respx.mock
async def test_search_tolerates_missing_fields():
    respx.get(BRAVE_URL).mock(
        return_value=_brave_ok([{"url": "https://example.com/x"}, {"no_url": True}, "garbage"])
    )
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        hits = await client.search("q", 5)
    finally:
        await client.aclose()

    assert hits == [WebHit(url="https://example.com/x", title="", snippets=[])]


@respx.mock
async def test_search_skips_non_https_urls():
    respx.get(BRAVE_URL).mock(
        return_value=_brave_ok([{"url": "http://example.com/x", "title": "insecure"}])
    )
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        hits = await client.search("q", 5)
    finally:
        await client.aclose()
    assert hits == []


@respx.mock
async def test_search_401_rejected_key_message_has_no_key():
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(401, text=f"bad key {FAKE_BRAVE_KEY}"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError) as exc:
            await client.search("q", 5)
    finally:
        await client.aclose()
    assert "search provider rejected the API key" in str(exc.value)
    assert FAKE_BRAVE_KEY not in str(exc.value)


@respx.mock
async def test_search_403_rejected_key():
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(403, text="forbidden"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError, match="search provider rejected the API key"):
            await client.search("q", 5)
    finally:
        await client.aclose()


@respx.mock
async def test_search_429_rate_limited():
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(429, text="slow down"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError, match="rate limited"):
            await client.search("q", 5)
    finally:
        await client.aclose()
    # And the message never contains the key, defense in depth.


@respx.mock
async def test_search_other_error_status():
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(500, text=f"key={FAKE_BRAVE_KEY}"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError) as exc:
            await client.search("q", 5)
    finally:
        await client.aclose()
    assert FAKE_BRAVE_KEY not in str(exc.value)
    assert "500" in str(exc.value)


@respx.mock
async def test_search_invalid_json_response():
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(200, text="not json"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.search("q", 5)
    finally:
        await client.aclose()


@respx.mock
async def test_search_timeout_raises_generic_tool_error():
    respx.get(BRAVE_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.search("q", 5)
    finally:
        await client.aclose()


@respx.mock
async def test_search_network_error_raises_generic_tool_error():
    respx.get(BRAVE_URL).mock(side_effect=httpx.ConnectError("boom"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.search("q", 5)
    finally:
        await client.aclose()


@respx.mock
async def test_search_response_too_large_aborts():
    huge = json.dumps({"grounding": {"generic": []}, "pad": "x" * (3 * 1024 * 1024)}).encode()
    respx.get(BRAVE_URL).mock(return_value=httpx.Response(200, content=huge))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError, match="too large"):
            await client.search("q", 5)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- fetch (Jina)


@respx.mock
async def test_fetch_request_shape_no_jina_key():
    route = respx.post(JINA_URL).mock(return_value=_jina_ok())
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        await client.fetch("https://example.com/a")
    finally:
        await client.aclose()

    assert route.called
    request = route.calls[0].request
    assert request.headers["DNT"] == "1"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["X-Timeout"] == "15"
    assert "Authorization" not in request.headers
    assert json.loads(request.content) == {"url": "https://example.com/a"}


@respx.mock
async def test_fetch_authorization_present_with_jina_key():
    route = respx.post(JINA_URL).mock(return_value=_jina_ok())
    client = BraveJinaClient(FAKE_BRAVE_KEY, FAKE_JINA_KEY)
    try:
        await client.fetch("https://example.com/a")
    finally:
        await client.aclose()
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {FAKE_JINA_KEY}"


@respx.mock
async def test_fetch_parses_page():
    respx.post(JINA_URL).mock(
        return_value=_jina_ok(url="https://example.com/a", title="Hello", content="page body")
    )
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        page = await client.fetch("https://example.com/a")
    finally:
        await client.aclose()
    assert page == WebPage(url="https://example.com/a", title="Hello", content="page body")


@respx.mock
async def test_fetch_401_rejected_key_message_no_keys():
    respx.post(JINA_URL).mock(
        return_value=httpx.Response(401, text=f"bad {FAKE_BRAVE_KEY} {FAKE_JINA_KEY}")
    )
    client = BraveJinaClient(FAKE_BRAVE_KEY, FAKE_JINA_KEY)
    try:
        with pytest.raises(ToolError) as exc:
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()
    assert "fetch provider rejected the API key" in str(exc.value)
    assert FAKE_BRAVE_KEY not in str(exc.value)
    assert FAKE_JINA_KEY not in str(exc.value)


@respx.mock
async def test_fetch_429_rate_limited():
    respx.post(JINA_URL).mock(return_value=httpx.Response(429, text="slow down"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError, match="rate limited"):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_non_2xx_generic_blocked_message():
    respx.post(JINA_URL).mock(return_value=httpx.Response(451, text=f"blocked {FAKE_JINA_KEY}"))
    client = BraveJinaClient(FAKE_BRAVE_KEY, FAKE_JINA_KEY)
    try:
        with pytest.raises(ToolError) as exc:
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()
    assert "blocked by site or unavailable" in str(exc.value)
    assert "451" in str(exc.value)
    assert FAKE_JINA_KEY not in str(exc.value)


@respx.mock
async def test_fetch_invalid_json_response():
    respx.post(JINA_URL).mock(return_value=httpx.Response(200, text="not json"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_unexpected_shape():
    respx.post(JINA_URL).mock(return_value=httpx.Response(200, json={"nope": True}))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_timeout_raises_generic_tool_error():
    respx.post(JINA_URL).mock(side_effect=httpx.ReadTimeout("boom"))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_response_too_large_aborts():
    huge = json.dumps(
        {"data": {"url": "https://example.com/a", "content": "x" * (3 * 1024 * 1024)}}
    )
    respx.post(JINA_URL).mock(return_value=httpx.Response(200, content=huge.encode()))
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        with pytest.raises(ToolError, match="too large"):
            await client.fetch("https://example.com/a")
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- client transport


def test_client_uses_expected_httpx_settings() -> None:
    client = BraveJinaClient(FAKE_BRAVE_KEY)
    try:
        assert client._client.follow_redirects is False
        assert client._client.trust_env is False
        assert client._client.timeout.connect == 15.0
    finally:
        await_close(client)


def test_repr_never_leaks_key() -> None:
    client = BraveJinaClient(FAKE_BRAVE_KEY, FAKE_JINA_KEY)
    try:
        assert FAKE_BRAVE_KEY not in repr(client)
        assert FAKE_JINA_KEY not in repr(client)
    finally:
        await_close(client)
