"""Tests for anymodel_subagents.models.list_zdr_tool_models.

Uses respx to mock OpenRouter's public `/endpoints/zdr` listing. The exact
response shape isn't publicly pinned down anywhere in this repo, so the
fixture below follows OpenRouter's documented `/models`-family convention
(a top-level `data` list of models, each carrying a list of provider
`endpoints`) extended with the `endpoints`-specific fields this module reads:
`supported_parameters`, `pricing` (strings, USD per token), `context_length`,
and `supports_implicit_caching`.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anymodel_subagents import models

ZDR_URL = "https://openrouter.ai/api/v1/endpoints/zdr"


@pytest.fixture(autouse=True)
def _reset_model_cache():
    models._reset_cache()
    yield
    models._reset_cache()


def _endpoint(
    *,
    prompt: str,
    completion: str,
    context_length: int = 64_000,
    tools: bool = True,
    implicit_caching: bool = False,
) -> dict:
    supported = ["temperature", "top_p"]
    if tools:
        supported.append("tools")
    return {
        "provider_name": "some-provider",
        "pricing": {"prompt": prompt, "completion": completion},
        "context_length": context_length,
        "supported_parameters": supported,
        "supports_implicit_caching": implicit_caching,
    }


FIXTURE = {
    "data": [
        {
            "id": "deepseek/deepseek-v4.1-flash",
            "name": "DeepSeek V4.1 Flash",
            "endpoints": [
                _endpoint(prompt="0.00000027", completion="0.0000011", context_length=64_000),
                _endpoint(
                    prompt="0.0000002",
                    completion="0.0000009",
                    context_length=128_000,
                    implicit_caching=True,
                ),
            ],
        },
        {
            "id": "some-vendor/no-tools-model",
            "name": "No Tools Model",
            "endpoints": [
                _endpoint(prompt="0.0000001", completion="0.0000002", tools=False),
            ],
        },
        {
            "id": "some-vendor/expensive-model",
            "name": "Expensive Model",
            "endpoints": [
                _endpoint(prompt="0.00003", completion="0.00006", context_length=200_000),
            ],
        },
        {
            "id": "some-vendor/mixed-endpoints",
            "name": "Mixed Endpoints",
            "endpoints": [
                _endpoint(prompt="0.000001", completion="0.000002", tools=False),
                _endpoint(prompt="0.000005", completion="0.00001", tools=True),
            ],
        },
    ]
}


# ---------------------------------------------------------------------------
# grouping / filtering
# ---------------------------------------------------------------------------


@respx.mock
async def test_keeps_only_tool_capable_models():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    ids = {m["model_id"] for m in result}
    assert "some-vendor/no-tools-model" not in ids
    assert "deepseek/deepseek-v4.1-flash" in ids
    assert "some-vendor/expensive-model" in ids
    assert "some-vendor/mixed-endpoints" in ids


@respx.mock
async def test_mixed_endpoints_model_only_counts_tool_capable_ones():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    mixed = next(m for m in result if m["model_id"] == "some-vendor/mixed-endpoints")
    # Only the second endpoint (tools=True) counts.
    assert mixed["zdr_provider_count"] == 1
    assert mixed["prompt_price_per_m"] == pytest.approx(5.0)


@respx.mock
async def test_reports_cheapest_price_and_context_and_caching():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    deepseek = next(m for m in result if m["model_id"] == "deepseek/deepseek-v4.1-flash")
    # Cheapest prompt price across its two tool-capable endpoints, in USD/1M tokens.
    assert deepseek["prompt_price_per_m"] == pytest.approx(0.2)
    assert deepseek["completion_price_per_m"] == pytest.approx(0.9)
    assert deepseek["zdr_provider_count"] == 2
    assert deepseek["context_length"] == 128_000
    assert deepseek["supports_implicit_caching"] is True


@respx.mock
async def test_model_with_no_implicit_caching_endpoint_reports_false():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    expensive = next(m for m in result if m["model_id"] == "some-vendor/expensive-model")
    assert expensive["supports_implicit_caching"] is False


@respx.mock
async def test_results_sorted_cheapest_first():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    prices = [m["prompt_price_per_m"] for m in result]
    assert prices == sorted(prices)


@respx.mock
async def test_max_input_price_filter_excludes_expensive_models():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http, max_input_price_per_m=1.0)

    ids = {m["model_id"] for m in result}
    assert "deepseek/deepseek-v4.1-flash" in ids
    assert "some-vendor/expensive-model" not in ids
    assert "some-vendor/mixed-endpoints" not in ids  # its only tool endpoint is $5/M


# ---------------------------------------------------------------------------
# malformed entries
# ---------------------------------------------------------------------------


@respx.mock
async def test_endpoint_without_prompt_price_is_dropped():
    """_price_per_million(None): the tools
    endpoint contributes no prompt price, so the model is skipped."""
    fixture = {
        "data": [
            {
                "id": "vendor/no-prompt-price",
                "name": "No Prompt Price",
                "endpoints": [
                    {
                        "pricing": {"completion": "0.000002"},
                        "context_length": 32_000,
                        "supported_parameters": ["tools"],
                    }
                ],
            },
            {
                "id": "vendor/valid",
                "name": "Valid",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
        ]
    }
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=fixture))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert [m["model_id"] for m in result] == ["vendor/valid"]


@respx.mock
async def test_endpoint_with_unparseable_price_is_dropped():
    """a non-numeric string (ValueError) and a non-numeric
    type (TypeError) both convert to None instead of raising."""
    fixture = {
        "data": [
            {
                "id": "vendor/string-price",
                "name": "String Price",
                "endpoints": [
                    {
                        "pricing": {"prompt": "free", "completion": "free"},
                        "supported_parameters": ["tools"],
                    }
                ],
            },
            {
                "id": "vendor/wrong-type-price",
                "name": "Wrong Type Price",
                "endpoints": [
                    {
                        "pricing": {"prompt": ["0.000001"], "completion": {"amount": 1}},
                        "supported_parameters": ["tools"],
                    }
                ],
            },
            {
                "id": "vendor/valid",
                "name": "Valid",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
        ]
    }
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=fixture))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert [m["model_id"] for m in result] == ["vendor/valid"]


@respx.mock
async def test_non_dict_model_entry_is_skipped():
    """a `data` list containing non-object entries."""
    fixture = {
        "data": [
            "not-a-model",
            None,
            42,
            {
                "id": "vendor/valid",
                "name": "Valid",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
        ]
    }
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=fixture))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert [m["model_id"] for m in result] == ["vendor/valid"]


@respx.mock
async def test_model_entry_without_string_id_is_skipped():
    """an `id` that is neither a string nor fallable to a
    string `slug`."""
    fixture = {
        "data": [
            {
                "id": 123,
                "name": "Numeric Id",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
            {
                "id": None,
                "name": "No Id Or Slug",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
            {
                "id": "vendor/valid",
                "name": "Valid",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
        ]
    }
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=fixture))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert [m["model_id"] for m in result] == ["vendor/valid"]


@respx.mock
async def test_model_entry_with_non_list_endpoints_is_skipped():
    """`endpoints` present but not a list, or absent entirely."""
    fixture = {
        "data": [
            {"id": "vendor/endpoints-string", "name": "Bad", "endpoints": "nope"},
            {"id": "vendor/endpoints-missing", "name": "Missing"},
            {
                "id": "vendor/valid",
                "name": "Valid",
                "endpoints": [_endpoint(prompt="0.000001", completion="0.000002")],
            },
        ]
    }
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=fixture))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert [m["model_id"] for m in result] == ["vendor/valid"]


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------


@respx.mock
async def test_second_call_uses_cache_not_a_second_request():
    route = respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        await models.list_zdr_tool_models(http)
        await models.list_zdr_tool_models(http)

    assert route.call_count == 1


@respx.mock
async def test_cache_expiry_triggers_a_second_request(monkeypatch):
    route = respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        await models.list_zdr_tool_models(http)

        real_time = models.time.monotonic
        monkeypatch.setattr(models.time, "monotonic", lambda: real_time() + models._CACHE_TTL_S + 1)
        await models.list_zdr_tool_models(http)

    assert route.call_count == 2


# ---------------------------------------------------------------------------
# error path
# ---------------------------------------------------------------------------


@respx.mock
async def test_timeout_returns_error_entry_instead_of_raising():
    respx.get(ZDR_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "error" in result[0]


@respx.mock
async def test_http_error_status_returns_error_entry():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "error" in result[0]


@respx.mock
async def test_malformed_json_returns_error_entry():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "error" in result[0]


@respx.mock
async def test_unexpected_top_level_shape_returns_error_entry():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "error" in result[0]


@respx.mock
async def test_missing_data_key_returns_empty_list_not_error():
    respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json={"unexpected": []}))
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert result == []


async def test_unexpected_fetch_error_returns_error_entry(monkeypatch):
    """a fetch failure that is neither an httpx.HTTPError
    nor a ValueError is still reported, never raised."""

    async def _boom(http, base_url):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(models, "_fetch_payload", _boom)
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "unexpected error fetching ZDR endpoints: kaboom" in result[0]["error"]


async def test_unexpected_parse_error_returns_error_entry(monkeypatch):
    """a payload that makes _group_by_model raise must not
    crash the caller."""

    async def _payload(http, base_url):
        return {"data": []}

    def _boom(payload, *, max_input_price_per_m):
        raise RuntimeError("bad payload")

    monkeypatch.setattr(models, "_fetch_payload", _payload)
    monkeypatch.setattr(models, "_group_by_model", _boom)
    async with httpx.AsyncClient() as http:
        result = await models.list_zdr_tool_models(http)

    assert len(result) == 1
    assert "unexpected error parsing ZDR endpoints: bad payload" in result[0]["error"]


@respx.mock
async def test_does_not_send_authorization_header():
    route = respx.get(ZDR_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        await models.list_zdr_tool_models(http)

    sent_headers = route.calls[0].request.headers
    assert "authorization" not in {k.lower() for k in sent_headers}
