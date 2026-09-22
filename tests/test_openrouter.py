import json
import re
import time

import httpx
import pytest
import respx

from anymodel_subagents.openrouter import (
    _BACKOFF_MAX_S,
    OpenRouterClient,
    OpenRouterError,
    _clean_provider_text,
    _retry_after_seconds,
)

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def _ok_response(content: str = "hi", usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage if usage is not None else {},
        },
    )


@respx.mock
async def test_zdr_provider_prefs_present_in_every_request():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="test/model", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()

    assert route.called
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert result.message["content"] == "hi"


@respx.mock
async def test_provider_prefs_override():
    # provider_prefs= is a caller escape hatch (e.g. adding non-ZDR keys), but it can never
    # drop or alter the ZDR keys -- they're applied after, unconditionally.
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000", provider_prefs={"zdr": True})
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }


@respx.mock
async def test_provider_prefs_cannot_disable_zdr():
    # Regression: provider_prefs={"zdr": False} used to be sent as-is (invariant 2 violation).
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000", provider_prefs={"zdr": False})
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }


@respx.mock
async def test_retry_on_429_then_success():
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"}),
            _ok_response("recovered"),
        ]
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()

    assert route.call_count == 2
    assert result.message["content"] == "recovered"


@respx.mock
async def test_retries_exhausted_raises():
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        with pytest.raises(OpenRouterError):
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    # initial attempt + 4 retries = 5 total
    assert route.call_count == 5


@respx.mock
async def test_http_200_with_error_body_raises():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "invalid request", "code": 400}}
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        with pytest.raises(OpenRouterError) as excinfo:
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert "invalid request" in str(excinfo.value)


@respx.mock
async def test_usage_parsed_with_missing_fields():
    respx.post(CHAT_URL).mock(return_value=_ok_response("ok", usage={"prompt_tokens": 10}))
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 0
    assert result.usage.cost == 0.0
    assert result.usage.cached_tokens == 0
    assert result.usage.reasoning_tokens == 0
    assert result.usage.requests == 1


