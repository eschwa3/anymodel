"""Regression tests for files.py hardening: regex-compile cost, atomic-write
TOCTOU on the parent directory, and untrusted-argument coercion."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from anymodel_subagents.tools import Grep, LocalWorkspace, Read, Write
from anymodel_subagents.tools import files as files_mod
from anymodel_subagents.types import PolicyError, ToolError


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


# --------------------------------------------------------------- Fix A: regex cost

# ~2 GB of memory if ever compiled; this test must never let it reach regex.
PATHOLOGICAL = "(?:(?:(?:a{1000}){1000}){1000}){1000}"


async def test_grep_refuses_pathological_pattern_fast(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("hello\n")
    start = time.monotonic()
    with pytest.raises(ToolError):
        await Grep().run({"pattern": PATHOLOGICAL}, ws)
    assert time.monotonic() - start < 2.0


async def test_grep_refuses_single_huge_bound(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("aaa\n")
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "a{1001}"}, ws)


async def test_grep_refuses_product_of_bounds(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("aaa\n")
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "a{100}b{100}c{100}d{100}"}, ws)


async def test_grep_refuses_overlong_pattern(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("aaa\n")
    with pytest.raises(ToolError):
        await Grep().run({"pattern": "a" * 1001}, ws)


async def test_grep_still_accepts_ordinary_patterns(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("aaa\n123-45\nfoo bar\n")
    for pattern in ("a{3}", r"\d{1,4}-\d{2}", "foo.*bar"):
        out = await Grep().run({"pattern": pattern}, ws)
        assert "No matches found" not in out, pattern


async def test_grep_open_ended_repetition_still_works(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("aaab\n")
    out = await Grep().run({"pattern": "a{3,}"}, ws)
    assert out.strip() == "f.txt"


# ------------------------------------------------------- Fix B: atomic-write TOCTOU


async def test_atomic_write_refuses_parent_swapped_during_mkstemp(
    ws: LocalWorkspace, tmp_path: Path, monkeypatch
) -> None:
    """Swap the write target's parent for an outside symlink inside mkstemp.

    The temp file then lands in the outside directory; the re-check before
    os.replace must refuse and clean it up, leaving nothing outside.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = ws.root / "inner"
    parent.mkdir()
    real_mkstemp = tempfile.mkstemp
    swapped: list[Path] = []

    def swapped_mkstemp(*args, **kwargs):
        bak = parent.with_name("inner.bak")
        os.rename(parent, bak)
        os.symlink(outside, parent)
        swapped.append(bak)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(files_mod.tempfile, "mkstemp", swapped_mkstemp)
    try:
        with pytest.raises(PolicyError):
            await Write().run({"file_path": "inner/target.txt", "content": "secret"}, ws)
    finally:
        if swapped:
            parent.unlink()
            swapped[0].rename(parent)

    assert not any(outside.iterdir()), "content escaped the workspace"
    assert not (parent / "target.txt").exists()


async def test_atomic_write_leaves_workspace_file_intact_on_refusal(
    ws: LocalWorkspace, tmp_path: Path, monkeypatch
) -> None:
    """Same swap, but via Edit on an existing file: the original file must
    survive untouched."""
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = ws.root / "inner"
    parent.mkdir()
    (parent / "note.txt").write_text("original\n")
    real_mkstemp = tempfile.mkstemp

    def swapped_mkstemp(*args, **kwargs):
        bak = parent.with_name("inner.bak")
        os.rename(parent, bak)
        os.symlink(outside, parent)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(files_mod.tempfile, "mkstemp", swapped_mkstemp)
    from anymodel_subagents.tools import Edit

    try:
        with pytest.raises(PolicyError):
            await Edit().run(
                {
                    "file_path": "inner/note.txt",
                    "old_string": "original",
                    "new_string": "edited",
                },
                ws,
            )
    finally:
        if parent.is_symlink():
            parent.unlink()
            parent.with_name("inner.bak").rename(parent)

    assert not any(outside.iterdir())
    assert (parent / "note.txt").read_text() == "original\n"


# ------------------------------------------------------ Fix C: bad argument types


async def test_read_rejects_infinite_offset(ws: LocalWorkspace) -> None:
    (ws.root / "f.txt").write_text("one\n")
    with pytest.raises(ToolError):
        await Read().run({"file_path": "f.txt", "offset": float("inf")}, ws)


async def test_write_rejects_none_content(ws: LocalWorkspace) -> None:
    with pytest.raises(ToolError):
        await Write().run({"file_path": "f.txt", "content": None}, ws)
