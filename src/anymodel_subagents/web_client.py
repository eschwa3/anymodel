"""Server-side web providers: Brave LLM Context (search) + Jina Reader (fetch).

See docs/adr/0001-worker-web-access.md. Requests are made from the server
process, never from inside a worker's sandbox -- the worker only ever talks
to `tools/web.py`, which calls a `WebClient` (this module's `BraveJinaClient`).

Error messages are always short, static strings: they never include provider
response bodies, so there is nothing here for `redact` to have to catch. The
live keys are still exposed via `redaction_secrets()` for the transcript/
ledger/error-scrubbing pipeline elsewhere, matching `OpenRouterClient`.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from anymodel_subagents.types import ToolError, WebClient, WebHit, WebPage

_BRAVE_URL = "https://api.search.brave.com/res/v1/llm/context"
_BRAVE_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
_JINA_URL = "https://r.jina.ai/"

_REQUEST_TIMEOUT_S = 15.0
# Jina renders the page before answering, and heavy pages (e.g. docs.python.org
# "What's New") take longer than a search call. Jina's own render budget
# (`X-Timeout`) sits below ours so Jina gives up and answers first.
_JINA_RENDER_TIMEOUT_S = 30
_FETCH_TIMEOUT_S = 45.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TOKENS = 4096
_RATE_LIMIT_RETRY_S = 1.1


class MissingWebKeyError(RuntimeError):
    """BRAVE_API_KEY is not set (or empty) while web_enabled is true."""


def _env_key(name: str) -> str | None:
    """Read an env var, treating an empty/whitespace-only value as unset.

    The plugin injects "" for an unset optional `userConfig` key, so a bare
    `os.environ.get` would treat "configured but empty" as "configured".
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    # An unset optional plugin userConfig value may arrive as the literal placeholder.
    if value.startswith("${"):
        return None
    return value or None


def web_client_from_env() -> WebClient:
    """Build the client from BRAVE_API_KEY (required) and JINA_API_KEY (optional)."""
    brave_key = _env_key("BRAVE_API_KEY")
    if not brave_key:
        raise MissingWebKeyError("BRAVE_API_KEY is not set")
    jina_key = _env_key("JINA_API_KEY")
    return BraveJinaClient(brave_key, jina_key)


class BraveJinaClient:
    """`WebClient` backed by Brave LLM Context (search) and Jina Reader (fetch)."""

    def __init__(
        self,
        brave_api_key: str,
        jina_api_key: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.__brave_key = brave_api_key
        self.__jina_key = jina_api_key or None
        # `follow_redirects=False`: neither provider is expected to redirect a
        # simple API call, and we'd rather fail loudly than silently follow
        # one. `trust_env=False`: unlike OpenRouterClient (an outbound API
        # call to an endpoint the operator already trusts), these two calls
        # carry a Brave/Jina key and worker-influenced query/URL text, and
        # honoring `HTTP_PROXY`/`NO_PROXY` etc. from the environment would let
        # anything that can set env vars for this process reroute that
        # traffic. The ADR asks for this explicitly.
        self._client = http_client or httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_S, follow_redirects=False, trust_env=False
        )
        self._owns_client = http_client is None
        # Brave's LLM Context isn't on every plan (the legacy Free plan answers 400
        # OPTION_NOT_IN_PLAN); on that answer, switch to web search for good.
        self._brave_llm_context = True

    def __repr__(self) -> str:  # never leak keys via logging/debugging
        return "BraveJinaClient(...)"

    def redaction_secrets(self) -> list[str]:
        secrets = [self.__brave_key]
        if self.__jina_key:
            secrets.append(self.__jina_key)
        return secrets

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _stream_capped(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        too_large_msg: str,
        timeout_msg: str,
        network_msg: str,
        timeout_s: float = _REQUEST_TIMEOUT_S,
    ) -> tuple[int, bytes]:
        """POST/GET `url`, aborting once the body exceeds `_MAX_RESPONSE_BYTES`.

        Streaming (rather than `.json()`/`.content` on a plain request) means a
        provider that returns an enormous or endless body never gets fully
        buffered before we notice and give up. A 429 is retried once after
        `_RATE_LIMIT_RETRY_S` (Brave's Free plan allows 1 request/second).
        """
        for attempt in (1, 2):
            status, body = await self._stream_once(
                method,
                url,
                params=params,
                json_body=json_body,
                headers=headers,
                too_large_msg=too_large_msg,
                timeout_msg=timeout_msg,
                network_msg=network_msg,
                timeout_s=timeout_s,
            )
            if status != 429 or attempt == 2:
                break
            await asyncio.sleep(_RATE_LIMIT_RETRY_S)
        return status, body

    async def _stream_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str] | None,
        too_large_msg: str,
        timeout_msg: str,
        network_msg: str,
        timeout_s: float,
    ) -> tuple[int, bytes]:
        try:
            async with self._client.stream(
                method, url, params=params, json=json_body, headers=headers, timeout=timeout_s
            ) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ToolError(too_large_msg)
                return resp.status_code, bytes(body)
        except httpx.TimeoutException:
            raise ToolError(timeout_msg) from None
        except httpx.HTTPError:
            raise ToolError(network_msg) from None

    async def search(self, query: str, max_results: int) -> list[WebHit]:
        if self._brave_llm_context:
            status, body = await self._brave_get(
                _BRAVE_URL,
                {
                    "q": query,
                    "maximum_number_of_urls": max_results,
                    "count": max_results,
                    "maximum_number_of_tokens": _MAX_TOKENS,
                },
            )
            if status == 400 and _brave_error_code(body) == "OPTION_NOT_IN_PLAN":
                self._brave_llm_context = False
            else:
                return _parse_brave_hits(_check_search_response(status, body))
        status, body = await self._brave_get(
            _BRAVE_WEB_URL, {"q": query, "count": max_results, "text_decorations": "false"}
        )
        return _parse_brave_web_hits(_check_search_response(status, body))

    async def _brave_get(self, url: str, params: dict[str, Any]) -> tuple[int, bytes]:
        return await self._stream_capped(
            "GET",
            url,
            params=params,
            headers={"X-Subscription-Token": self.__brave_key, "Accept": "application/json"},
            too_large_msg="search failed: response too large",
            timeout_msg="search failed: request timed out",
            network_msg="search failed: network error",
        )

    async def fetch(self, url: str) -> WebPage:
        headers = {
            "Accept": "application/json",
            "DNT": "1",
            "X-Timeout": str(_JINA_RENDER_TIMEOUT_S),
        }
        if self.__jina_key:
            headers["Authorization"] = f"Bearer {self.__jina_key}"
        status, body = await self._stream_capped(
            "POST",
            _JINA_URL,
            json_body={"url": url},
            headers=headers,
            too_large_msg="fetch failed: response too large",
            timeout_msg="fetch failed: request timed out",
            network_msg="fetch failed: network error",
            timeout_s=_FETCH_TIMEOUT_S,
        )
        if status in (401, 403):
            raise ToolError("fetch provider rejected the API key")
        if status == 429:
            raise ToolError("fetch failed: rate limited")
        if status >= 400:
            raise ToolError(f"fetch failed: blocked by site or unavailable (HTTP {status})")

        try:
            data = json.loads(body)
        except ValueError:
            raise ToolError("fetch failed: invalid response from provider") from None
        return _parse_jina_page(data, url)