@respx.mock
async def test_usage_parsed_with_nested_details_present():
    respx.post(CHAT_URL).mock(
        return_value=_ok_response(
            "ok",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": 0.0032,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.usage.cached_tokens == 40
    assert result.usage.reasoning_tokens == 5
    assert result.usage.cost == 0.0032


@respx.mock
async def test_usage_parsed_with_none_details():
    respx.post(CHAT_URL).mock(
        return_value=_ok_response(
            "ok",
            usage={
                "prompt_tokens": None,
                "completion_tokens": None,
                "cost": None,
                "prompt_tokens_details": None,
                "completion_tokens_details": None,
            },
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.usage.prompt_tokens == 0
    assert result.usage.completion_tokens == 0
    assert result.usage.cost == 0.0


@respx.mock
async def test_api_key_never_appears_in_exception_text():
    api_key = "sk-or-v1-supersecretvalue1234567890"
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, text=f"bad request near key {api_key}")
    )
    # respx echoing the key back in the body would be unusual, but even so the client's own
    # exception construction/message must never *add* the key -- and repr() must never show it.
    client = OpenRouterClient(api_key)
    try:
        with pytest.raises(OpenRouterError):
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert api_key not in repr(client)
    assert api_key not in str(client)


@respx.mock
async def test_client_repr_never_leaks_key():
    api_key = "sk-or-v1-anothersecret0987654321"
    client = OpenRouterClient(api_key)
    assert api_key not in repr(client)
    await client.aclose()


def test_retry_after_clamped_to_backoff_max():
    resp = httpx.Response(429, headers={"Retry-After": "9999"})
    assert _retry_after_seconds(resp) == _BACKOFF_MAX_S


def test_retry_after_under_max_is_unclamped():
    resp = httpx.Response(429, headers={"Retry-After": "2"})
    assert _retry_after_seconds(resp) == 2.0


def test_retry_after_negative_clamped_to_zero():
    resp = httpx.Response(429, headers={"Retry-After": "-5"})
    assert _retry_after_seconds(resp) == 0.0


@respx.mock
async def test_extra_body_cannot_override_protected_fields():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        await client.chat_completions(
            model="real-model",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={
                "model": "evil-model",
                "messages": [{"role": "user", "content": "evil"}],
                "provider": {"zdr": False},
                "temperature": 0.2,
            },
        )
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["model"] == "real-model"
    assert sent_body["messages"] == [{"role": "user", "content": "hi"}]
    assert sent_body["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    # Non-protected fields from extra_body still pass through.
    assert sent_body["temperature"] == 0.2


@respx.mock
async def test_extra_body_cannot_inject_fake_tools():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        await client.chat_completions(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"tools": [{"type": "function", "function": {"name": "evil"}}]},
        )
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert "tools" not in sent_body


@respx.mock
async def test_extra_body_provider_prefs_from_constructor_still_win():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient(
        "sk-or-v1-testkey0000000000000000", provider_prefs={"zdr": True, "custom": 1}
    )
    try:
        await client.chat_completions(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"provider": {"zdr": False}},
        )
    finally:
        await client.aclose()

    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["provider"] == {
        "zdr": True,
        "custom": 1,
        "data_collection": "deny",
        "require_parameters": True,
    }


@respx.mock
async def test_headers_include_referer_and_title():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    sent_headers = route.calls[0].request.headers
    assert sent_headers["X-Title"] == "anymodel-subagents"
    assert "HTTP-Referer" in sent_headers
    assert sent_headers["Authorization"] == "Bearer sk-or-v1-testkey0000000000000000"


@respx.mock
async def test_http_error_includes_provider_reason():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            402,
            json={
                "error": {
                    "code": 402,
                    "message": "This request requires more credits,\nor fewer max_tokens.",
                }
            },
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey")
    try:
        with pytest.raises(OpenRouterError) as exc:
            await client.chat_completions(model="m/x", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert "HTTP 402: This request requires more credits, or fewer max_tokens." in str(exc.value)
    assert "sk-or-v1-testkey" not in str(exc.value)


@respx.mock
async def test_max_tokens_is_bounded_by_default():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey")
    try:
        await client.chat_completions(model="m/x", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert json.loads(route.calls.last.request.content)["max_tokens"] == 16_384


# --------------------------------------------------------------------------
# finish_reason
# --------------------------------------------------------------------------


@respx.mock
async def test_finish_reason_present_is_surfaced():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "length"}
                ]
            },
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.finish_reason == "length"


@respx.mock
async def test_finish_reason_missing_is_none():
    respx.post(CHAT_URL).mock(return_value=_ok_response("hi"))
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.finish_reason is None


@respx.mock
async def test_finish_reason_non_str_is_none():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": 123}
                ]
            },
        )
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        result = await client.chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.aclose()
    assert result.finish_reason is None


# --------------------------------------------------------------------------
# max_output_tokens: default vs. override, validation
# --------------------------------------------------------------------------


@respx.mock
async def test_extra_body_max_tokens_wins_over_default():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        await client.chat_completions(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"max_tokens": 42},
        )
    finally:
        await client.aclose()
    assert json.loads(route.calls.last.request.content)["max_tokens"] == 42


@respx.mock
async def test_max_output_tokens_none_omits_field():
    route = respx.post(CHAT_URL).mock(return_value=_ok_response())
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000", max_output_tokens=None)
    try:
        await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert "max_tokens" not in json.loads(route.calls.last.request.content)


@pytest.mark.parametrize("bad_value", [0, -1, True, False, 1.5, "16384"])
def test_max_output_tokens_invalid_raises(bad_value):
    with pytest.raises(ValueError):
        OpenRouterClient("sk-or-v1-testkey0000000000000000", max_output_tokens=bad_value)


def test_max_output_tokens_valid_int_accepted():
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000", max_output_tokens=1)
    assert client._max_output_tokens == 1


# --------------------------------------------------------------------------
# _error_detail hygiene: key scrubbing, control chars, 200-with-error path
# --------------------------------------------------------------------------


@respx.mock
async def test_json_error_body_echoing_key_is_scrubbed_from_exception():
    api_key = "sk-or-v1-supersecretvalue1234567890"
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": f"bad key {api_key} rejected"}})
    )
    client = OpenRouterClient(api_key)
    try:
        with pytest.raises(OpenRouterError) as excinfo:
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert api_key not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


