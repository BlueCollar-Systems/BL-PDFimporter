"""Pytest bootstrap: repo root on sys.path; cache under %TEMP% when needed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
root_s = str(ROOT)
if root_s not in sys.path:
    sys.path.insert(0, root_s)

# Avoid permission errors on a read-only or locked .pytest_cache in the repo.
if os.environ.get("PYTEST_CACHE_DIR") is None:
    cache = Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "pytest-cache-bl-pdfimporter"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_CACHE_DIR"] = str(cache)
