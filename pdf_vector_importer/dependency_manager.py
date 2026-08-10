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
import os
import shutil
import subprocess
import sys
import uuid
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


def _vendored_pymupdf_paths() -> "list[Path]":
    lib_dir = get_lib_dir()
    paths = []
    seen = set()
    candidates = [lib_dir / "pymupdf", lib_dir / "fitz"]
    # Both patterns match the same dist-info on case-insensitive filesystems
    # (Windows); dedupe by resolved path so a path is never quarantined twice --
    # the second pass would otherwise delete the first pass's quarantine.
    for pattern in ("pymupdf-*.dist-info", "PyMuPDF-*.dist-info"):
        candidates.extend(lib_dir.glob(pattern))
    for target in candidates:
        if not target.exists():
            continue
        key = os.path.normcase(str(target.resolve()))
        if key in seen:
            continue
        seen.add(key)
        paths.append(target)
    return paths


def _quarantine_vendored_pymupdf() -> "tuple[list[tuple[Path, Path]], list[Path]]":
    """Move the vendored wheels aside so a failed install can restore them.

    Returns ``(moved, unsecured)``. The caller MUST abort when *unsecured* is
    non-empty.

    Three defects an independent review reproduced by execution shaped this:

    * The old code rmtree'd the bundle BEFORE the pip call, so an offline
      machine ended up with no PyMuPDF at all. Renaming instead keeps the
      known-good tree until the install actually succeeds.
    * A pre-existing ``.quarantine`` directory was rmtree'd on entry. After a
      failed restore that stale directory is the ONLY surviving copy, so
      deleting it turned a recoverable orphan into permanent loss. The
      quarantine name is now unique per run and we never touch one we did not
      create.
    * A failed rename used to be swallowed with "let pip --upgrade overwrite
      what it can". That reasoning is wrong: ``pip install --target --upgrade``
      rmtree's the destination directory before installing, so pip is precisely
      the thing that destroys an unsecured bundle. Failures are now reported so
      the caller can refuse to run pip at all.
    """
    moved: "list[tuple[Path, Path]]" = []
    unsecured: "list[Path]" = []
    token = uuid.uuid4().hex[:8]
    for target in _vendored_pymupdf_paths():
        quarantine = target.with_name("%s.quarantine-%s" % (target.name, token))
        try:
            target.rename(quarantine)
            moved.append((target, quarantine))
        except OSError as exc:
            # Windows holds a directory open while its .pyd/.dll is mapped, which
            # is the common case once anything has tried to import PyMuPDF.
            print(
                "[PDF Vector Importer] Could not secure a backup of "
                f"{target.name}: {exc}"
            )
            unsecured.append(target)
    return moved, unsecured


def _restore_quarantined(moved: "list[tuple[Path, Path]]") -> "list[Path]":
    """Put the quarantined bundle back. Returns the paths that could NOT be restored.

    The previous version was ``except OSError: pass``, which silently orphaned the
    only good copy: a partial rmtree of the destination leaves it existing, the
    rename back then raises FileExistsError, and nothing ever looks in a
    ``.quarantine`` directory again. Restore is now verified and, when a rename
    cannot work, falls back to a copy so the bytes come back even if the
    directory entry cannot be reused.
    """
    failed: "list[Path]" = []
    for original, quarantine in moved:
        if not quarantine.exists():
            failed.append(original)
            continue
        if original.exists():
            shutil.rmtree(original, ignore_errors=True)
        try:
            quarantine.rename(original)
            continue
        except OSError:
            pass
        try:
            # Rename refused (destination remnants, or a locked handle). Copy the
            # contents back instead of giving up on the only good copy.
            if quarantine.is_dir():
                shutil.copytree(quarantine, original, dirs_exist_ok=True)
            else:
                shutil.copy2(quarantine, original)
            shutil.rmtree(quarantine, ignore_errors=True)
        except OSError as exc:
            print(
                "[PDF Vector Importer] COULD NOT RESTORE the bundled runtime to "
                f"{original}: {exc}"
            )
            print(
                "[PDF Vector Importer] A known-good copy is still on disk at "
                f"{quarantine} -- move it back to {original.name} manually. "
                "Do not delete it."
            )
            failed.append(original)
    return failed


def _discard_quarantined(moved: "list[tuple[Path, Path]]") -> None:
    for _original, quarantine in moved:
        shutil.rmtree(quarantine, ignore_errors=True)


