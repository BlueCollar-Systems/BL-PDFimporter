from __future__ import annotations

import tempfile
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pdf_vector_importer import dependency_manager
from scripts import smoke_release_zip


def _write_synthetic_release_zip(path: Path, *, include_fonttools: bool) -> None:
    fonttools_members = {
        "pdf_vector_importer/lib/fontTools/__init__.py",
        "pdf_vector_importer/lib/fontTools/ttLib/__init__.py",
        "pdf_vector_importer/lib/fontTools/cffLib/__init__.py",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/METADATA",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/WHEEL",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE.external",
    }
    members = set(smoke_release_zip.REQUIRED_MEMBERS) - fonttools_members
    members.add("pdf_vector_importer/pdfcadcore/__init__.py")
    if include_fonttools:
        members.update(fonttools_members)

    content = {
        "pdf_vector_importer/__init__.py": (
            "bl_info = {'version': (1, 0, 67)}\n"
            "def register():\n    pass\n"
        ),
        "pdf_vector_importer/pdfcadcore/fitz_loader.py": (
            "class _Fitz:\n    open = staticmethod(lambda *_a, **_k: None)\n"
            "def import_fitz(**_kwargs):\n    return _Fitz()\n"
        ),
        "pdf_vector_importer/lib/fontTools/__init__.py": "__version__ = '4.60.2'\n",
        "pdf_vector_importer/lib/fontTools/ttLib/__init__.py": "class TTFont:\n    pass\n",
        "pdf_vector_importer/lib/fontTools/cffLib/__init__.py": "class CFFFontSet:\n    pass\n",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/METADATA": (
            "Metadata-Version: 2.4\nName: fonttools\nVersion: 4.60.2\n"
        ),
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE": "MIT\n",
        "pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE.external": (
            "External notices\n"
        ),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member in sorted(members):
            archive.writestr(member, content.get(member, ""))


class TestDependencyManager(unittest.TestCase):
    def test_runtime_check_never_invokes_pip_repair(self) -> None:
        """Import-time checks must never invoke package-manager/network repair."""
        with patch.object(dependency_manager, "check_pymupdf", return_value=False), \
                patch.object(dependency_manager, "_import_error_detail", return_value="missing"), \
                patch.object(
                    dependency_manager,
                    "install_pymupdf",
                    side_effect=AssertionError("runtime checks must not invoke pip"),
                ) as installer:
            available = dependency_manager.ensure_pymupdf_runtime(auto_install=True)

        self.assertFalse(available)
        installer.assert_not_called()

    def test_import_hot_path_requests_offline_dependency_check(self) -> None:
        source = Path(dependency_manager.__file__).with_name("bl_import_engine.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ensure_pymupdf_runtime(auto_install=False)", source)
        self.assertNotIn("ensure_pymupdf_runtime(auto_install=True)", source)

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

    def test_failed_install_restores_the_bundled_runtime(self) -> None:
        # The clean-machine trap the third-party advice recommends walking into:
        # auto-install rmtree'd the bundled pymupdf BEFORE a pip call that an
        # offline machine cannot complete, leaving no runtime at all. A failed
        # install must leave the bundle exactly as it found it.
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            (lib / "pymupdf").mkdir()
            (lib / "pymupdf" / "__init__.py").write_text("x", encoding="utf-8")
            (lib / "fitz").mkdir()
            (lib / "fitz" / "__init__.py").write_text("x", encoding="utf-8")
            (lib / "pymupdf-1.27.2.3.dist-info").mkdir()
            (lib / "pymupdf-1.27.2.3.dist-info" / "WHEEL").write_text("t", encoding="utf-8")

            def _fail(*_a, **_k):
                raise subprocess.CalledProcessError(1, ["pip"])

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", _fail):
                ok = dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertFalse(ok, "a failed offline install must report failure")
            self.assertTrue(
                (lib / "pymupdf" / "__init__.py").is_file(),
                "the bundled pymupdf must survive a failed install",
            )
            self.assertTrue((lib / "fitz" / "__init__.py").is_file())
            self.assertTrue((lib / "pymupdf-1.27.2.3.dist-info" / "WHEEL").is_file())
            leftovers = [p.name for p in lib.glob("*.quarantine*")]
            self.assertEqual([], leftovers, f"no quarantine debris: {leftovers}")

    def test_successful_install_clears_the_old_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            (lib / "pymupdf").mkdir()
            (lib / "pymupdf" / "stale.py").write_text("old", encoding="utf-8")

            def _ok(*_a, **_k):
                return 0

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", _ok), \
                    patch.object(dependency_manager, "_purge_stale_pymupdf_modules"), \
                    patch.object(dependency_manager, "ensure_lib_path"), \
                    patch.object(dependency_manager, "check_pymupdf", return_value=True):
                ok = dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertTrue(ok)
            # The stale bundle was moved aside before the (successful) install and
            # not restored; no quarantine debris remains.
            self.assertEqual([], [p.name for p in lib.glob("*.quarantine*")])

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

    def test_ci_installs_the_declared_fonttools_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (
            ".github/workflows/bl-pdfimporter-ci.yml",
            ".github/workflows/auto-release.yml",
        ):
            source = (root / relative).read_text(encoding="utf-8").lower()
            self.assertIn('"fonttools==4.60.2"', source, relative)

    def test_release_zip_smoke_rejects_missing_fonttools_runtime_and_licenses(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_zip_smoke_") as tmp:
            zip_path = Path(tmp) / "missing-fonttools.zip"
            _write_synthetic_release_zip(zip_path, include_fonttools=False)
            script = Path(smoke_release_zip.__file__).resolve()
            result = subprocess.run(
                [sys.executable, str(script), str(zip_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fonttools", (result.stderr + result.stdout).lower())

    def test_release_zip_smoke_imports_fonttools_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_zip_smoke_") as tmp:
            zip_path = Path(tmp) / "complete.zip"
            _write_synthetic_release_zip(zip_path, include_fonttools=True)
            script = Path(smoke_release_zip.__file__).resolve()
            result = subprocess.run(
                [sys.executable, str(script), str(zip_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
