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
    dist/Blender-PDF-Importer_vX.Y.Z.zip
"""
from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
PKG = ROOT / "pdf_vector_importer"
LIB_DIR = PKG / "lib"

# Patterns to exclude from the release zip
_EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache", "_archived"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644
_VENDORED_LIB = PKG / "lib"
_REQUIRED_RUNTIME_FILES = (
    _VENDORED_LIB / "pymupdf" / "__init__.py",
    _VENDORED_LIB / "pymupdf" / "extra.py",
    _VENDORED_LIB / "pymupdf" / "_extra.pyd",
    _VENDORED_LIB / "pymupdf" / "_mupdf.pyd",
    _VENDORED_LIB / "pymupdf" / "mupdfcpp64.dll",
    PKG / "_vendored_pymupdf_extra.py",
    _VENDORED_LIB / "fontTools" / "ttLib" / "__init__.py",
    _VENDORED_LIB / "fontTools" / "cffLib" / "__init__.py",
    _VENDORED_LIB / "fonttools-4.60.2.dist-info" / "METADATA",
    _VENDORED_LIB / "fonttools-4.60.2.dist-info" / "WHEEL",
    _VENDORED_LIB / "fonttools-4.60.2.dist-info" / "licenses" / "LICENSE",
    _VENDORED_LIB / "fonttools-4.60.2.dist-info" / "licenses" / "LICENSE.external",
)


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
    normalized_parts = tuple(part.casefold() for part in path.parts)
    if any(
        normalized_parts[index : index + 2] == ("lib", "bin")
        for index in range(len(normalized_parts) - 1)
    ):
        return True
    if path.name.casefold() == "record" and any(
        part.endswith(".dist-info") for part in normalized_parts
    ):
        return True
    if path.suffix in _EXCLUDE_SUFFIXES:
        return True
    return False


def _write_deterministic_file(
    zf: ZipFile,
    source_path: Path,
    archive_name: str,
) -> None:
    """Write one file with checkout-independent ZIP metadata."""
    info = ZipInfo(archive_name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _ZIP_FILE_MODE << 16
    zf.writestr(
        info,
        source_path.read_bytes(),
        compress_type=ZIP_DEFLATED,
        compresslevel=9,
    )


def _write_deterministic_bytes(zf: ZipFile, data: bytes, archive_name: str) -> None:
    info = ZipInfo(archive_name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _ZIP_FILE_MODE << 16
    zf.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)


def _release_source_commit() -> str:
    for key in ("BC_RELEASE_SOURCE_SHA", "GITHUB_SHA"):
        value = str(os.environ.get(key) or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    value = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise RuntimeError("Could not resolve an exact 40-character release source SHA")
    return value.lower()


def _verify_vendored_pymupdf() -> None:
    """Ensure release ZIPs include their private offline runtimes."""
    _verify_vendored_runtime()
    fonttools_code = (
        "import sys; "
        f"sys.path.insert(0, r'{LIB_DIR}'); "
        "import fontTools; "
        "from fontTools.cffLib import CFFFontSet; "
        "from fontTools.ttLib import TTFont; "
        "print(fontTools.__version__)"
    )
    fonttools_proc = subprocess.run(
        [sys.executable, "-S", "-c", fonttools_code],
        capture_output=True,
        text=True,
    )
    if fonttools_proc.returncode != 0 or fonttools_proc.stdout.strip() != "4.60.2":
        raise RuntimeError(
            "Vendored FontTools 4.60.2 could not be imported from "
            "pdf_vector_importer/lib. "
            f"stderr: {fonttools_proc.stderr.strip()}"
        )
    if sys.platform != "win32":
        print(
            "Vendored PyMuPDF Windows runtime files present; "
            f"skipping binary import check on {sys.platform}."
        )
        return
    code = (
        "import sys; "
        f"sys.path.insert(0, r'{LIB_DIR}'); "
        "import pymupdf as fitz; "
        "print(getattr(fitz, '__version__', '') or getattr(fitz, 'VersionBind', ''))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            "Vendored PyMuPDF could not be imported from pdf_vector_importer/lib. "
            f"stderr: {proc.stderr.strip()}"
        )


def _prune_vendored_pymupdf() -> None:
    for rel in (
        Path("pymupdf") / "mupdf-devel",
    ):
        path = _VENDORED_LIB / rel
        if path.exists():
            shutil.rmtree(path)


def _verify_vendored_runtime() -> None:
    missing = [path for path in _REQUIRED_RUNTIME_FILES if not path.exists()]
    dist_info = list(_VENDORED_LIB.glob("pymupdf-*.dist-info/COPYING"))
    if missing or not dist_info:
        details = [str(path.relative_to(ROOT)) for path in missing]
        if not dist_info:
            details.append("pdf_vector_importer/lib/pymupdf-*.dist-info/COPYING")
        raise RuntimeError(
            "Release build requires bundled offline runtime files. Missing: "
            + ", ".join(details)
        )


def main() -> int:
    version = _read_version()
    out_path = DIST / f"Blender-PDF-Importer_v{version}.zip"

    DIST.mkdir(parents=True, exist_ok=True)
    _verify_vendored_pymupdf()
    _prune_vendored_pymupdf()

    release_files = []
    for path in PKG.rglob("*"):
        if path.is_file() and not _should_exclude(path):
            release_files.append((path.relative_to(ROOT).as_posix(), path.read_bytes()))

    for meta in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        meta_path = ROOT / meta
        if meta_path.exists():
            release_files.append((meta, meta_path.read_bytes()))

    from pdf_vector_importer.build_identity import create_release_identity

    package_entries = [
        (name, data)
        for name, data in release_files
        if name.startswith("pdf_vector_importer/")
        and not name.endswith("/_release_identity.json")
    ]
    identity = create_release_identity(
        importer_version=version,
        source_commit=_release_source_commit(),
        source_tag=f"v{version}",
        package_entries=package_entries,
    )
    release_files = [
        (name, data)
        for name, data in release_files
        if not name.endswith("/_release_identity.json")
    ]
    release_files.append((
        "pdf_vector_importer/_release_identity.json",
        (json.dumps(identity, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    ))

    release_files.sort(key=lambda item: item[0])
    with ZipFile(out_path, "w") as zf:
        for archive_name, data in release_files:
            _write_deterministic_bytes(zf, data, archive_name)

    print(f"Built: {out_path}  ({len(release_files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
