# -*- coding: utf-8 -*-
# dependency_manager.py — PyMuPDF dependency management
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Manages the PyMuPDF (fitz) dependency for the Blender addon.
Handles checking availability, installing to addon lib dir, and path setup.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path


def get_lib_dir() -> Path:
    """Return the addon's private lib/ directory for pip-installed packages."""
    addon_dir = Path(__file__).resolve().parent
    return addon_dir / "lib"


def ensure_lib_path() -> None:
    """Add bundled runtime paths to sys.path if not already present."""
    repair_vendored_pymupdf()
    addon_dir = str(Path(__file__).resolve().parent)
    if addon_dir not in sys.path:
        sys.path.insert(0, addon_dir)
    lib_dir = str(get_lib_dir())
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def repair_vendored_pymupdf() -> bool:
    """
    Restore PyMuPDF's pure-Python helper when an install/update left it behind.

    Some Blender add-on installs observed in the field contained the compiled
    PyMuPDF extension files but missed ``pymupdf/extra.py``.  Importing PyMuPDF
    then fails from a partially initialized module before preferences can help.
    The release zip carries a root-level backup so this can self-heal.
    """
    addon_dir = Path(__file__).resolve().parent
    pymupdf_dir = addon_dir / "lib" / "pymupdf"
    missing_helper = pymupdf_dir / "extra.py"
    backup = addon_dir / "_vendored_pymupdf_extra.py"
    compiled_helper = pymupdf_dir / "_extra.pyd"

    if missing_helper.exists():
        return False
    if not backup.is_file() or not compiled_helper.exists():
        return False
    try:
        shutil.copy2(backup, missing_helper)
        print(f"[PDF Vector Importer] Repaired vendored PyMuPDF helper: {missing_helper}")
        return True
    except OSError as exc:
        print(f"[PDF Vector Importer] Could not repair vendored PyMuPDF helper: {exc}")
        return False


def _purge_stale_pymupdf_modules() -> None:
    for name in list(sys.modules):
        if name == "fitz" or name == "pymupdf" or name.startswith("pymupdf."):
            del sys.modules[name]
    importlib.invalidate_caches()


def _import_error_detail() -> str:
    lib_dir = get_lib_dir()
    ensure_lib_path()
    try:
        from .pdfcadcore.fitz_loader import import_fitz

        import_fitz(prefer_lib_dir=str(lib_dir))
        return ""
    except ImportError as exc:
        return str(exc)
    except OSError as exc:
        return str(exc)


def check_pymupdf() -> bool:
    """Check whether PyMuPDF (fitz) is importable and exposes ``open``."""
    return not _import_error_detail()


def _clear_vendored_pymupdf_binaries() -> None:
    """Remove ABI-mismatched vendored wheels before a fresh pip install."""
    lib_dir = get_lib_dir()
    for rel in (
        Path("pymupdf"),
        Path("fitz"),
    ):
        target = lib_dir / rel
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for dist_info in lib_dir.glob("pymupdf-*.dist-info"):
        shutil.rmtree(dist_info, ignore_errors=True)
    for dist_info in lib_dir.glob("PyMuPDF-*.dist-info"):
        shutil.rmtree(dist_info, ignore_errors=True)


def install_pymupdf(*, clear_vendored: bool = True) -> bool:
    """
    Install PyMuPDF into the addon's lib/ directory.

    In Blender 3.x+, sys.executable points to the bundled Python binary.
    We use it directly with pip install --target.

    Returns True on success, False on failure.
    """
    lib_dir = get_lib_dir()
    lib_dir.mkdir(parents=True, exist_ok=True)

    if clear_vendored:
        _clear_vendored_pymupdf_binaries()

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
        print(
            "[PDF Vector Importer] Check that Blender's bundled Python has network "
            "access and pip is available."
        )
        return False
    except FileNotFoundError:
        print(f"[PDF Vector Importer] Python executable not found: {python_exe}")
        print("[PDF Vector Importer] Cannot install PyMuPDF without a valid Python binary.")
        return False
    except OSError as exc:
        print(f"[PDF Vector Importer] OS error during pip install: {exc}")
        return False

    _purge_stale_pymupdf_modules()
    ensure_lib_path()
    return check_pymupdf()


def ensure_pymupdf_runtime(*, auto_install: bool = False) -> bool:
    """
    Verify PyMuPDF can load in the current Blender Python process.

    When *auto_install* is True and import fails (common on Blender 5.x when
    vendored cp311 wheels ship with cp312), attempt a pip install into lib/.
    """
    if check_pymupdf():
        return True
    detail = _import_error_detail()
    if detail:
        print(f"[PDF Vector Importer] PyMuPDF import failed: {detail}")
    if not auto_install:
        return False
    print("[PDF Vector Importer] Attempting automatic PyMuPDF install for this Blender Python...")
    return install_pymupdf(clear_vendored=True)


def get_pymupdf_version() -> str:
    """Return the installed PyMuPDF version string, or empty string."""
    ensure_lib_path()
    try:
        from .pdfcadcore.fitz_loader import import_fitz

        fitz = import_fitz(prefer_lib_dir=str(get_lib_dir()))
        version = getattr(fitz, "__version__", None)
        if version is None:
            version = getattr(fitz, "version", None)
        if isinstance(version, str):
            return version
        if isinstance(version, (tuple, list)) and version:
            return str(version[0])
        return "unknown"
    except (ImportError, AttributeError, IndexError, OSError):
        return ""


def runtime_diagnostics() -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    detail = _import_error_detail()
    if detail:
        return f"Python {py} — PyMuPDF NOT available ({detail})"
    ver = get_pymupdf_version()
    return f"Python {py} — PyMuPDF {ver or 'unknown'}"


def host_python_meets_floor() -> bool:
    """Return True when the host CPython can load the vendored cp310-abi3 wheel."""
    return sys.version_info[:2] >= (3, 10)


def report_host_python_floor() -> bool:
    """Emit one actionable sentence when the host Python is below the package floor."""
    if host_python_meets_floor():
        return True
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(
        "[PDF Vector Importer] Host Python "
        f"{py} is below the packaged floor (requires Python >=3.10 / Blender 3.1+). "
        "The vendored PyMuPDF wheel cannot load on this host."
    )
    return False


def print_diagnostics() -> None:
    """Print first-run diagnostic info: Blender version, Python version, PyMuPDF version."""
    print("[PDF Vector Importer] --- Dependency Diagnostics ---")

    print(f"[PDF Vector Importer] Python: {sys.version}")

    try:
        import bpy

        blender_ver = ".".join(str(v) for v in bpy.app.version)
        print(f"[PDF Vector Importer] Blender: {blender_ver}")
    except Exception:
        print("[PDF Vector Importer] Blender: not available (headless/CLI mode)")

    print(f"[PDF Vector Importer] {runtime_diagnostics()}")
    report_host_python_floor()
    print("[PDF Vector Importer] --- End Diagnostics ---")