def _brave_error_code(body: bytes) -> str | None:
    """Brave's `error.code` (e.g. OPTION_NOT_IN_PLAN); only ever compared to fixed strings."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _check_search_response(status: int, body: bytes) -> Any:
    if status in (401, 403):
        raise ToolError("search provider rejected the API key")
    if status == 429:
        raise ToolError("search failed: rate limited")
    if status >= 400:
        raise ToolError(f"search failed: provider error (HTTP {status})")
    try:
        return json.loads(body)
    except ValueError:
        raise ToolError("search failed: invalid response from provider") from None


def _parse_brave_web_hits(data: Any) -> list[WebHit]:
    """Tolerant parse of Brave web search's `web.results[]` (the fallback path)."""
    web = data.get("web") if isinstance(data, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(results, list):
        return []
    generic = []
    for r in results:
        if not isinstance(r, dict):
            continue
        extra = r.get("extra_snippets")
        snippets = [r.get("description")] + (extra if isinstance(extra, list) else [])
        generic.append({"url": r.get("url"), "title": r.get("title"), "snippets": snippets})
    return _parse_brave_hits({"grounding": {"generic": generic}})


def _parse_brave_hits(data: Any) -> list[WebHit]:
    """Tolerant parse of Brave LLM Context's `grounding.generic[]`."""
    hits: list[WebHit] = []
    if not isinstance(data, dict):
        return hits
    grounding = data.get("grounding")
    generic = grounding.get("generic") if isinstance(grounding, dict) else None
    if not isinstance(generic, list):
        return hits
    for item in generic:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            continue
        title = item.get("title")
        title = title if isinstance(title, str) else ""
        raw_snippets = item.get("snippets")
        snippets = (
            [s for s in raw_snippets if isinstance(s, str)]
            if isinstance(raw_snippets, list)
            else []
        )
        hits.append(WebHit(url=url, title=title, snippets=snippets))
    return hits


def _parse_jina_page(data: Any, fallback_url: str) -> WebPage:
    """Tolerant parse of Jina Reader's response.

    Shape `{"data": {"title", "url", "content"}}`, confirmed against a live
    keyless response on 2026-09-25.
    """
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        raise ToolError("fetch failed: unexpected response shape")
    url = payload.get("url")
    url = url if isinstance(url, str) and url else fallback_url
    title = payload.get("title")
    title = title if isinstance(title, str) else ""
    content = payload.get("content")
    content = content if isinstance(content, str) else ""
    return WebPage(url=url, title=title, content=content)
