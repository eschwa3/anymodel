"""Async client for the OpenRouter chat-completions API.

Non-streaming only. Every request is routed through OpenRouter's ZDR (zero data
retention) provider preferences by default -- see `DEFAULT_PROVIDER_PREFS`.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from anymodel_subagents.redact import redact
from anymodel_subagents.types import Usage

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_PROVIDER_PREFS: dict[str, Any] = {
    "zdr": True,
    "data_collection": "deny",
    "require_parameters": True,
}

# Opt-in OpenRouter provider sort (config.yaml's `provider_sort`; see config.py). An explicit
# sort turns off OpenRouter's default price-weighted load balancing among ZDR-eligible
# providers, so it can pick a pricier one -- off (None) reproduces today's request bodies
# byte-for-byte.
_VALID_PROVIDER_SORT = ("throughput", "latency", "price")

_MAX_RETRIES = 4
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 20.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenRouterError(Exception):
    """Raised for any request/response failure. Never contains the API key."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        # Body is truncated defensively -- it's provider-controlled text, not ours to trust.
        self.body = (body or "")[:2000]


_PROVIDER_TEXT_SCAN_CAP = 4096
_NOT_VISIBLE_ASCII = re.compile(r"[^\x21-\x7e]")
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
_WITHHELD = "[provider message withheld: it contained key-shaped text]"


def _clean_provider_text(text: Any, secrets: list[str] | None) -> str:
    """Bound, single-line, printable, secret-scrubbed rendering of provider-controlled text.

    Cleans (scrub -> printable-only -> strip) *before* checking for emptiness, so a message
    that's all non-printable/whitespace after cleaning collapses to "" rather than leaving a
    dangling separator at the call site. The API key is never echoed by OpenRouter, but this
    scrubs it anyway -- the client must not rely on the provider's good behavior.
    """
    if not isinstance(text, str):
        return ""
    # Cap BEFORE scrubbing: this runs synchronously on the event loop, on text whose size
    # the provider controls.
    scrubbed = redact(text[:_PROVIDER_TEXT_SCAN_CAP], secrets)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in scrubbed).strip()
    # A key split by one control or invisible-but-printable character slips past `redact`
    # and is trivially reassembled by a reader. Re-check with everything but visible ASCII
    # removed; if a secret appears only then, the text is hostile -- withhold it entirely.
    denoised = _NOT_VISIBLE_ASCII.sub("", cleaned)
    if redact(denoised, secrets) != denoised:
        return _WITHHELD
    # Same for a live key split by visible punctuation (`sk-or-v1-dec.oy...`), and for a key
    # cut short by the scan cap: compare letters and digits only.
    alnum = _NOT_ALNUM.sub("", cleaned)
    for secret in secrets or []:
        bare = _NOT_ALNUM.sub("", secret)
        if len(bare) >= 16 and (bare in alnum or alnum.endswith(bare[:16])):
            return _WITHHELD
    return cleaned[:300]


def _format_detail(cleaned: str) -> str:
    return f": {cleaned}" if cleaned else ""


def _error_detail(body: str, secrets: list[str] | None = None) -> str:
    """The provider's own explanation, e.g. why a 402 was returned. Untrusted text -- see
    `_clean_provider_text`. Returns "" or a leading ": " + the cleaned text."""
    try:
        err = json.loads(body).get("error")
        detail = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        detail = None
    return _format_detail(_clean_provider_text(detail, secrets))


@dataclass
class ChatResult:
    """One non-streaming chat-completion response."""

    message: dict[str, Any]  # the assistant message: role/content/tool_calls
    usage: Usage
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None


def _parse_usage(usage: dict[str, Any] | None) -> Usage:
    """Defensively parse an OpenRouter `usage` block. Fields may be missing or None."""
    usage = usage or {}

    def _num(value: Any, default: float = 0) -> Any:
        return value if isinstance(value, (int, float)) else default

    prompt_tokens = int(_num(usage.get("prompt_tokens"), 0))
    completion_tokens = int(_num(usage.get("completion_tokens"), 0))
    cost = float(_num(usage.get("cost"), 0.0))

    cached_tokens = 0
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached_tokens = int(_num(prompt_details.get("cached_tokens"), 0))

    reasoning_tokens = 0
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning_tokens = int(_num(completion_details.get("reasoning_tokens"), 0))

    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        cost=cost,
        requests=1,
    )


