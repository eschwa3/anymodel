"""Role loading: bundled/user/project markdown role files (see SPEC.md's
"Roles (routing)" section).

Role files use the same shape as Claude Code agent files: a YAML frontmatter
block followed by a system-prompt body, e.g.

    ---
    name: reviewer
    description: One line the orchestrator uses to pick this role.
    model: deepseek/deepseek-v4.1-flash
    mode: read-only
    isolation: none
    max_turns: 25
    ---
    <system prompt body>

Search order, later overriding earlier by `name`:

    bundled `anymodel_subagents/workers/`
      -> user `${XDG_CONFIG_HOME:-~/.config}/anymodel-subagents/workers/`
      -> project `<repo_root>/.workers/`

SECURITY: project-level `.workers/*.md` files live inside the repository a
worker is operating on, which may itself be untrusted -- and a role file *is*
a system prompt, so a hostile one is a direct route to prompt injection
against the worker (exactly what SPEC.md's "Injection posture" section is
about). They are therefore loaded ONLY when the caller's config has
`allow_project_roles: true`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from anymodel_subagents.types import Mode

_APP_NAME = "anymodel-subagents"

# Kept in sync with jobs._MODEL_RE by hand -- duplicated rather than imported
# to avoid a circular import (jobs.py imports this module to resolve a task's
# `role` field).
_NAME_RE = re.compile(r"^[a-z0-9-]{2,40}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{3,100}$")
_VALID_MODES: tuple[str, ...] = ("read-only", "edit", "edit+bash")
_VALID_ISOLATION: tuple[str, ...] = ("none", "worktree")

_MAX_BODY_CHARS = 20_000
_MAX_FILE_BYTES = 64 * 1024
_MAX_MAX_TURNS = 1000  # sanity ceiling only; jobs.py clamps again against cfg.max_turns

# `---` frontmatter fence, then the body (DOTALL so `.` matches newlines).
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.DOTALL)

RoleSource = Literal["bundled", "user", "project"]


@dataclass(frozen=True)
class Role:
    """One parsed, validated role file."""

    name: str
    description: str
    model: str
    mode: Mode
    isolation: str | None  # None = role has no isolation opinion; else "none" | "worktree"
    max_turns: int | None
    prompt: str  # the system-prompt body
    source: RoleSource
    path: Path


def _bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "workers"


def _user_dir() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / _APP_NAME / "workers"


def _project_workers_dir(project_dir: Path | None) -> Path | None:
    if project_dir is None:
        return None
    return Path(project_dir) / ".workers"


def _parse_role_file(path: Path, source: RoleSource) -> Role:
    """Parse and validate one role file. Raises ValueError with a short reason on failure."""
    if path.is_symlink():
        raise ValueError(f"{path}: symlinks are not allowed, skipping")

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{path}: could not read file: {exc}") from exc

    if len(raw_bytes) > _MAX_FILE_BYTES:
        raise ValueError(f"{path}: file exceeds {_MAX_FILE_BYTES} bytes, skipping")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from exc

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter (expected a leading '---' block)")

    frontmatter_text, body = m.group(1), m.group(2)

    try:
        meta = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML frontmatter: {exc}") from exc

    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")  # noqa: TRY004

    name = meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"{path}: invalid or missing 'name' (must match {_NAME_RE.pattern})")

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path}: invalid or missing 'description'")

    model = meta.get("model")
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise ValueError(f"{path}: invalid or missing 'model'")

    mode = meta.get("mode")
    if mode not in _VALID_MODES:
        raise ValueError(f"{path}: invalid or missing 'mode' (must be one of {_VALID_MODES})")

    isolation = meta.get("isolation")
    if isolation is not None and isolation not in _VALID_ISOLATION:
        raise ValueError(f"{path}: invalid 'isolation' (must be one of {_VALID_ISOLATION})")

    max_turns = meta.get("max_turns")
    if max_turns is not None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise ValueError(f"{path}: 'max_turns' must be an integer")
        if not (1 <= max_turns <= _MAX_MAX_TURNS):
            raise ValueError(f"{path}: 'max_turns' must be between 1 and {_MAX_MAX_TURNS}")

    body = body.strip()
    if not body:
        raise ValueError(f"{path}: role body (system prompt) must not be empty")
    if len(body) > _MAX_BODY_CHARS:
        raise ValueError(f"{path}: role body exceeds {_MAX_BODY_CHARS} characters")

    return Role(
        name=name,
        description=description.strip(),
        model=model,
        mode=mode,  # type: ignore[arg-type]  -- validated against _VALID_MODES above
        isolation=isolation,
        max_turns=max_turns,
        prompt=body,
        source=source,
        path=path,
    )


def _load_dir(dir_path: Path, source: RoleSource) -> tuple[dict[str, Role], list[str]]:
    roles: dict[str, Role] = {}
    warnings: list[str] = []
    if not dir_path.is_dir():
        return roles, warnings
    for entry in sorted(dir_path.glob("*.md")):
        try:
            role = _parse_role_file(entry, source)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        roles[role.name] = role
    return roles, warnings


def load_roles_with_warnings(
    project_dir: Path | None = None, cfg: Any | None = None
) -> tuple[dict[str, Role], list[str]]:
    """Load roles in override order (bundled -> user -> project), by name.

    Never raises for a bad individual role file -- it is skipped and a short
    reason is appended to the returned warnings list instead. Project-level
    roles (`<project_dir>/.workers/*.md`) are only loaded when `cfg` has
    `allow_project_roles` set truthy; see the module docstring's SECURITY note.
    """
    roles: dict[str, Role] = {}
    warnings: list[str] = []

    for dir_path, source in ((_bundled_dir(), "bundled"), (_user_dir(), "user")):
        found, w = _load_dir(dir_path, source)
        roles.update(found)
        warnings.extend(w)

    project_workers_dir = _project_workers_dir(project_dir)
    if project_workers_dir is not None:
        allow_project_roles = bool(getattr(cfg, "allow_project_roles", False))
        if allow_project_roles:
            found, w = _load_dir(project_workers_dir, "project")
            roles.update(found)
            warnings.extend(w)
        elif project_workers_dir.is_dir():
            warnings.append(
                f"{project_workers_dir}: project role files found but not loaded "
                "(set allow_project_roles: true in config.yaml to enable)"
            )

    return roles, warnings


def load_roles(project_dir: Path | None = None, cfg: Any | None = None) -> dict[str, Role]:
    """Convenience wrapper over `load_roles_with_warnings` that discards warnings."""
    roles, _warnings = load_roles_with_warnings(project_dir, cfg)
    return roles
