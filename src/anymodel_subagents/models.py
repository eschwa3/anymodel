"""OpenRouter model discovery: tool-calling-capable, ZDR-only models with prices.

See SPEC.md's "Open items" note on picking default model ids from a live
`/api/v1/models`-style listing filtered to tools + ZDR endpoints -- this backs
`list_workers(include_models=True)`.

`list_zdr_tool_models` calls OpenRouter's public `/endpoints/zdr` listing. That
endpoint is public and requires no API key, so callers pass a plain
`httpx.AsyncClient` (never an `OpenRouterClient`, which would attach the
key) -- no `Authorization` header is ever sent by this module.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_ZDR_ENDPOINTS_PATH = "/endpoints/zdr"
_CACHE_TTL_S = 3600.0
_REQUEST_TIMEOUT_S = 30.0

# Process-wide cache of the raw (unfiltered) endpoints payload. Shared across
# calls regardless of `max_input_price_per_m` -- the price filter is applied
# in-process against the cached payload rather than re-fetched per filter.
_cache: dict[str, Any] = {"payload": None, "fetched_at": 0.0}


def _reset_cache() -> None:
    """Test-only: clear the in-memory cache so each test starts fresh."""
    _cache["payload"] = None
    _cache["fetched_at"] = 0.0


def _price_per_million(price_per_token: Any) -> float | None:
    """OpenRouter prices are strings of USD-per-token; convert to USD per 1M tokens."""
    if price_per_token is None:
        return None
    try:
        return float(price_per_token) * 1_000_000
    except (TypeError, ValueError):
        return None


async def _fetch_payload(http: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    now = time.monotonic()
    cached = _cache["payload"]
    if cached is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_S:
        return cached

    resp = await http.get(f"{base_url}{_ZDR_ENDPOINTS_PATH}", timeout=_REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        # ValueError (not TypeError) is this module's public contract for a malformed response.
        msg = "unexpected response shape from /endpoints/zdr (expected an object)"
        raise ValueError(msg)  # noqa: TRY004

    _cache["payload"] = payload
    _cache["fetched_at"] = now
    return payload


def _group_by_model(
    payload: dict[str, Any], *, max_input_price_per_m: float | None
) -> list[dict[str, Any]]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        return []

    results: list[dict[str, Any]] = []
    for model_entry in entries:
        if not isinstance(model_entry, dict):
            continue
        model_id = model_entry.get("id") or model_entry.get("slug")
        if not isinstance(model_id, str):
            continue

        endpoints = model_entry.get("endpoints")
        if not isinstance(endpoints, list):
            continue

        tool_endpoints = [
            ep
            for ep in endpoints
            if isinstance(ep, dict) and "tools" in (ep.get("supported_parameters") or [])
        ]
        if not tool_endpoints:
            continue

        prompt_prices: list[float] = []
        completion_prices: list[float] = []
        context_lengths: list[int] = []
        implicit_caching = False
        for ep in tool_endpoints:
            pricing = ep.get("pricing") or {}
            p = _price_per_million(pricing.get("prompt"))
            c = _price_per_million(pricing.get("completion"))
            if p is not None:
                prompt_prices.append(p)
            if c is not None:
                completion_prices.append(c)
            ctx = ep.get("context_length")
            if isinstance(ctx, (int, float)) and not isinstance(ctx, bool):
                context_lengths.append(int(ctx))
            if ep.get("supports_implicit_caching"):
                implicit_caching = True

        if not prompt_prices:
            continue
        cheapest_prompt = min(prompt_prices)
        if max_input_price_per_m is not None and cheapest_prompt > max_input_price_per_m:
            continue

        results.append(
            {
                "model_id": model_id,
                "name": model_entry.get("name") or model_id,
                "prompt_price_per_m": cheapest_prompt,
                "completion_price_per_m": (min(completion_prices) if completion_prices else None),
                "context_length": max(context_lengths) if context_lengths else None,
                "zdr_provider_count": len(tool_endpoints),
                "supports_implicit_caching": implicit_caching,
            }
        )

    results.sort(key=lambda m: m["prompt_price_per_m"])
    return results


async def list_zdr_tool_models(
    http: httpx.AsyncClient,
    *,
    max_input_price_per_m: float | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    """Tool-calling-capable ZDR models on OpenRouter, cheapest prompt price first.

    Each result: `model_id`, `name`, `prompt_price_per_m`/`completion_price_per_m`
    (cheapest across ZDR-tool-capable providers, USD per 1M tokens),
    `context_length` (best available), `zdr_provider_count`, and
    `supports_implicit_caching` (true if any qualifying provider supports it).

    Never raises: a request timeout, transport error, HTTP error status, or
    malformed response is reported back as a single-item list containing an
    `{"error": "..."}` entry instead.
    """
    try:
        payload = await _fetch_payload(http, base_url)
    except (httpx.HTTPError, ValueError) as exc:
        return [{"error": f"failed to fetch ZDR endpoints from OpenRouter: {exc}"}]
    except Exception as exc:  # noqa: BLE001 - this must never raise into a tool call
        return [{"error": f"unexpected error fetching ZDR endpoints: {exc}"}]

    try:
        return _group_by_model(payload, max_input_price_per_m=max_input_price_per_m)
    except Exception as exc:  # noqa: BLE001 - a malformed payload must not crash the caller
        return [{"error": f"unexpected error parsing ZDR endpoints: {exc}"}]