@respx.mock
async def test_all_control_char_message_has_no_dangling_separator():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "\x00\x01\x02"}})
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        with pytest.raises(OpenRouterError) as excinfo:
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    message = str(excinfo.value)
    assert not message.endswith(": ")
    assert message == "OpenRouter returned HTTP 400"


@respx.mock
async def test_200_with_error_path_is_bounded_single_line_and_scrubbed():
    api_key = "sk-or-v1-supersecretvalue1234567890"
    long_msg = f"denied {api_key}\n" + ("line one\nline two " * 50)
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"error": {"message": long_msg}})
    )
    client = OpenRouterClient(api_key)
    try:
        with pytest.raises(OpenRouterError) as excinfo:
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    message = str(excinfo.value)
    assert api_key not in message
    assert "[REDACTED]" in message  # the scrub ran; the key was not merely cut off by the bound
    assert "\n" not in message
    assert len(message) < 350


@respx.mock
async def test_200_with_error_path_all_control_chars_no_dangling_separator():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"error": {"message": "\x00\x01\x02"}})
    )
    client = OpenRouterClient("sk-or-v1-testkey0000000000000000")
    try:
        with pytest.raises(OpenRouterError) as excinfo:
            await client.chat_completions(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert str(excinfo.value) == "OpenRouter error"


@pytest.mark.parametrize("noise", ["\x01", "\n", "\u200b", "\ufe00", "\u0301", "\u3164", " "])
def test_provider_text_with_key_split_by_one_character_is_withheld(noise):
    # `redact` can't match a key with one character inserted, but a reader reassembles it
    # trivially -- the client must not rely on the provider's good behaviour.
    api_key = "sk-or-v1-decoy000000000000000000"
    split = api_key[:13] + noise + api_key[13:]
    cleaned = _clean_provider_text(f"upstream rejected credential {split} (invalid)", [api_key])
    assert api_key not in re.sub(r"[^\x21-\x7e]", "", cleaned)
    assert "withheld" in cleaned


def test_provider_text_without_a_key_is_not_withheld():
    cleaned = _clean_provider_text("This request requires more credits,\nor fewer max_tokens.", [])
    assert cleaned == "This request requires more credits, or fewer max_tokens."


def test_provider_text_is_capped_before_scrubbing():
    # Many PEM BEGIN markers and no END: quadratic in an unbounded scrub, and this runs on
    # the event loop. 2 MB took ~10 s before the cap.
    hostile = ("A" * 1000 + " -----BEGIN RSA PRIVATE KEY----- ") * 2000
    start = time.monotonic()
    _clean_provider_text(hostile, ["sk-or-v1-decoy000000000000000000"])
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize("sep", [".", '"', "|", "/", "-"])
def test_provider_text_with_key_split_by_visible_punctuation_is_withheld(sep):
    api_key = "sk-or-v1-decoy000000000000000000"
    split = api_key[:13] + sep + api_key[13:]
    cleaned = _clean_provider_text(f"bad credential {split}", [api_key])
    # Withheld, or (for "-") redacted by a generic pattern: either way not reassemblable.
    assert re.sub(r"[^A-Za-z0-9]", "", api_key) not in re.sub(r"[^A-Za-z0-9]", "", cleaned)


def test_provider_text_key_prefix_left_by_the_scan_cap_is_withheld():
    # Padding that strips away moves a key cut off by the 4096-char scan cap to the front.
    api_key = "sk-or-v1-" + "a1b2c3d4" * 8
    text = "\x01" * (4096 - 24) + api_key
    cleaned = _clean_provider_text(text, [api_key])
    assert api_key[:20] not in cleaned


@pytest.mark.parametrize(
    "message",
    [
        "This request requires more credits, or fewer max_tokens. You requested up to 16384.",
        "No endpoints found that support the provided parameters (zdr, tools).",
        "Rate limit exceeded: free-models-per-min. Retry after 12s (req_01J9ZK3Q8W-7f2c-4d1e).",
        "Provider returned error: context_length_exceeded (131072 tokens max, got 140233).",
        "z-ai/glm-5.3-flash is not a valid model ID",
    ],
)
def test_realistic_provider_errors_are_not_withheld(message):
    assert _clean_provider_text(message, ["sk-or-v1-decoy000000000000000000"]) == message
