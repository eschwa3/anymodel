"""Make `inventory_lib` importable regardless of where pytest's rootdir
ends up (repo root, this fixture dir, or a copied temp dir)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
