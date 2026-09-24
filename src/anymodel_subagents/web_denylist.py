"""Portable web denylist (docs/adr/0001-worker-web-access.md, "Portable launch denylist").

Contract stub -- implemented by the denylist builder.
"""

from __future__ import annotations

from collections.abc import Iterable


class Denylist:
    def __init__(self, entries: Iterable[str]) -> None:
        raise NotImplementedError

    def is_blocked(self, host: str) -> bool:
        """True if `host` (any case, IDNA or Unicode, optional trailing dot) is denied."""
        raise NotImplementedError


def parse(text: str) -> list[str]:
    """Parse denylist file text into normalized entries (`!`-prefixed = exception)."""
    raise NotImplementedError


def load_denylist(extra: Iterable[str] = ()) -> Denylist:
    """Bundled list + user file (XDG config dir) + `extra` (config.yaml's web_denylist_extra)."""
    raise NotImplementedError


def to_claude_code_rules(denylist: Denylist) -> tuple[list[str], list[str]]:
    """(`WebFetch(domain:...)` deny rules, warnings) for Claude Code `permissions.deny`."""
    raise NotImplementedError
