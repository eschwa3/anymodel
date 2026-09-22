"""AST-based static checks used to score the R5 (bulk-migration) task.

Scans a `jobsched` tree for any remaining call to the deprecated
`jobsched.utils.time.now` (however it got imported: bare, aliased, or via
`from jobsched.utils import time` / `import jobsched.utils.time as x`
module-qualified access), excluding an explicitly exempt set of files.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

DEPRECATED_MODULE = "jobsched.utils.time"
DEPRECATED_FUNC = "now"

# The one call site the task explicitly says must NOT be migrated.
EXEMPT_FILES = frozenset({"jobsched/reports/legacy_export.py"})


@dataclass
class Finding:
    file: str
    line: int
    snippet: str


class _ImportBindings(ast.NodeVisitor):
    """Collects, for one module, which local names refer to the deprecated
    function directly (call as `name()`) vs. which local names refer to the
    `jobsched.utils.time` module itself (call as `name.now()`).
    """

    def __init__(self) -> None:
        self.func_names: set[str] = set()
        self.module_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == DEPRECATED_MODULE:
                self.module_names.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            if module == DEPRECATED_MODULE and alias.name == DEPRECATED_FUNC:
                self.func_names.add(local)
            elif module == "jobsched.utils" and alias.name == "time":
                self.module_names.add(local)
        self.generic_visit(node)


class _CallFinder(ast.NodeVisitor):
    def __init__(self, func_names: set[str], module_names: set[str]) -> None:
        self.func_names = func_names
        self.module_names = module_names
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if (
            isinstance(fn, ast.Name)
            and fn.id in self.func_names
            or (
                isinstance(fn, ast.Attribute)
                and fn.attr == DEPRECATED_FUNC
                and isinstance(fn.value, ast.Name)
                and fn.value.id in self.module_names
            )
        ):
            self.calls.append(node)
        self.generic_visit(node)


def find_deprecated_now_calls(
    root: Path, exempt_files: frozenset[str] = EXEMPT_FILES
) -> list[Finding]:
    """Find every call to the deprecated `now()` under `root/jobsched`.

    `root` is the repo root (containing `jobsched/`); paths in the result
    and in `exempt_files` are POSIX, relative to `root`.
    """
    findings: list[Finding] = []
    package_dir = root / "jobsched"
    if not package_dir.is_dir():
        return findings

    for path in sorted(package_dir.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in exempt_files:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (OSError, SyntaxError):
            continue

        bindings = _ImportBindings()
        bindings.visit(tree)
        if not bindings.func_names and not bindings.module_names:
            continue

        finder = _CallFinder(bindings.func_names, bindings.module_names)
        finder.visit(tree)
        lines = source.splitlines()
        for call in finder.calls:
            lineno = call.lineno
            snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            findings.append(Finding(file=rel, line=lineno, snippet=snippet))

    return findings


def exempt_file_unchanged(root: Path, reference_root: Path, rel_path: str) -> bool:
    """True if `rel_path` under `root` is byte-identical to the same path
    under `reference_root` (used to confirm the must-not-change site was
    left alone).
    """
    a = root / rel_path
    b = reference_root / rel_path
    if not a.is_file() or not b.is_file():
        return False
    return a.read_bytes() == b.read_bytes()
