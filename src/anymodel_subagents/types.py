"""Shared contracts between the engine and worker tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

Mode = Literal["read-only", "edit", "edit+bash"]


class PolicyError(Exception):
    """A tool call was refused by workspace policy. The message is shown to the worker model."""


class ToolError(Exception):
    """A tool call failed for an ordinary reason (file missing, bad regex). Shown to the model."""


class Workspace(Protocol):
    """Confines a worker to one directory tree. Implemented in tools/workspace.py."""

    root: Path

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        """Return the real absolute path for `path` (absolute, or relative to root).

        Raises PolicyError if the resolved path escapes root or matches a denied pattern.
        """
        ...

    def is_denied(self, path: Path) -> bool:
        """True if `path` must be hidden from listings/search (fails closed)."""
        ...

    def is_sensitive(self, path: Path) -> bool:
        """True if a write to `path` is allowed but must be flagged for deliberate review

        (build/test/tooling files that execute later: conftest.py, Makefile, package.json, ...).
        """
        ...

    def is_write_denied(self, path: str | Path) -> bool:
        """True if a write to `path` would be refused by policy. Never raises.

        Used post-hoc (e.g. by worktree finalization) to classify paths a
        sandboxed Bash script may have written outside the file-tool policy
        entirely -- see `tools/workspace.py`'s `LocalWorkspace.is_write_denied`.
        """
        ...


class Tool(Protocol):
    name: str
    #: OpenAI function-calling schema: {"type": "function", "function": {name, description, parameters}}
    schema: dict[str, Any]

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        """Execute and return text for the model. Raise PolicyError/ToolError on refusal/failure."""
        ...


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0  # USD, summed from OpenRouter usage.cost
    requests: int = 0


@dataclass
class WorkerResult:
    status: Literal["completed", "max_turns", "timeout", "error", "cancelled", "budget_exceeded"]
    final_message: str
    model: str
    turns: int
    usage: Usage
    tool_calls: int = 0
    invalid_tool_calls: int = 0  # unknown tool, unparseable JSON args, schema-invalid args
    changed_files: list[str] = field(default_factory=list)  # normalized, root-relative
    sensitive_changed_files: list[str] = field(default_factory=list)  # subset needing review
    transcript_path: str | None = None
    error: str | None = None
    duration_s: float = 0.0
    # Populated only for worktree jobs: paths a sandboxed script wrote outside
    # file-tool policy (denied globs, secret-shaped names, symlinks, setuid/
    # setgid bits) that were reverted before the worktree was committed.
    policy_reverted_files: list[str] = field(default_factory=list)
    policy_note: str | None = None  # short explanation, set only when the above is non-empty
