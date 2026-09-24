"""Worker-facing file tools and the workspace that confines them."""

from __future__ import annotations

from anymodel_subagents.tools.bash import Bash, BashPolicy
from anymodel_subagents.tools.files import Edit, Glob, Grep, Read, Write
from anymodel_subagents.tools.web import web_tools
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import Mode, Tool, WebClient
from anymodel_subagents.web_denylist import Denylist

__all__ = [
    "Bash",
    "BashPolicy",
    "Edit",
    "Glob",
    "Grep",
    "LocalWorkspace",
    "Read",
    "Write",
    "tools_for_mode",
    "web_tools",
]


def tools_for_mode(
    mode: Mode,
    *,
    bash_policy: BashPolicy | None = None,
    web_client: WebClient | None = None,
    denylist: Denylist | None = None,
    web_max_calls: int = 30,
) -> list[Tool]:
    """Return fresh tool instances for the given worker mode.

    `bash_policy` is required for `edit+bash`; `web_client` and `denylist` for `web`.
    Each is ignored for other modes. `web` never gets file tools, and no other mode gets
    web tools (docs/adr/0001-worker-web-access.md).
    """
    if mode == "read-only":
        return [Read(), Grep(), Glob()]
    if mode == "edit":
        return [Read(), Grep(), Glob(), Edit(), Write()]
    if mode == "edit+bash":
        if bash_policy is None:
            raise ValueError("edit+bash mode requires a bash_policy")
        return [Read(), Grep(), Glob(), Edit(), Write(), Bash(bash_policy)]
    if mode == "web":
        if web_client is None or denylist is None:
            raise ValueError("web mode requires a web_client and a denylist")
        return web_tools(web_client, denylist, max_calls=web_max_calls)
    raise ValueError(f"unknown mode: {mode!r}")