def install_pymupdf(*, clear_vendored: bool = True) -> bool:
    """
    Install PyMuPDF into the addon's lib/ directory.

    In Blender 3.x+, sys.executable points to the bundled Python binary.
    We use it directly with pip install --target.

    Returns True on success, False on failure.
    """
    lib_dir = get_lib_dir()
    lib_dir.mkdir(parents=True, exist_ok=True)

    # Quarantine, don't delete: a failed offline install must leave the bundled
    # runtime intact. The old code removed it first, so a network-less machine
    # ended up with no PyMuPDF at all -- and PyMuPDF is the sole rasteriser, so
    # that took out even the terminal Raster rung.
    moved: "list[tuple[Path, Path]]" = []
    unsecured: "list[Path]" = []
    if clear_vendored:
        moved, unsecured = _quarantine_vendored_pymupdf()
        if unsecured:
            # Refuse to run pip at all. pip --target --upgrade rmtree's the
            # destination, so proceeding here would destroy the very bytes we
            # just failed to back up. Doing nothing is always recoverable;
            # continuing is not.
            _restore_quarantined(moved)
            print(
                "[PDF Vector Importer] Install aborted: could not back up "
                + ", ".join(p.name for p in unsecured)
                + ". Nothing was changed. Close other applications using PyMuPDF "
                "(or restart Blender) and try again."
            )
            return False

    python_exe = sys.executable
    restored_cleanly = True

    def _restore_and_fail() -> bool:
        nonlocal restored_cleanly
        failed = _restore_quarantined(moved)
        restored_cleanly = not failed
        return False

    try:
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
            return _restore_and_fail()
        except FileNotFoundError:
            print(f"[PDF Vector Importer] Python executable not found: {python_exe}")
            print("[PDF Vector Importer] Cannot install PyMuPDF without a valid Python binary.")
            return _restore_and_fail()
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[PDF Vector Importer] error during pip install: {exc}")
            return _restore_and_fail()

        _purge_stale_pymupdf_modules()
        ensure_lib_path()
        if check_pymupdf():
            _discard_quarantined(moved)
            return True
        # pip reported success but the result does not import (e.g. an ABI wheel
        # that still cannot load): restore the known-good bundle so the addon
        # stays in the state it started from.
        return _restore_and_fail()
    except BaseException:
        # Any unhandled failure -- including one raised by a stubbed subprocess in
        # a test, or a KeyboardInterrupt mid-install -- must not leave the bundle
        # stranded in quarantine with nothing at lib/pymupdf.
        _restore_and_fail()
        raise
    finally:
        # Report the difference between "install failed, bundle intact" and
        # "install failed, bundle NOT restored". These used to be the same
        # message, so a user whose runtime had just been destroyed was told
        # nothing was lost.
        if moved and not restored_cleanly:
            print(
                "[PDF Vector Importer] WARNING: the bundled PyMuPDF was NOT fully "
                "restored. See the quarantine path printed above before reinstalling."
            )
        elif moved and restored_cleanly:
            print("[PDF Vector Importer] The bundled runtime was left in place.")


def ensure_pymupdf_runtime(*, auto_install: bool = False) -> bool:
    """
    Verify PyMuPDF can load in the current Blender Python process.

    ``auto_install`` is retained only for compatibility with older callers.
    Runtime checks never invoke pip or contact the network.  They may perform
    only the deterministic helper restoration already bundled by
    ``repair_vendored_pymupdf``.  Official release ZIPs are required to carry a
    complete compatible runtime; any other damage or incompatibility fails
    closed.
    """
    if check_pymupdf():
        return True
    detail = _import_error_detail()
    if detail:
        print(f"[PDF Vector Importer] PyMuPDF import failed: {detail}")
    if auto_install:
        print(
            "[PDF Vector Importer] Automatic dependency installation is disabled. "
            "Reinstall the official release ZIP; import did not run pip or access the network."
        )
    return False


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


def bundled_runtime_platform_supported() -> bool:
    """Whether the vendored PyMuPDF wheel can load on this platform at all.

    The bundle is ``cp310-abi3-win_amd64`` and ships only ``.pyd``/``.dll``
    binaries, so it is Windows-x64-only as a matter of fact, not policy.
    """
    return sys.platform == "win32"


def runtime_unavailable_message() -> str:
    """Platform-accurate text for "the bundled runtime will not load".

    The previous single message told every user to "reinstall the official release
    ZIP". On macOS and Linux that is advice to repeat the one action that cannot
    possibly work: the ZIP contains Windows-only binaries, and since this add-on
    no longer registers a pip installer for packaged releases there is no
    in-product recovery path either. Saying so plainly is not a support promise --
    it is the difference between a user who understands their situation and one who
    reinstalls three times and concludes the product is broken.
    """
    if bundled_runtime_platform_supported():
        return (
            "The bundled PyMuPDF runtime could not load. Reinstall the official "
            "PDF Vector Importer release ZIP. Import did not download packages or "
            "run pip."
        )
    return (
        "This release bundles a Windows-x64-only PyMuPDF runtime "
        "(cp310-abi3-win_amd64), so it cannot load on %s. Reinstalling the release "
        "ZIP will not change that. Import did not download packages or run pip."
        % sys.platform
    )


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
