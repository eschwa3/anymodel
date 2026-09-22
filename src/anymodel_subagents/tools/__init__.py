"""Worker-facing file tools and the workspace that confines them."""

from __future__ import annotations

from anymodel_subagents.tools.bash import Bash, BashPolicy
from anymodel_subagents.tools.files import Edit, Glob, Grep, Read, Write
from anymodel_subagents.tools.workspace import LocalWorkspace
from anymodel_subagents.types import Mode, Tool

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
]


def tools_for_mode(mode: Mode, *, bash_policy: BashPolicy | None = None) -> list[Tool]:
    """Return fresh tool instances for the given worker mode.

    `bash_policy` is required for `edit+bash`; it is ignored for other modes.
    """
    if mode == "read-only":
        return [Read(), Grep(), Glob()]
    if mode == "edit":
        return [Read(), Grep(), Glob(), Edit(), Write()]
    if mode == "edit+bash":
        if bash_policy is None:
            raise ValueError("edit+bash mode requires a bash_policy")
        return [Read(), Grep(), Glob(), Edit(), Write(), Bash(bash_policy)]
    raise ValueError(f"unknown mode: {mode!r}")
