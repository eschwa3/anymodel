"""Server-side web providers: Brave LLM Context (search) + Jina Reader (fetch).

Contract stub -- implemented by the web-tools builder. See docs/adr/0001-worker-web-access.md.
"""

from __future__ import annotations

from anymodel_subagents.types import WebClient


class MissingWebKeyError(RuntimeError):
    """BRAVE_API_KEY is not set (or empty) while web_enabled is true."""


def web_client_from_env() -> WebClient:
    """Build the client from BRAVE_API_KEY (required) and JINA_API_KEY (optional).

    Empty strings count as unset (plugin userConfig injects "" for unset optional keys).
    """
    raise NotImplementedError
