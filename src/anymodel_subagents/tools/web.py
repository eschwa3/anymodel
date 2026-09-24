"""WebSearch and WebFetch worker tools for `web` mode (docs/adr/0001-worker-web-access.md).

Contract stub -- implemented by the web-tools builder.
"""

from __future__ import annotations

from anymodel_subagents.types import Tool, WebClient
from anymodel_subagents.web_denylist import Denylist


def web_tools(client: WebClient, denylist: Denylist, *, max_calls: int) -> list[Tool]:
    """Fresh [WebSearch, WebFetch] sharing one per-job call counter capped at `max_calls`."""
    raise NotImplementedError
