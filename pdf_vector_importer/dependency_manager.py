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
        import pymupdf as fitz  # noqa: F401  — PyMuPDF >= 1.24 preferred name
        return True
    except ImportError:
        pass
    try:
        import fitz  # noqa: F401  — Legacy fallback
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
                "PyMuPDF>=1.24,<2.0",
            ],
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[PDF Vector Importer] pip install failed (exit code {exc.returncode}).")
        print(f"[PDF Vector Importer] Command: {exc.cmd}")
        print("[PDF Vector Importer] Check that Blender's bundled Python has network "
              "access and pip is available.")
        return False
    except FileNotFoundError:
        print(f"[PDF Vector Importer] Python executable not found: {python_exe}")
        print("[PDF Vector Importer] Cannot install PyMuPDF without a valid Python binary.")
        return False
    except OSError as exc:
        print(f"[PDF Vector Importer] OS error during pip install: {exc}")
        return False

    # Ensure the new path is available immediately
    ensure_lib_path()

    # Verify the install worked
    try:
        import importlib

        importlib.invalidate_caches()
        try:
            import pymupdf as fitz  # noqa: F401
        except ImportError:
            import fitz  # noqa: F401
        return True
    except ImportError:
        return False


def get_pymupdf_version() -> str:
    """Return the installed PyMuPDF version string, or empty string."""
    ensure_lib_path()
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        return getattr(fitz, "version", ("unknown",))[0]
    except (ImportError, AttributeError, IndexError):
        return ""


def print_diagnostics() -> None:
    """Print first-run diagnostic info: Blender version, Python version, PyMuPDF version."""
    print("[PDF Vector Importer] --- Dependency Diagnostics ---")

    # Python version
    print(f"[PDF Vector Importer] Python: {sys.version}")

    # Blender version (may not be available outside Blender)
    try:
        import bpy
        blender_ver = ".".join(str(v) for v in bpy.app.version)
        print(f"[PDF Vector Importer] Blender: {blender_ver}")
    except Exception:
        print("[PDF Vector Importer] Blender: not available (headless/CLI mode)")

    # PyMuPDF version
    pymupdf_ver = get_pymupdf_version()
    if pymupdf_ver:
        print(f"[PDF Vector Importer] PyMuPDF: {pymupdf_ver}")
    else:
        print("[PDF Vector Importer] PyMuPDF: NOT INSTALLED")

    print("[PDF Vector Importer] --- End Diagnostics ---")