class OpenRouterClient:
    """Thin async wrapper over POST {base_url}/chat/completions."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        provider_prefs: dict[str, Any] | None = None,
        provider_sort: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        request_timeout_s: float = 120.0,
        max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_output_tokens is not None and (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be None or an int >= 1")
        # Defense in depth: config.py's load_config already validates `provider_sort`, but this
        # constructor must never trust that a caller went through it.
        if provider_sort is not None and provider_sort not in _VALID_PROVIDER_SORT:
            raise ValueError(
                f"provider_sort must be one of {_VALID_PROVIDER_SORT} or None, "
                f"got {provider_sort!r}"
            )

        self.__api_key = api_key
        self._base_url = base_url.rstrip("/")
        if provider_prefs is not None:
            # ZDR keys are applied LAST and unconditionally: an explicit provider_prefs=
            # (a caller escape hatch, not user config) must never be able to drop or alter
            # them. `dict(provider_prefs)` also avoids aliasing the caller's own dict.
            self._provider_prefs = {**provider_prefs, **DEFAULT_PROVIDER_PREFS}
        else:
            # `sort` is set first, from caller-controlled config, then the ZDR keys are set
            # (via `update`) AFTER and unconditionally -- so no value of `sort` can displace or
            # alter them. With `provider_sort` unset this reproduces `DEFAULT_PROVIDER_PREFS`
            # exactly (same three keys, same values): today's request bodies, unchanged.
            prefs: dict[str, Any] = {}
            if provider_sort is not None:
                prefs["sort"] = provider_sort
            prefs.update(DEFAULT_PROVIDER_PREFS)
            self._provider_prefs = prefs
        self._timeout = request_timeout_s
        self._max_output_tokens = max_output_tokens
        self._client = http_client or httpx.AsyncClient(timeout=request_timeout_s)
        self._owns_client = http_client is None

    def __repr__(self) -> str:  # never leak the key via logging/debugging
        return f"OpenRouterClient(base_url={self._base_url!r})"

    def redaction_secrets(self) -> list[str]:
        """Live secret values that must be scrubbed from transcripts/logs. Not for display."""
        return [self.__api_key]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.__api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/anymodel-subagents",
            "X-Title": "anymodel-subagents",
        }

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra_body: dict[str, Any] | None = None,
    ) -> ChatResult:
        # Merge defensively: `extra_body` is applied FIRST so it can never clobber the
        # fields that are load-bearing for correctness (model/messages) or for the ZDR
        # data-retention guarantee (provider). Those are always set after, from trusted
        # values -- `provider_prefs` is the only sanctioned way to change them. `max_tokens`
        # is only a *default*: if the caller's extra_body already set it, that value wins.
        body: dict[str, Any] = {}
        if extra_body:
            body.update(extra_body)
        body["model"] = model
        body["messages"] = messages
        body["provider"] = dict(self._provider_prefs)
        if self._max_output_tokens is not None:
            # Without this OpenRouter reserves credit for the model's full output window per
            # request and can answer 402 ("requires more credits, or fewer max_tokens") on a
            # key that still has credit, especially with several workers in flight.
            body.setdefault("max_tokens", self._max_output_tokens)
        if tools:
            body["tools"] = tools
        else:
            body.pop("tools", None)
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        else:
            body.pop("tool_choice", None)

        response_json = await self._post_with_retries(body)

        if isinstance(response_json, dict) and response_json.get("error"):
            err = response_json["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            detail = _format_detail(_clean_provider_text(msg, self.redaction_secrets()))
            raise OpenRouterError(f"OpenRouter error{detail}", body=str(response_json)[:2000])

        choices = response_json.get("choices") or []
        if not choices:
            raise OpenRouterError(
                "OpenRouter response had no choices", body=str(response_json)[:2000]
            )
        choice = choices[0]
        message = choice.get("message") or {}
        usage = _parse_usage(response_json.get("usage"))
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = None
        return ChatResult(
            message=message, usage=usage, raw=response_json, finish_reason=finish_reason
        )

    async def _post_with_retries(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/chat/completions"
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(url, json=body, headers=self._headers())
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= _MAX_RETRIES:
                    raise OpenRouterError(f"OpenRouter request failed: {exc}") from None
                await self._sleep_backoff(attempt, retry_after=None)
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                last_error = OpenRouterError(
                    f"OpenRouter returned HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text,
                )
                await self._sleep_backoff(attempt, retry_after=_retry_after_seconds(resp))
                continue

            if resp.status_code >= 400:
                raise OpenRouterError(
                    f"OpenRouter returned HTTP {resp.status_code}"
                    f"{_error_detail(resp.text, self.redaction_secrets())}",
                    status_code=resp.status_code,
                    body=resp.text,
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise OpenRouterError(f"OpenRouter returned invalid JSON: {exc}") from None

        # Unreachable in practice -- loop always returns or raises -- but keeps type-checkers happy.
        if last_error is not None:
            raise OpenRouterError(
                f"OpenRouter request failed after retries: {last_error}"
            ) from None
        raise OpenRouterError("OpenRouter request failed after retries")

    async def _sleep_backoff(self, attempt: int, *, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2**attempt))
            delay = delay * (0.5 + random.random())  # jitter in [0.5x, 1.5x)
        await asyncio.sleep(delay)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse `Retry-After`, clamped to `_BACKOFF_MAX_S`.

    A malicious or misconfigured server could otherwise send an enormous
    `Retry-After` and stall the worker for far longer than our own backoff
    ceiling allows.
    """
    header = resp.headers.get("Retry-After")
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    return max(0.0, min(seconds, _BACKOFF_MAX_S))
