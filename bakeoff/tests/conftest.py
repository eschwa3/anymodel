"""Shared setup for the bake-off harness's own verification suite.

Run with `uv run pytest bakeoff/tests -q` from the repo root. Not collected
by the project's main `uv run pytest` (its `testpaths` is `["tests"]` at the
repo root only).
"""

from __future__ import annotations

import sys
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BAKEOFF_DIR.parent

for p in (str(BAKEOFF_DIR), str(REPO_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)
