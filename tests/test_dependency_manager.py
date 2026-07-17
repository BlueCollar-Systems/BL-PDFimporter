from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf_vector_importer import dependency_manager


class TestDependencyManager(unittest.TestCase):
    def test_repairs_missing_pymupdf_extra_helper_from_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_dep_repair_") as tmp:
            addon_dir = Path(tmp) / "pdf_vector_importer"
            pymupdf_dir = addon_dir / "lib" / "pymupdf"
            pymupdf_dir.mkdir(parents=True)
            backup = addon_dir / "_vendored_pymupdf_extra.py"
            backup.write_text("# repaired helper\nVALUE = 42\n", encoding="utf-8")
            (pymupdf_dir / "_extra.pyd").write_bytes(b"compiled")

            fake_file = addon_dir / "dependency_manager.py"
            with patch.object(dependency_manager, "__file__", str(fake_file)):
                repaired = dependency_manager.repair_vendored_pymupdf()

            self.assertTrue(repaired)
            self.assertEqual(
                (pymupdf_dir / "extra.py").read_text(encoding="utf-8"),
                backup.read_text(encoding="utf-8"),
            )

    def test_ensure_lib_path_adds_addon_dir_for_bundled_pdfcadcore(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_dep_path_") as tmp:
            addon_dir = Path(tmp) / "pdf_vector_importer"
            addon_dir.mkdir(parents=True)
            fake_file = addon_dir / "dependency_manager.py"
            original_path = list(sys.path)
            try:
                with patch.object(dependency_manager, "__file__", str(fake_file)):
                    dependency_manager.ensure_lib_path()
                self.assertIn(str(addon_dir), sys.path)
                self.assertIn(str(addon_dir / "lib"), sys.path)
            finally:
                sys.path[:] = original_path

    def test_build_release_requires_pymupdf_extra_and_repair_backup(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("build_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('_VENDORED_LIB / "pymupdf" / "extra.py"', source)
        self.assertIn('PKG / "_vendored_pymupdf_extra.py"', source)

    def test_vendored_fonttools_runtime_is_pure_python_and_licensed(self) -> None:
        lib_dir = Path(__file__).resolve().parents[1] / "pdf_vector_importer" / "lib"
        self.assertTrue((lib_dir / "fontTools" / "ttLib" / "__init__.py").is_file())
        self.assertTrue((lib_dir / "fontTools" / "cffLib" / "__init__.py").is_file())
        dist_info = lib_dir / "fonttools-4.60.2.dist-info"
        self.assertTrue((dist_info / "licenses" / "LICENSE").is_file())
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
        wheel = (dist_info / "WHEEL").read_text(encoding="utf-8")
        self.assertIn("Requires-Python: >=3.9", metadata)
        self.assertIn("Root-Is-Purelib: true", wheel)
        self.assertIn("Tag: py3-none-any", wheel)
        self.assertEqual(list((lib_dir / "fontTools").rglob("*.pyd")), [])

    def test_build_release_requires_fonttools_runtime_and_license(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("build_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('_VENDORED_LIB / "fontTools" / "ttLib" / "__init__.py"', source)
        self.assertIn(
            '_VENDORED_LIB / "fonttools-4.60.2.dist-info" / "licenses" / "LICENSE"',
            source,
        )

    def test_dependency_manager_uses_package_relative_pdfcadcore(self) -> None:
        source = Path(dependency_manager.__file__).read_text(encoding="utf-8")
        self.assertIn("from .pdfcadcore.fitz_loader import import_fitz", source)
        self.assertNotIn("from pdfcadcore.fitz_loader import import_fitz", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
