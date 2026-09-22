"""Regression tests for files.py: Glob walk bounds and forbidden characters
in write-target paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from anymodel_subagents.tools import Edit, Glob, LocalWorkspace, Read, Write
from anymodel_subagents.types import PolicyError


@pytest.fixture
def ws(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    return LocalWorkspace(root=root)


# ------------------------------------------------- Fix A: Glob walk is bounded


async def test_glob_deadline_stops_walk_and_says_so(ws: LocalWorkspace, monkeypatch) -> None:
    for i in range(20):
        (ws.root / f"f{i}.py").write_text("x")
    monkeypatch.setattr(Glob, "TIMEOUT_S", 0)
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert "incomplete" in out
    assert "f0.py" not in out
    assert "No files found" not in out


async def test_glob_visited_cap_stops_walk_and_says_so(ws: LocalWorkspace, monkeypatch) -> None:
    for i in range(20):
        (ws.root / f"f{i}.py").write_text("x")
    monkeypatch.setattr(Glob, "MAX_VISITED", 5)
    out = await Glob().run({"pattern": "*.py"}, ws)
    listed = out.splitlines()
    assert "incomplete" in listed[-1]
    py_lines = [line for line in listed if line.endswith(".py")]
    assert 0 < len(py_lines) < 20


async def test_glob_small_tree_has_no_cut_short_note(ws: LocalWorkspace) -> None:
    (ws.root / "a.py").write_text("x")
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert out.strip() == "a.py"
    assert "incomplete" not in out


async def test_glob_visits_cap_covers_nonmatching_huge_tree(
    ws: LocalWorkspace, monkeypatch
) -> None:
    """A pattern matching nothing must still stop once the cap is hit."""
    for i in range(20):
        (ws.root / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(Glob, "MAX_VISITED", 5)
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert "incomplete" in out
    assert "No files found" not in out


def _empty_dir_tree(root: Path) -> None:
    """~300 empty directories: 30 siblings, each with 10 empty children."""
    for i in range(30):
        for j in range(10):
            (root / f"d{i:02d}" / f"s{j:02d}").mkdir(parents=True)


async def test_glob_visited_cap_triggers_on_empty_dir_tree(ws: LocalWorkspace, monkeypatch) -> None:
    """A tree of empty directories must still hit the visited cap (no files ever)."""
    _empty_dir_tree(ws.root)
    monkeypatch.setattr(Glob, "MAX_VISITED", 50)
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert "incomplete" in out
    assert "No files found" not in out


async def test_glob_expired_deadline_triggers_on_empty_dir_tree(
    ws: LocalWorkspace, monkeypatch
) -> None:
    """An expired deadline must cut short even a tree with no files at all."""
    _empty_dir_tree(ws.root)
    monkeypatch.setattr(Glob, "TIMEOUT_S", 0)
    out = await Glob().run({"pattern": "*.py"}, ws)
    assert "incomplete" in out
    assert "No files found" not in out


def _patch_counting_scandir(monkeypatch) -> list[int]:
    """Wrap os.scandir (what files.py's walk uses) and record entries pulled.

    One token is appended per entry actually pulled off a listing, so
    `len(pulled)` is how much of the tree Glob really examined. os.walk
    materialises a whole directory's listing before any cap is checked; the
    lazy stack walk must stop pulling well before that.
    """
    pulled: list[int] = []
    real_scandir = os.scandir

    class _CountingScan:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._inner)  # StopIteration (exhaustion) is not a pull
            pulled.append(1)  # count this yielded entry
            return entry

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return self._inner.__exit__(*exc)

    def counting_scandir(path):
        return _CountingScan(real_scandir(path))

    monkeypatch.setattr(os, "scandir", counting_scandir)
    return pulled


async def test_glob_lazy_walk_pulls_no_more_than_cap_plus_one_entries(
    ws: LocalWorkspace, monkeypatch
) -> None:
    """One huge directory must not be listed past the visited cap."""
    bulk = ws.root / "bulk"
    bulk.mkdir()
    for i in range(5000):
        (bulk / f"f{i:04d}.bin").write_text("")
    cap = 100
    monkeypatch.setattr(Glob, "MAX_VISITED", cap)
    pulled = _patch_counting_scandir(monkeypatch)
    out = await Glob().run({"pattern": "*.nomatch"}, ws)
    assert "incomplete" in out
    assert len(pulled) <= cap + 1


async def test_glob_visited_cap_counts_each_directory_entry_once(
    ws: LocalWorkspace, monkeypatch
) -> None:
    """10 dirs + 40 files = 50 entries: cap 60 fits, cap 40 does not.

    The old accounting (`1 + len(dirnames)` per walk step) counted every
    subdirectory twice, so a cap of 60 wrongly cut this tree short.
    """
    for i in range(10):
        d = ws.root / f"d{i:02d}"
        d.mkdir()
        for j in range(4):
            (d / f"f{j}.txt").write_text("x")

    monkeypatch.setattr(Glob, "MAX_VISITED", 60)
    out = await Glob().run({"pattern": "*.txt"}, ws)
    lines = out.splitlines()
    assert len(lines) == 40
    assert all(line.endswith(".txt") for line in lines)
    assert "incomplete" not in out

    monkeypatch.setattr(Glob, "MAX_VISITED", 40)
    out = await Glob().run({"pattern": "*.txt"}, ws)
    assert "incomplete" in out


# ---------------------------------- Fix B: forbidden characters in write paths

FORBIDDEN_PATHS = {
    "control": "bad\x01name.txt",
    "del": "bad\x7fname.txt",
    "bidi-202e": "bad\u202ename.txt",
    "isolate-2066": "bad\u2066name.txt",
    "zero-width-200b": "bad\u200bname.txt",
    "zero-width-200d": "bad\u200dname.txt",
    "bom-feff": "bad\ufeffname.txt",
    "angle-lt": "bad<name.txt",
    "angle-gt": "bad>name.txt",
    "nested-component": "ok/\u202edir/f.txt",
}


async def test_write_refuses_forbidden_path_characters(ws: LocalWorkspace) -> None:
    for label, path in FORBIDDEN_PATHS.items():
        with pytest.raises(PolicyError) as excinfo:
            await Write().run({"file_path": path, "content": "x"}, ws)
        assert "path" in str(excinfo.value), label
        assert "bad" not in str(excinfo.value), label


async def test_edit_refuses_forbidden_path_characters(ws: LocalWorkspace) -> None:
    for path in FORBIDDEN_PATHS.values():
        with pytest.raises(PolicyError):
            await Edit().run({"file_path": path, "old_string": "a", "new_string": "b"}, ws)


async def test_write_allows_ordinary_names(ws: LocalWorkspace) -> None:
    msg = await Write().run({"file_path": "src/ok_name-1.py", "content": "x = 1\n"}, ws)
    assert (ws.root / "src" / "ok_name-1.py").read_text() == "x = 1\n"
    assert "src/ok_name-1.py" in msg


async def test_write_allows_space_in_name(ws: LocalWorkspace) -> None:
    await Write().run({"file_path": "my notes.txt", "content": "hi\n"}, ws)
    assert (ws.root / "my notes.txt").read_text() == "hi\n"


async def test_edit_allows_ordinary_names(ws: LocalWorkspace) -> None:
    (ws.root / "src").mkdir()
    (ws.root / "src" / "ok_name-1.py").write_text("x = 1\n")
    await Edit().run({"file_path": "src/ok_name-1.py", "old_string": "1", "new_string": "2"}, ws)
    assert (ws.root / "src" / "ok_name-1.py").read_text() == "x = 2\n"


async def test_read_still_allows_tricky_named_existing_file(ws: LocalWorkspace) -> None:
    (ws.root / "bad\u200bname.txt").write_text("content\n")
    out = await Read().run({"file_path": "bad\u200bname.txt"}, ws)
    assert "content" in out
