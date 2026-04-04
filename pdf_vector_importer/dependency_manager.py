# -*- coding: utf-8 -*-
# dependency_manager.py — PyMuPDF dependency management
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Manages the PyMuPDF (fitz) dependency for the Blender addon.
Handles checking availability, installing to addon lib dir, and path setup.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def get_lib_dir() -> Path:
    """Return the addon's private lib/ directory for pip-installed packages."""
    addon_dir = Path(__file__).resolve().parent
    return addon_dir / "lib"


def ensure_lib_path() -> None:
    """Add the addon's lib/ directory to sys.path if not already present."""
    lib_dir = str(get_lib_dir())
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def check_pymupdf() -> bool:
    """Check whether PyMuPDF (fitz) is importable."""
    ensure_lib_path()
    try:
        import fitz  # noqa: F401

        return True
    except ImportError:
        return False


def install_pymupdf() -> bool:
    """
    Install PyMuPDF into the addon's lib/ directory.

    In Blender 3.x+, sys.executable points to the bundled Python binary.
    We use it directly with pip install --target.

    Returns True on success, False on failure.
    """
    lib_dir = get_lib_dir()
    lib_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable

    try:
        subprocess.check_call(
            [
                python_exe,
                "-m",
                "pip",
                "install",
                "--target",
                str(lib_dir),
                "--upgrade",
                "PyMuPDF",
            ],
            timeout=300,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False

    # Ensure the new path is available immediately
    ensure_lib_path()

    # Verify the install worked
    try:
        import importlib

        importlib.invalidate_caches()
        import fitz  # noqa: F401

        return True
    except ImportError:
        return False


def get_pymupdf_version() -> str:
    """Return the installed PyMuPDF version string, or empty string."""
    ensure_lib_path()
    try:
        import fitz

        return getattr(fitz, "version", ("unknown",))[0]
    except (ImportError, AttributeError, IndexError):
        return ""
