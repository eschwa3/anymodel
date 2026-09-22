"""File tools exposed to workers: Read, Glob, Grep, Write, Edit.

Names and parameter shapes mirror Claude Code's own tools, since the cheap
worker models are well-trained on that exact interface. All policy
enforcement (root confinement, denied paths) lives in `LocalWorkspace` —
these tools call `ws.resolve()`/`ws.is_denied()` and otherwise just do I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

import regex

from anymodel_subagents.types import PolicyError, ToolError, Workspace

_SKIP_DIR_NAMES = {".git", "node_modules", ".venv"}


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _clamp_int(value: Any, *, lo: int, hi: int | None, default: int, name: str) -> int:
    """Coerce a model-controlled argument to int and clamp it to [lo, hi].

    `value` is absent (None) -> `default`. `value` present but not coercible
    to int -> ToolError, never a raw Python exception surfaced to the model.
    """
    if value is None:
        return default
    try:
        ivalue = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ToolError(f"{name} must be an integer") from None
    ivalue = max(ivalue, lo)
    if hi is not None and ivalue > hi:
        ivalue = hi
    return ivalue


def _check_pattern_cost(pattern: str) -> None:
    """Refuse regexes whose *compile* cost is unbounded.

    `regex.compile` itself can be the attack, independent of match time:
    nested counted repetitions allocate memory proportional to the product
    of their bounds (`(?:(?:a{1000}){1000}){1000}` ~ 2 GB). Bound the
    pattern length, each counted bound, and the product of all counted
    bounds. This is a cheap static scan, so it runs before compiling.
    """
    if len(pattern) > 1000:
        raise ToolError("pattern too long (max 1000 characters)")
    product = 1
    for m in re.finditer(r"\{(\d+)(?:,(\d*))?\}", pattern):
        low = int(m.group(1))
        if m.group(2) is None:  # {n} — exact count, one bound
            high = low
        else:
            high = int(m.group(2)) if m.group(2) else None
        if low > 1000 or (high is not None and high > 1000):
            raise ToolError("pattern repetition bound too large (max 1000)")
        if high is not None:
            product *= high
            if product > 100000:
                raise ToolError("pattern repetition product too large (max 100000)")


def _check_path_characters(path: str) -> None:
    """Refuse a write-target path whose characters could mislead a reviewer.

    Bidi/zero-width formatting characters (Trojan-Source style trickery) and
    angle brackets have no legitimate use in a worker-supplied path: they can
    make one path render as another in reports, logs, and tool output. The
    whole requested path is scanned, which is per-component equivalent since
    none of these characters is a path separator. Write tools only — reading
    an oddly-named file that already exists stays allowed.
    """
    for ch in path:
        code = ord(ch)
        if code < 32 or code == 127:
            raise PolicyError("path contains a control character")
        if code in _TRICKY_PATH_CODEPOINTS:
            raise PolicyError("path contains a bidi or zero-width character")
        if ch in "<>":
            raise PolicyError("path contains '<' or '>'")


# Bidi-control and invisible-formatting code points used in "Trojan Source"
# -style tricks (mirrors the set bash.py refuses in commands).
_TRICKY_PATH_CODEPOINTS = frozenset(
    {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200B, 0x200C, 0x200D, 0xFEFF}
)


def _check_regular_readable(path: Path, *, dir_hint: str = "") -> os.stat_result:
    """Stat `path`, refusing directories, non-regular files, and hardlinks.

    Called immediately before a Read/Edit tool actually opens `path`, so a
    FIFO or device node can't cause an indefinite blocking open/read, and a
    hard-linked file — which can alias content living outside policy control
    under an innocuous-looking name, or let a write reach a file shared with
    something outside the workspace — is refused outright rather than
    silently trusted.
    """
    if not path.exists():
        raise ToolError("file not found")
    if path.is_dir():
        raise ToolError(f"path is a directory{dir_hint}")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("not a regular file")
    if st.st_nlink > 1:
        raise PolicyError("refusing to operate on a hard-linked file")
    return st


def _check_parent_dir(parent: Path, expected: Path, root: Path) -> None:
    """Re-verify the write target's parent directory (TOCTOU).

    `parent` may have been swapped for a symlink since resolve() time; the
    real directory must still be exactly `expected` (the parent as resolved
    by `ws.resolve()`) and inside the workspace root.
    """
    real_parent = Path(os.path.realpath(parent))
    real_root = Path(os.path.realpath(root))
    if real_parent != expected or not real_parent.is_relative_to(real_root):
        raise PolicyError("directory changed under the workspace during write")


def _atomic_write(path: Path, data: bytes, root: Path) -> None:
    """Write `data` to `path` atomically, preserving the existing file mode.

    Refuses to write onto an existing non-regular file or hard-linked file
    (see `_check_regular_readable`'s docstring for why hardlinks are denied
    outright).

    TOCTOU mitigation: the symlink check is re-run immediately before the
    final os.replace() to narrow (not eliminate — see workspace.py's
    docstring) the race window between resolve()-time validation and the
    write.
    """
    if path.exists() and not path.is_symlink():
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ToolError("refusing to write: not a regular file")
        if st.st_nlink > 1:
            raise PolicyError("refusing to write a hard-linked file")
        mode = st.st_mode & 0o777
    else:
        mode = 0o644
    parent = path.parent
    _check_parent_dir(parent, parent, root)
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        if path.is_symlink():
            raise PolicyError("refusing to write through a symlink")
        _check_parent_dir(parent, parent, root)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class Read:
    """Read a text file, `cat -n` style."""

    name = "Read"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Read",
            "description": (
                "Read a text file from the workspace, returned with line numbers. "
                "Refuses binary files, directories, and files larger than 10 MB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or root-relative path to the file.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based line number to start reading from (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default 2000).",
                    },
                },
                "required": ["file_path"],
            },
        },
    }

    MAX_BYTES = 10 * 1024 * 1024
    DEFAULT_LIMIT = 2000
    MAX_LIMIT = 5000
    MAX_LINE_CHARS = 2000

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        file_path = args["file_path"]
        offset = _clamp_int(args.get("offset"), lo=1, hi=None, default=1, name="offset")
        limit = _clamp_int(
            args.get("limit"), lo=1, hi=self.MAX_LIMIT, default=self.DEFAULT_LIMIT, name="limit"
        )
        final = ws.resolve(file_path, for_write=False)
        return await asyncio.to_thread(self._read_sync, final, offset, limit)

    @classmethod
    def _read_sync(cls, final: Path, offset: int, limit: int) -> str:
        st = _check_regular_readable(final, dir_hint="; use Glob to list its contents")
        size = st.st_size
        if size > cls.MAX_BYTES:
            raise ToolError(f"file too large ({size} bytes, max {cls.MAX_BYTES})")
        raw = final.read_bytes()
        if _looks_binary(raw):
            raise ToolError("binary file; cannot read as text")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = offset - 1
        selected = lines[start : start + limit]
        out_lines = []
        for i, line in enumerate(selected, start=offset):
            if len(line) > cls.MAX_LINE_CHARS:
                line = line[: cls.MAX_LINE_CHARS]
            out_lines.append(f"{i:6d}\t{line}")
        return "\n".join(out_lines)


class Glob:
    """Find files by glob pattern, newest first."""

    name = "Glob"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": (
                "Find files matching a glob pattern within the workspace, sorted by "
                "modification time (newest first). Skips .git, node_modules, .venv, "
                "and anything denied by policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '*.py' or 'src/**/*.ts'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search within (default: workspace root).",
                    },
                },
                "required": ["pattern"],
            },
        },
    }

    MAX_RESULTS = 500
    # Upper bound on how many matching candidates we accumulate before
    # sorting, independent of MAX_RESULTS: bounds memory on a pathological
    # tree with an enormous number of matches, at the cost of the result
    # possibly not being the *global* newest MAX_RESULTS files in that case
    # (we still sort and return the newest among whatever we collected).
    MAX_CANDIDATES = 5000
    # The walk itself is bounded independently of how many entries *match*:
    # a pattern that matches nothing must not sweep an enormous tree. Bound
    # both the number of entries visited and the wall-clock time, checked the
    # same way Grep checks its deadline.
    MAX_VISITED = 200_000
    TIMEOUT_S = 10.0

    TIME_CUT_NOTE = "note: search stopped at the time limit; results may be incomplete"
    VISITED_CUT_NOTE = "note: search stopped at the entry limit; results may be incomplete"

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        pattern = args["pattern"]
        rel_path = args.get("path") or "."
        base = ws.resolve(rel_path, for_write=False)
        if not base.is_dir():
            raise ToolError(f"not a directory: {rel_path}")
        deadline = time.monotonic() + self.TIMEOUT_S
        return await asyncio.to_thread(self._glob_sync, pattern, base, ws, deadline)

    @classmethod
    def _glob_sync(cls, pattern: str, base: Path, ws: Workspace, deadline: float) -> str:
        root = ws.root
        hits: list[tuple[float, str]] = []
        visited = 0
        cut_note = ""
        # Explicit stack walk instead of os.walk: scandir yields entries
        # lazily, so one directory holding hundreds of thousands of files is
        # never materialised before the caps are checked. Each directory
        # entry and each file entry is counted exactly once (os.walk's
        # `1 + len(dirnames)` counted every subdirectory twice: once as a
        # parent's child, once as its own step), the caps are checked per
        # entry as it is pulled, and symlinked directories are never entered.
        # Unreadable directories are skipped silently, like os.walk's
        # onerror=None. Output ordering comes from the mtime sort below,
        # unchanged from the os.walk version.
        stack = [base]
        while stack and not cut_note and len(hits) < cls.MAX_CANDIDATES:
            current = stack.pop()
            if time.monotonic() >= deadline:
                cut_note = cls.TIME_CUT_NOTE
                break
            try:
                scan = os.scandir(current)
            except OSError:
                continue
            with scan:
                for entry in scan:
                    visited += 1
                    if visited > cls.MAX_VISITED:
                        cut_note = cls.VISITED_CUT_NOTE
                        break
                    if time.monotonic() >= deadline:
                        cut_note = cls.TIME_CUT_NOTE
                        break
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIP_DIR_NAMES:
                            stack.append(entry.path)
                        continue
                    full = Path(entry.path)
                    try:
                        rel_posix = full.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    matched = (
                        fnmatch.fnmatch(rel_posix, pattern)
                        if "/" in pattern
                        else fnmatch.fnmatch(entry.name, pattern)
                    )
                    if not matched:
                        continue
                    if ws.is_denied(rel_posix):
                        continue
                    try:
                        mtime = full.stat().st_mtime
                    except OSError:
                        continue
                    hits.append((mtime, rel_posix))
                    if len(hits) >= cls.MAX_CANDIDATES:
                        break
        hits.sort(key=lambda t: t[0], reverse=True)
        hits = hits[: cls.MAX_RESULTS]
        lines = [rel for _, rel in hits]
        if cut_note:
            lines.append(cut_note)
            return "\n".join(lines)
        if not lines:
            return "No files found."
        return "\n".join(lines)


class Grep:
    """regex-based search over workspace files, resistant to ReDoS.

    Uses the third-party `regex` module (not stdlib `re`) and passes an
    explicit `timeout=` to every match call. `regex`'s matching loop checks
    a wall-clock deadline as it runs and periodically releases the GIL, so
    it can raise TimeoutError mid-match instead of holding the GIL until a
    pathological pattern finishes backtracking — which, for stdlib `re`, can
    take minutes on a single line and freezes the whole asyncio loop (see
    the regression test for the exact repro). We additionally:

    - Cap the whole call to `timeout_s` wall-clock seconds, checked between
      files and lines.
    - Cap any single match call to `per_match_timeout_s`, so one call can't
      by itself consume the whole per-call budget.
    - Truncate any single line to `MAX_LINE_CHARS` before matching, since
      match cost also grows with input length.
    - Cap total emitted output to `MAX_OUTPUT_BYTES`.
    """

    name = "Grep"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": (
                "Search file contents with a Python regular expression. "
                "Skips binaries, files over 2 MB, and anything denied by policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "path": {
                        "type": "string",
                        "description": "File or directory to search (default: workspace root).",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "Default: files_with_matches.",
                    },
                    "-i": {"type": "boolean", "description": "Case-insensitive match."},
                    "-n": {
                        "type": "boolean",
                        "description": "Show line numbers (content mode only).",
                    },
                    "-C": {
                        "type": "integer",
                        "description": "Lines of context around each match (content mode only).",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "Cap on output lines (default 200).",
                    },
                },
                "required": ["pattern"],
            },
        },
    }

    DEFAULT_HEAD_LIMIT = 200
    MIN_HEAD_LIMIT = 1
    MAX_HEAD_LIMIT = 5000
    MIN_CONTEXT = 0
    MAX_CONTEXT = 20
    MAX_FILE_BYTES = 2 * 1024 * 1024
    MAX_LINE_CHARS = 4096
    MAX_OUTPUT_BYTES = 256 * 1024
    TIMEOUT_S = 10.0
    PER_MATCH_TIMEOUT_S = 1.0

    def __init__(
        self,
        timeout_s: float = TIMEOUT_S,
        max_file_bytes: int = MAX_FILE_BYTES,
        per_match_timeout_s: float = PER_MATCH_TIMEOUT_S,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_file_bytes = max_file_bytes
        self.per_match_timeout_s = per_match_timeout_s

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        pattern = args["pattern"]
        rel_path = args.get("path") or "."
        glob_filter = args.get("glob")
        output_mode = args.get("output_mode") or "files_with_matches"
        if output_mode not in ("files_with_matches", "content", "count"):
            raise ToolError(f"invalid output_mode: {output_mode!r}")
        ignore_case = bool(args.get("-i", False))
        show_line_numbers = bool(args.get("-n", False))
        context = _clamp_int(
            args.get("-C"), lo=self.MIN_CONTEXT, hi=self.MAX_CONTEXT, default=0, name="-C"
        )
        head_limit = _clamp_int(
            args.get("head_limit"),
            lo=self.MIN_HEAD_LIMIT,
            hi=self.MAX_HEAD_LIMIT,
            default=self.DEFAULT_HEAD_LIMIT,
            name="head_limit",
        )

        _check_pattern_cost(pattern)
        try:
            # to_thread: compiling a merely *valid* pattern can still be
            # slow; keep the event loop responsive.
            compiled = await asyncio.to_thread(
                regex.compile, pattern, regex.IGNORECASE if ignore_case else 0
            )
        except regex.error as exc:
            raise ToolError(f"invalid regex: {exc}") from exc

        base = ws.resolve(rel_path, for_write=False)
        deadline = time.monotonic() + self.timeout_s
        try:
            return await asyncio.to_thread(
                self._search_sync,
                compiled,
                base,
                ws,
                glob_filter,
                output_mode,
                show_line_numbers,
                context,
                head_limit,
                deadline,
            )
        except TimeoutError as exc:
            raise ToolError("search timed out") from exc

    def _search_sync(
        self,
        pattern: regex.Pattern,
        base: Path,
        ws: Workspace,
        glob_filter: str | None,
        output_mode: str,
        show_line_numbers: bool,
        context: int,
        head_limit: int,
        deadline: float,
    ) -> str:
        root = ws.root
        output: list[str] = []
        for full in self._candidate_files(base):
            if time.monotonic() >= deadline:
                raise TimeoutError("search timed out")
            try:
                rel_posix = full.relative_to(root).as_posix()
            except ValueError:
                continue
            if glob_filter and not (
                fnmatch.fnmatch(rel_posix, glob_filter) or fnmatch.fnmatch(full.name, glob_filter)
            ):
                continue
            if ws.is_denied(rel_posix):
                continue
            try:
                st = full.stat()
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_nlink > 1:
                    # Hard-linked file: skip rather than abort the whole
                    # search (see files.py/workspace.py docstrings on why
                    # hardlinks are refused for direct-target tools).
                    continue
                if st.st_size > self.max_file_bytes:
                    continue
                raw = full.read_bytes()
            except OSError:
                continue
            if _looks_binary(raw):
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
            matched_idx: list[int] = []
            for i, line in enumerate(lines):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("search timed out")
                if len(line) > self.MAX_LINE_CHARS:
                    line = line[: self.MAX_LINE_CHARS]
                    lines[i] = line
                match_timeout = min(self.per_match_timeout_s, remaining)
                if pattern.search(line, timeout=match_timeout):
                    matched_idx.append(i)
            if not matched_idx:
                continue
            if output_mode == "files_with_matches":
                output.append(rel_posix)
            elif output_mode == "count":
                output.append(f"{rel_posix}:{len(matched_idx)}")
            else:
                output.extend(
                    self._format_content(rel_posix, lines, matched_idx, show_line_numbers, context)
                )
            output_bytes = sum(len(o) + 1 for o in output)
            if len(output) >= head_limit or output_bytes >= self.MAX_OUTPUT_BYTES:
                break
        if not output:
            return "No matches found."
        result = "\n".join(output[:head_limit])
        encoded = result.encode("utf-8")
        if len(encoded) > self.MAX_OUTPUT_BYTES:
            result = encoded[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        return result

    @staticmethod
    def _candidate_files(base: Path):
        if base.is_file():
            yield base
            return
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
            for fname in filenames:
                yield Path(dirpath) / fname

    @staticmethod
    def _format_content(
        rel_posix: str,
        lines: list[str],
        matched_idx: list[int],
        show_line_numbers: bool,
        context: int,
    ) -> list[str]:
        matched_set = set(matched_idx)
        include: set[int] = set()
        for i in matched_idx:
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                include.add(j)
        out: list[str] = []
        prev: int | None = None
        for i in sorted(include):
            if prev is not None and i != prev + 1:
                out.append("--")
            marker = ":" if i in matched_set else "-"
            if show_line_numbers:
                out.append(f"{rel_posix}{marker}{i + 1}{marker}{lines[i]}")
            else:
                out.append(f"{rel_posix}{marker}{lines[i]}")
            prev = i
        return out


class Write:
    """Create or overwrite a file."""

    name = "Write"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Create or overwrite a file with the given content (max 1 MB).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to write."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["file_path", "content"],
            },
        },
    }

    MAX_BYTES = 1024 * 1024

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        file_path = args["file_path"]
        content = args.get("content", "")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        _check_path_characters(file_path)
        final = ws.resolve(file_path, for_write=True)
        data = content.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ToolError(f"content too large ({len(data)} bytes, max {self.MAX_BYTES})")
        await asyncio.to_thread(self._write_sync, final, data, ws.root)
        rel = final.relative_to(ws.root)
        return f"Wrote {len(data)} bytes to {rel.as_posix()}"

    @staticmethod
    def _write_sync(final: Path, data: bytes, root: Path) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(final, data, root)


class Edit:
    """Exact-string find-and-replace within a file."""

    name = "Edit"
    schema: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": (
                "Replace an exact string in a file. Fails if old_string is missing, or "
                "appears more than once without replace_all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to edit."},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false).",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    }

    async def run(self, args: dict[str, Any], ws: Workspace) -> str:
        file_path = args["file_path"]
        old_string = args["old_string"]
        new_string = args["new_string"]
        replace_all = bool(args.get("replace_all", False))
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical")
        _check_path_characters(file_path)
        final = ws.resolve(file_path, for_write=True)
        return await asyncio.to_thread(
            self._edit_sync, final, old_string, new_string, replace_all, ws.root
        )

    @staticmethod
    def _edit_sync(
        final: Path, old_string: str, new_string: str, replace_all: bool, root: Path
    ) -> str:
        _check_regular_readable(final)
        try:
            text = final.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("binary file; cannot edit as text") from exc
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string not found in file")
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string found {count} times; pass replace_all=true or add more context"
            )
        new_text = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )
        _atomic_write(final, new_text.encode("utf-8"), root)
        rel = final.relative_to(root)
        n = count if replace_all else 1
        return f"Edited {rel.as_posix()} ({n} replacement{'s' if n != 1 else ''})"
