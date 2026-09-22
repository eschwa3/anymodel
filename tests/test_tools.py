"""Tests for the Read/Glob/Grep/Write/Edit tool implementations."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from pathlib import Path

import pytest

from anymodel_subagents.tools import Edit, Glob, Grep, LocalWorkspace, Read, Write, tools_for_mode
from anymodel_subagents.types import PolicyError, ToolError


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


# --------------------------------------------------------------------------- Read


async def test_read_numbering_and_default_limit(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("one\ntwo\nthree\n")
    out = await Read().run({"file_path": "f.txt"}, ws)
    lines = out.splitlines()
    assert lines[0] == f"{1:6d}\tone"
    assert lines[1] == f"{2:6d}\ttwo"
    assert lines[2] == f"{3:6d}\tthree"


async def test_read_offset_and_limit(ws: LocalWorkspace) -> None:
    content = "\n".join(f"line{i}" for i in range(1, 11))
    (ws.root / "f.txt").write_text(content)
    out = await Read().run({"file_path": "f.txt", "offset": 3, "limit": 2}, ws)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("line3")
    assert lines[1].endswith("line4")


async def test_read_binary_file_errors(ws: LocalWorkspace) -> None:
    (ws.root / "bin.dat").write_bytes(b"\x00\x01\x02binarydata")
    with pytest.raises(ToolError):
        await Read().run({"file_path": "bin.dat"}, ws)


async def test_read_directory_suggests_glob(ws: LocalWorkspace) -> None:
    (ws.root / "sub").mkdir()
    with pytest.raises(ToolError, match="Glob"):
        await Read().run({"file_path": "sub"}, ws)


async def test_read_missing_file_errors(ws: LocalWorkspace) -> None:
    with pytest.raises(ToolError):
        await Read().run({"file_path": "nope.txt"}, ws)


async def test_read_too_large_file_errors(ws: LocalWorkspace, monkeypatch) -> None:
    monkeypatch.setattr(Read, "MAX_BYTES", 10)
    (ws.root / "big.txt").write_text("x" * 100)
    with pytest.raises(ToolError):
        await Read().run({"file_path": "big.txt"}, ws)


async def test_read_denies_env_file(ws: LocalWorkspace) -> None:
    (ws.root / ".env").write_text("SECRET=1")
    from anymodel_subagents.types import PolicyError

    with pytest.raises(PolicyError):
        await Read().run({"file_path": ".env"}, ws)


# --------------------------------------------------------------------------- Glob


async def test_glob_sorts_by_mtime_desc(ws: LocalWorkspace) -> None:
    import os
    import time

    (ws.root / "a.py").write_text("a")
    (ws.root / "b.py").write_text("b")
    (ws.root / "c.py").write_text("c")
    # Force distinct mtimes deterministically regardless of filesystem clock resolution.
    now = time.time()
    os.utime(ws.root / "a.py", (now - 20, now - 20))
    os.utime(ws.root / "b.py", (now - 10, now - 10))
    os.utime(ws.root / "c.py", (now, now))

    out = await Glob().run({"pattern": "*.py"}, ws)
    assert out.splitlines() == ["c.py", "b.py", "a.py"]


async def test_glob_drops_denied_and_dotgit(ws: LocalWorkspace) -> None:
    (ws.root / "keep.txt").write_text("x")
    (ws.root / ".env").write_text("SECRET=1")
    git_dir = ws.root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("x")

    out = await Glob().run({"pattern": "*"}, ws)
    results = out.splitlines()
    assert "keep.txt" in results
    assert ".env" not in results
    assert not any(".git" in r for r in results)


async def test_glob_drops_symlink_escaping_root(ws: LocalWorkspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws.root / "link.txt").symlink_to(outside)
    (ws.root / "normal.txt").write_text("hi")

    out = await Glob().run({"pattern": "*"}, ws)
    results = out.splitlines()
    assert "normal.txt" in results
    assert "link.txt" not in results


async def test_glob_no_matches(ws: LocalWorkspace) -> None:
    out = await Glob().run({"pattern": "*.nonexistent"}, ws)
    assert "No files found" in out


# --------------------------------------------------------------------------- Grep


async def test_grep_files_with_matches(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("def foo():\n    pass\n")
    (ws.root / "b.py").write_text("def bar():\n    pass\n")
    out = await Grep().run({"pattern": "def foo"}, ws)
    assert out.strip() == "a.py"


async def test_grep_content_mode_with_line_numbers_and_context(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("one\ntwo\nMATCH\nfour\nfive\n")
    out = await Grep().run({"pattern": "MATCH", "output_mode": "content", "-n": True, "-C": 1}, ws)
    lines = out.splitlines()
    assert "a.py-2-two" in lines
    assert "a.py:3:MATCH" in lines
    assert "a.py-4-four" in lines


async def test_grep_count_mode(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("x\nx\ny\nx\n")
    out = await Grep().run({"pattern": "x", "output_mode": "count"}, ws)
    assert out.strip() == "a.py:3"


async def test_grep_case_insensitive(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("Hello World\n")
    out = await Grep().run({"pattern": "hello", "-i": True}, ws)
    assert out.strip() == "a.py"
    out2 = await Grep().run({"pattern": "hello"}, ws)
    assert "No matches" in out2


async def test_grep_skips_env_file(ws: LocalWorkspace) -> None:
    (ws.root / ".env").write_text("SECRET_TOKEN=abc123\n")
    (ws.root / "code.py").write_text("# nothing here\n")
    out = await Grep().run({"pattern": "SECRET"}, ws)
    assert "No matches" in out


async def test_grep_respects_glob_filter(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("needle\n")
    (ws.root / "a.txt").write_text("needle\n")
    out = await Grep().run({"pattern": "needle", "glob": "*.py"}, ws)
    assert out.strip() == "a.py"


async def test_grep_invalid_regex_errors(ws: LocalWorkspace) -> None:
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "("}, ws)


async def test_grep_invalid_output_mode_errors(ws: LocalWorkspace) -> None:
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "x", "output_mode": "bogus"}, ws)


async def test_grep_head_limit_caps_output(ws: LocalWorkspace) -> None:
    for i in range(10):
        (ws.root / f"f{i}.py").write_text("needle\n")
    out = await Grep().run({"pattern": "needle", "head_limit": 3}, ws)
    assert len(out.splitlines()) == 3


async def test_grep_catastrophic_regex_times_out(ws: LocalWorkspace) -> None:
    # With the third-party `regex` module, the textbook (a+)+$ bomb at its
    # textbook size (n=30) now resolves in microseconds instead of freezing
    # for 60+ seconds under stdlib `re` -- the module's matcher just doesn't
    # suffer the same catastrophic blowup for this pattern. It still blows
    # up superlinearly for large enough input, though, so we scale the input
    # up until a single regex.search() call genuinely exceeds the search's
    # own timeout, to prove the timeout path itself still works as defense
    # in depth (for patterns/inputs the `regex` module doesn't defang).
    line = "a" * 2000 + "!"
    (ws.root / "evil.txt").write_text(line)
    grep = Grep(timeout_s=0.3, per_match_timeout_s=0.3)
    start = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        await grep.run({"pattern": r"(a+)+$"}, ws)
    elapsed = time.monotonic() - start
    assert elapsed <= 0.6  # <= 2x timeout_s


async def test_grep_timeout_does_not_block_event_loop(ws: LocalWorkspace) -> None:
    # Regression for the CRITICAL bug: stdlib re.search() holds the GIL for
    # the duration of a catastrophic match, so nothing else -- including the
    # asyncio event loop running on the same process -- can make progress
    # until it returns. A heartbeat coroutine ticking throughout the grep
    # call proves the loop stayed responsive instead of freezing.
    line = "a" * 2000 + "!"
    (ws.root / "evil.txt").write_text(line)
    grep = Grep(timeout_s=0.5, per_match_timeout_s=0.5)

    ticks = 0
    stop = False

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    hb_task = asyncio.create_task(heartbeat())
    try:
        with pytest.raises(ToolError, match="timed out"):
            await grep.run({"pattern": r"(a+)+$"}, ws)
    finally:
        stop = True
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    # A stalled loop would show up as `ticks` staying near 0 despite the
    # ~0.5s grep call.
    assert ticks >= 10


async def test_grep_timeout_does_not_leak_threads(ws: LocalWorkspace) -> None:
    # The old daemon-thread design spawned a brand-new thread per call that,
    # on timeout, stayed alive forever spinning inside re.search() -- a
    # thread leak proportional to the number of timed-out calls. The fixed
    # implementation relies solely on asyncio.to_thread's own bounded
    # executor, so repeated timeouts must not accumulate live threads.
    line = "a" * 2000 + "!"
    (ws.root / "evil.txt").write_text(line)
    grep = Grep(timeout_s=0.3, per_match_timeout_s=0.3)
    baseline = threading.active_count()
    for _ in range(3):
        with pytest.raises(ToolError, match="timed out"):
            await grep.run({"pattern": r"(a+)+$"}, ws)
    await asyncio.sleep(0.05)
    assert threading.active_count() <= baseline + 1


async def test_grep_head_limit_clamped_to_range(ws: LocalWorkspace) -> None:
    for i in range(3):
        (ws.root / f"f{i}.py").write_text("needle\n")
    out = await Grep().run({"pattern": "needle", "head_limit": 0}, ws)
    assert len(out.splitlines()) == 1  # clamped up to MIN_HEAD_LIMIT
    out2 = await Grep().run({"pattern": "needle", "head_limit": 10**9}, ws)
    assert len(out2.splitlines()) == 3  # unaffected; well under MAX_HEAD_LIMIT


async def test_grep_context_clamped_to_range(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("\n".join(str(i) for i in range(60)))
    out = await Grep().run({"pattern": "^30$", "output_mode": "content", "-C": 10**6}, ws)
    lines = [line for line in out.splitlines() if line != "--"]
    # context clamped to MAX_CONTEXT (20), so at most 41 lines (20 before + match + 20 after)
    assert len(lines) <= 41


async def test_grep_non_int_numeric_args_raise_tool_error(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("needle\n")
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "needle", "head_limit": "many"}, ws)
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "needle", "-C": "lots"}, ws)


async def test_grep_caps_total_output_bytes(ws: LocalWorkspace) -> None:
    long_line = "needle " + "x" * 2000
    content = "\n".join(long_line for _ in range(300))
    (ws.root / "big.txt").write_text(content)
    out = await Grep().run({"pattern": "needle", "output_mode": "content", "head_limit": 10**6}, ws)
    assert len(out.encode("utf-8")) <= Grep.MAX_OUTPUT_BYTES


async def test_grep_truncates_overlong_lines_before_matching(ws: LocalWorkspace) -> None:
    # A match past MAX_LINE_CHARS is invisible (the line is truncated before
    # matching), but the file still isn't skipped, and a match within the
    # truncation window is still found.
    line = "x" * (Grep.MAX_LINE_CHARS + 100) + "needle"
    (ws.root / "f.txt").write_text(line)
    out = await Grep().run({"pattern": "needle"}, ws)
    assert "No matches found" in out
    out2 = await Grep().run({"pattern": "x{10}"}, ws)
    assert out2.strip() == "f.txt"


async def test_read_offset_non_positive_clamped_to_one(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("one\ntwo\nthree\n")
    out = await Read().run({"file_path": "f.txt", "offset": 0}, ws)
    assert out.splitlines()[0] == f"{1:6d}\tone"
    out2 = await Read().run({"file_path": "f.txt", "offset": -5}, ws)
    assert out2.splitlines()[0] == f"{1:6d}\tone"


async def test_read_limit_clamped_to_max(ws: LocalWorkspace, monkeypatch) -> None:
    monkeypatch.setattr(Read, "MAX_LIMIT", 3)
    content = "\n".join(f"l{i}" for i in range(1, 11))
    (ws.root / "f.txt").write_text(content)
    out = await Read().run({"file_path": "f.txt", "limit": 1000}, ws)
    assert len(out.splitlines()) == 3


async def test_read_non_int_numeric_args_raise_tool_error(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("hi")
    with pytest.raises(ToolError):
        await Read().run({"file_path": "f.txt", "offset": "abc"}, ws)
    with pytest.raises(ToolError):
        await Read().run({"file_path": "f.txt", "limit": "abc"}, ws)


# --------------------------------------------------------------------------- Regular files / hardlinks


async def test_read_refuses_fifo(ws: LocalWorkspace) -> None:
    os.mkfifo(ws.root / "pipe")
    with pytest.raises(ToolError, match="regular file"):
        await Read().run({"file_path": "pipe"}, ws)


async def test_grep_skips_fifo(ws: LocalWorkspace) -> None:
    os.mkfifo(ws.root / "pipe")
    (ws.root / "normal.py").write_text("needle\n")
    out = await Grep().run({"pattern": "needle"}, ws)
    assert out.strip() == "normal.py"


async def test_read_refuses_hardlinked_file(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hello")
    os.link(target, ws.root / "alias.txt")
    with pytest.raises(PolicyError, match="hard-linked"):
        await Read().run({"file_path": "alias.txt"}, ws)


async def test_edit_refuses_hardlinked_file(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hello world")
    os.link(target, ws.root / "alias.txt")
    with pytest.raises(PolicyError, match="hard-linked"):
        await Edit().run({"file_path": "alias.txt", "old_string": "hello", "new_string": "hi"}, ws)


async def test_write_refuses_overwriting_hardlinked_file(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("hello")
    os.link(target, ws.root / "alias.txt")
    with pytest.raises(PolicyError, match="hard-linked"):
        await Write().run({"file_path": "alias.txt", "content": "new"}, ws)


async def test_grep_skips_hardlinked_file(ws: LocalWorkspace) -> None:
    target = ws.root / "real.txt"
    target.write_text("needle\n")
    os.link(target, ws.root / "alias.txt")
    (ws.root / "other.txt").write_text("needle\n")
    out = await Grep().run({"pattern": "needle"}, ws)
    results = out.splitlines()
    assert results == ["other.txt"]


# --------------------------------------------------------------------------- Glob bounded memory


async def test_glob_stops_collecting_once_bounded(ws: LocalWorkspace, monkeypatch) -> None:
    monkeypatch.setattr(Glob, "MAX_CANDIDATES", 5)
    monkeypatch.setattr(Glob, "MAX_RESULTS", 5)
    for i in range(20):
        (ws.root / f"f{i}.py").write_text("x")
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert len(out.splitlines()) == 5


# --------------------------------------------------------------------------- Write


async def test_write_creates_file_and_returns_confirmation(ws: LocalWorkspace) -> None:
    msg = await Write().run({"file_path": "new.txt", "content": "hello"}, ws)
    assert (ws.root / "new.txt").read_text() == "hello"
    assert "new.txt" in msg


async def test_write_creates_parent_dirs(ws: LocalWorkspace) -> None:
    await Write().run({"file_path": "a/b/c.txt", "content": "x"}, ws)
    assert (ws.root / "a" / "b" / "c.txt").read_text() == "x"


async def test_write_is_atomic_no_partial_file_left_behind(ws: LocalWorkspace) -> None:
    await Write().run({"file_path": "f.txt", "content": "v1"}, ws)
    await Write().run({"file_path": "f.txt", "content": "v2"}, ws)
    assert (ws.root / "f.txt").read_text() == "v2"
    leftovers = [p for p in ws.root.iterdir() if p.name.startswith(".f.txt")]
    assert leftovers == []


async def test_write_preserves_existing_file_mode(ws: LocalWorkspace) -> None:
    import os
    import stat

    path = ws.root / "f.txt"
    path.write_text("v1")
    os.chmod(path, 0o640)
    await Write().run({"file_path": "f.txt", "content": "v2"}, ws)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


async def test_write_size_cap(ws: LocalWorkspace, monkeypatch) -> None:
    monkeypatch.setattr(Write, "MAX_BYTES", 10)
    with pytest.raises(ToolError):
        await Write().run({"file_path": "f.txt", "content": "x" * 100}, ws)


async def test_write_denies_secrets_path(ws: LocalWorkspace) -> None:
    from anymodel_subagents.types import PolicyError

    with pytest.raises(PolicyError):
        await Write().run({"file_path": ".env", "content": "SECRET=1"}, ws)


async def test_write_denies_write_only_path(ws: LocalWorkspace) -> None:
    from anymodel_subagents.types import PolicyError

    with pytest.raises(PolicyError):
        await Write().run({"file_path": "CLAUDE.md", "content": "hi"}, ws)


# --------------------------------------------------------------------------- Edit


async def test_edit_simple_replace(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("hello world")
    await Edit().run({"file_path": "f.txt", "old_string": "world", "new_string": "there"}, ws)
    assert (ws.root / "f.txt").read_text() == "hello there"


async def test_edit_not_found_errors(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("hello world")
    with pytest.raises(ToolError, match="not found"):
        await Edit().run({"file_path": "f.txt", "old_string": "xyz", "new_string": "abc"}, ws)


async def test_edit_multiple_matches_without_replace_all_errors(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("foo foo foo")
    with pytest.raises(ToolError, match="3"):
        await Edit().run({"file_path": "f.txt", "old_string": "foo", "new_string": "bar"}, ws)


async def test_edit_replace_all(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("foo foo foo")
    await Edit().run(
        {"file_path": "f.txt", "old_string": "foo", "new_string": "bar", "replace_all": True}, ws
    )
    assert (ws.root / "f.txt").read_text() == "bar bar bar"


async def test_edit_identical_strings_errors(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("hello world")
    with pytest.raises(ToolError):
        await Edit().run({"file_path": "f.txt", "old_string": "world", "new_string": "world"}, ws)


async def test_edit_missing_file_errors(ws: LocalWorkspace) -> None:
    with pytest.raises(ToolError):
        await Edit().run({"file_path": "nope.txt", "old_string": "a", "new_string": "b"}, ws)


async def test_edit_denies_secrets_path(ws: LocalWorkspace) -> None:
    from anymodel_subagents.types import PolicyError

    with pytest.raises(PolicyError):
        await Edit().run({"file_path": ".env", "old_string": "a", "new_string": "b"}, ws)


# --------------------------------------------------------------------------- tools_for_mode


def test_tools_for_mode_read_only() -> None:
    tools = tools_for_mode("read-only")
    names = {t.name for t in tools}
    assert names == {"Read", "Grep", "Glob"}


def test_tools_for_mode_edit() -> None:
    tools = tools_for_mode("edit")
    names = {t.name for t in tools}
    assert names == {"Read", "Grep", "Glob", "Edit", "Write"}


def test_tools_for_mode_edit_bash_requires_policy() -> None:
    with pytest.raises(ValueError, match="bash_policy"):
        tools_for_mode("edit+bash")


def test_tools_for_mode_edit_bash_with_policy() -> None:
    from anymodel_subagents.tools import BashPolicy

    policy = BashPolicy(allow_prefixes=("echo",), allow_unsandboxed=False, state_dir=Path("/tmp"))
    tools = tools_for_mode("edit+bash", bash_policy=policy)
    names = {t.name for t in tools}
    assert names == {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}


def test_tools_for_mode_unknown_mode() -> None:
    with pytest.raises(ValueError):
        tools_for_mode("bogus")  # type: ignore[arg-type]
