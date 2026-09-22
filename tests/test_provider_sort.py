"""Tests for opt-in OpenRouter provider sort (config.yaml's `provider_sort`).

Covers the wiring in openrouter.py: default (unset) reproduces today's request bodies
byte-for-byte, each allowed value adds `sort` without touching the three ZDR keys, and a
caller-supplied `provider` body still can't override any of it -- with or without a
configured sort. See tests/test_config.py for load_config's own validation of the value.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from anymodel_subagents.openrouter import OpenRouterClient

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = "sk-or-v1-testkey0000000000000000"

_ZDR_KEYS = {"zdr": True, "data_collection": "deny", "require_parameters": True}


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    )


@respx.mock
async def test_default_provider_sort_omits_sort_key():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(API_KEY)
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == _ZDR_KEYS
    assert "sort" not in sent_body["provider"]


@pytest.mark.parametrize("sort_value", ["throughput", "latency", "price"])
@respx.mock
async def test_provider_sort_adds_sort_without_touching_zdr_keys(sort_value):
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(API_KEY, provider_sort=sort_value)
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {**_ZDR_KEYS, "sort": sort_value}


@respx.mock
async def test_caller_provider_body_overridden_with_default_config():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(API_KEY)
    try:
        await client.chat_completions(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"provider": {"sort": "price", "zdr": False, "data_collection": "allow"}},
        )
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == _ZDR_KEYS
    assert "sort" not in sent_body["provider"]


@respx.mock
async def test_caller_provider_body_overridden_with_sort_configured():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(API_KEY, provider_sort="throughput")
    try:
        await client.chat_completions(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"provider": {"sort": "price", "zdr": False, "data_collection": "allow"}},
        )
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {**_ZDR_KEYS, "sort": "throughput"}


@pytest.mark.parametrize("bad_value", ["bogus", "", "PRICE", "Throughput"])
def test_invalid_provider_sort_raises_at_construction(bad_value):
    # Defense in depth: the client validates independently of config.py's load_config,
    # which never lets an invalid value reach here in the first place.
    with pytest.raises(ValueError):
        OpenRouterClient(API_KEY, provider_sort=bad_value)


def test_valid_provider_sort_accepted_at_construction():
    for value in ("throughput", "latency", "price"):
        client = OpenRouterClient(API_KEY, provider_sort=value)
        assert client._provider_prefs["sort"] == value


@respx.mock
async def test_provider_prefs_cannot_drop_zdr_keys_with_sort_configured():
    # Regression: an explicit provider_prefs= (e.g. from a future caller combining it with
    # provider_sort) must still lose to the ZDR keys, no matter what it sets them to.
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(
        API_KEY,
        provider_prefs={"sort": "price", "zdr": False, "data_collection": "allow"},
    )
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {**_ZDR_KEYS, "sort": "price"}


@respx.mock
async def test_retry_body_keeps_zdr_keys_and_sort_across_429_then_200():
    route = respx.post(CHAT_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), _ok_response()]
    )
    client = OpenRouterClient(API_KEY, provider_sort="throughput")
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    assert route.call_count == 2
    bodies = [json.loads(call.request.content)["provider"] for call in route.calls]
    assert bodies == [{**_ZDR_KEYS, "sort": "throughput"}] * 2


async def test_body_provider_is_not_the_clients_live_prefs_dict():
    # Regression: body["provider"] must be a fresh dict each request, never an alias of the
    # client's own `_provider_prefs` -- a caller mutating the returned body (or another request
    # reusing it) must not be able to poison every later request from this client.
    client = OpenRouterClient(API_KEY, provider_sort="price")
    captured: dict = {}

    async def spy(body):
        captured["body"] = body
        return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

    client._post_with_retries = spy  # type: ignore[method-assign]

    await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    assert captured["body"]["provider"] is not client._provider_prefs
    assert captured["body"]["provider"] == client._provider_prefs


def test_server_client_factory_passes_the_configured_sort(monkeypatch):
    """Wiring: config.yaml's provider_sort must reach the client the server builds."""
    from anymodel_subagents import server
    from anymodel_subagents.config import Config

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")
    client = server._default_client_factory(Config(provider_sort="throughput"))()
    assert client._provider_prefs == {
        "sort": "throughput",
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    default = server._default_client_factory(Config())()
    assert default._provider_prefs == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
