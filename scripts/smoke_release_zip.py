#!/usr/bin/env python3
"""Smoke-test the shipped Blender add-on ZIP."""
from __future__ import annotations

import argparse
import glob
import importlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REQUIRED_MEMBERS = {
    "pdf_vector_importer/__init__.py",
    "pdf_vector_importer/bl_import_engine.py",
    "pdf_vector_importer/operators.py",
    "pdf_vector_importer/pdfcadcore/fitz_loader.py",
    "pdf_vector_importer/_vendored_pymupdf_extra.py",
    "pdf_vector_importer/lib/pymupdf/__init__.py",
    "pdf_vector_importer/lib/pymupdf/extra.py",
    "pdf_vector_importer/lib/pymupdf/mupdf.py",
    "pdf_vector_importer/lib/pymupdf/_extra.pyd",
    "pdf_vector_importer/lib/pymupdf/_mupdf.pyd",
    "pdf_vector_importer/lib/pymupdf/mupdfcpp64.dll",
    "pdf_vector_importer/lib/fontTools/__init__.py",
    "pdf_vector_importer/lib/fontTools/ttLib/__init__.py",
    "pdf_vector_importer/lib/fontTools/cffLib/__init__.py",
    "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/METADATA",
    "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/WHEEL",
    "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE",
    "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE.external",
}


def _resolve_zip(pattern: str) -> Path:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No release ZIP matched {pattern!r}")
    return Path(matches[-1]).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", help="Release ZIP path or glob pattern")
    args = parser.parse_args()

    zip_path = _resolve_zip(args.zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        missing = sorted(REQUIRED_MEMBERS - names)
        if missing:
            raise SystemExit(
                "Release ZIP is missing required add-on members: "
                + ", ".join(missing)
            )
        runtime_helper = "pdf_vector_importer/lib/pymupdf/extra.py"
        repair_helper = "pdf_vector_importer/_vendored_pymupdf_extra.py"
        if zf.read(repair_helper) != zf.read(runtime_helper):
            raise SystemExit(
                "Release ZIP PyMuPDF repair helper is not byte-identical to "
                "the bundled runtime helper"
            )
        with tempfile.TemporaryDirectory(prefix="bl_addon_zip_") as tmp:
            zf.extractall(tmp)
            lib_dir = Path(tmp) / "pdf_vector_importer" / "lib"
            fonttools_code = "\n".join(
                (
                    "import sys",
                    f"sys.path.insert(0, {str(lib_dir)!r})",
                    "import fontTools",
                    "from fontTools.ttLib import TTFont",
                    "from fontTools.cffLib import CFFFontSet",
                    "assert fontTools.__version__ == '4.60.2'",
                    "assert TTFont is not None and CFFFontSet is not None",
                    f"metadata = ({str(lib_dir)!r} + '/fonttools-4.60.2.dist-info/METADATA')",
                    "assert 'Version: 4.60.2' in open(metadata, encoding='utf-8').read()",
                    f"license_dir = ({str(lib_dir)!r} + '/fonttools-4.60.2.dist-info/licenses')",
                    "assert open(license_dir + '/LICENSE', encoding='utf-8').read().strip()",
                    "assert open(license_dir + '/LICENSE.external', encoding='utf-8').read().strip()",
                )
            )
            fonttools_proc = subprocess.run(
                [sys.executable, "-S", "-c", fonttools_code],
                capture_output=True,
                text=True,
            )
            if fonttools_proc.returncode != 0:
                raise SystemExit(
                    "Vendored FontTools import/metadata/license smoke failed: "
                    + (fonttools_proc.stderr.strip() or fonttools_proc.stdout.strip())
                )
            sys.path.insert(0, tmp)
            addon = importlib.import_module("pdf_vector_importer")
            version = addon.bl_info.get("version")
            if not isinstance(version, tuple) or len(version) != 3:
                raise SystemExit("pdf_vector_importer.bl_info version is invalid")
            if not callable(getattr(addon, "register", None)):
                raise SystemExit("pdf_vector_importer.register is missing")
            if sys.platform == "win32":
                lib_dir = str(lib_dir)
                code = (
                    "import sys; "
                    f"sys.path.insert(0, r'{tmp}'); "
                    "from pdf_vector_importer.pdfcadcore.fitz_loader import import_fitz; "
                    f"fitz = import_fitz(prefer_lib_dir=r'{lib_dir}'); "
                    "assert callable(getattr(fitz, 'open', None))"
                )
                proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
                if proc.returncode != 0:
                    raise SystemExit(
                        "Vendored PyMuPDF import failed from release ZIP: "
                        + (proc.stderr.strip() or proc.stdout.strip())
                    )

    print(f"Release ZIP smoke passed: {zip_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
