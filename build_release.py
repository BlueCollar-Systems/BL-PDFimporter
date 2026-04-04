#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# build_release.py — Build a Blender addon ZIP package
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Creates a distributable ZIP for the pdf_vector_importer Blender addon.
Reads the version from __init__.py bl_info and packages the addon directory.

Usage:
    python build_release.py

Output:
    dist/pdf_vector_importer_vX.Y.Z.zip
"""
from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
PKG = ROOT / "pdf_vector_importer"

# Patterns to exclude from the release zip
_EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _read_version() -> str:
    """Extract version tuple from __init__.py bl_info dict."""
    init_path = PKG / "__init__.py"
    text = init_path.read_text(encoding="utf-8")
    match = re.search(r'"version"\s*:\s*\((\d+),\s*(\d+),\s*(\d+)\)', text)
    if match:
        return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
    return "0.0.0"


def _should_exclude(path: Path) -> bool:
    """Return True if a file should be excluded from the zip."""
    for part in path.parts:
        if part in _EXCLUDE_DIRS:
            return True
    if path.suffix in _EXCLUDE_SUFFIXES:
        return True
    return False


def main() -> int:
    version = _read_version()
    out_path = DIST / f"pdf_vector_importer_v{version}.zip"

    DIST.mkdir(parents=True, exist_ok=True)

    count = 0
    with ZipFile(out_path, "w", ZIP_DEFLATED) as zf:
        # Package all addon files
        for path in sorted(PKG.rglob("*")):
            if path.is_dir():
                continue
            if _should_exclude(path):
                continue
            arcname = str(path.relative_to(ROOT))
            zf.write(path, arcname)
            count += 1

        # Include project metadata at the top level
        for meta in ("README.md", "LICENSE"):
            meta_path = ROOT / meta
            if meta_path.exists():
                zf.write(meta_path, meta)
                count += 1

    print(f"Built: {out_path}  ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
