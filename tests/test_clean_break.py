"""BCS-ARCH-001 clean-break contract: old --preset and quality-tier flags are gone."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


TEST_PDF = Path(r"C:\Users\Rowdy Payton\Desktop\PDFTest Files\1015 - Rev 0.pdf")
REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_PY = REPO_ROOT / "pdf_vector_importer" / "operators.py"
LEGACY_ADDON_INIT_PY = REPO_ROOT / "blender_pdf_vector_importer" / "__init__.py"
ADDON_CONFIG_PY = REPO_ROOT / "pdf_vector_importer" / "pdfcadcore" / "import_config.py"
CLI_CONFIG_PY = REPO_ROOT / "blender_pdf_vector_importer" / "core" / "PDFImportConfig.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "blender_pdf_vector_importer.cli", *args]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


class TestCleanBreak(unittest.TestCase):
    """``--preset`` must have been deleted per BCS-ARCH-001 -- no shim."""

    @unittest.skipUnless(TEST_PDF.is_file(), f"Test PDF not available: {TEST_PDF}")
    def test_old_preset_flag_errors_out(self) -> None:
        result = _run_cli(str(TEST_PDF), "--preset", "shop")
        self.assertNotEqual(
            result.returncode,
            0,
            msg="--preset should be rejected; it was accepted instead",
        )
        combined = (result.stdout + result.stderr).lower()
        self.assertTrue(
            "unrecognized arguments" in combined or "--preset" in combined,
            msg=f"Unexpected error output: {combined!r}",
        )


class TestRule5FlagsRemoved(unittest.TestCase):
    """BCS-ARCH-001 Rule 5 sweep: quality-tier CLI flags must error out."""

    REMOVED_FLAGS = (
        "--hatch-mode",
        "--arc-mode",
        "--cleanup-level",
        "--lineweight-mode",
        "--raster-dpi",
        "--strict-text-fidelity",
        "--no-strict-text-fidelity",
        "--no-arcs",
        "--no-raster-fallback",
        "--grouping-mode",
    )

    @unittest.skipUnless(TEST_PDF.is_file(), f"Test PDF not available: {TEST_PDF}")
    def test_removed_flags_error_out(self) -> None:
        for flag in self.REMOVED_FLAGS:
            with self.subTest(flag=flag):
                # Most flags need a value; pass "x" — argparse rejects on flag itself first.
                result = _run_cli(str(TEST_PDF), flag, "x")
                self.assertNotEqual(
                    result.returncode, 0,
                    msg=f"{flag!r} should be rejected; it was accepted instead",
                )
                combined = (result.stdout + result.stderr).lower()
                self.assertTrue(
                    "unrecognized arguments" in combined or flag.lower() in combined,
                    msg=f"Unexpected output for {flag}: {combined!r}",
                )


class TestBlGuiProfessionalImport(unittest.TestCase):
    """Import operator: professional copy; strategy only in Advanced."""

    def setUp(self) -> None:
        self.source = OPERATORS_PY.read_text(encoding="utf-8")

    def test_professional_import_tagline(self) -> None:
        self.assertIn("Professional import", self.source)

    def test_show_advanced_gates_mode(self) -> None:
        self.assertIn("show_advanced", self.source)
        self.assertIn("effective_mode = self.mode if self.show_advanced else \"auto\"", self.source)

    def test_draw_hides_mode_unless_advanced(self) -> None:
        self.assertIn("if self.show_advanced:", self.source)
        self.assertNotIn('layout.prop(self, "mode")\n        layout.separator()', self.source)

    def test_all_four_text_modes_in_ui(self) -> None:
        for key in ("labels", "3d_text", "glyphs", "geometry"):
            self.assertIn(f'("{key}"', self.source)


class TestRule5OperatorPropsRemoved(unittest.TestCase):
    """Operator must not expose quality-tier BoolProperties (UI strip)."""

    REMOVED_PROPS = (
        "detect_arcs:",
        "make_faces:",
        "map_dashes:",
        "ignore_fill_only_shapes:",
    )

    def setUp(self) -> None:
        self.source = OPERATORS_PY.read_text(encoding="utf-8")

    def test_removed_props_not_declared(self) -> None:
        for prop in self.REMOVED_PROPS:
            self.assertNotIn(
                prop, self.source,
                f"Operator still declares quality-tier property {prop!r} (BCS-ARCH-001 Rule 5).",
            )

    def test_self_prop_references_gone(self) -> None:
        for attr in ("self.detect_arcs", "self.make_faces", "self.map_dashes",
                     "self.ignore_fill_only_shapes"):
            self.assertNotIn(
                attr, self.source,
                f"Operator still references {attr!r} after Rule 5 sweep.",
            )

    def test_text_default_is_scale_stable(self) -> None:
        self.assertIn('default="3d_text"', self.source)
        self.assertNotIn('default="labels"', self.source)
        self.assertNotIn("Import text as Blender text objects (default)", self.source)

    def test_no_legacy_preset_labels(self) -> None:
        for label in (
            "Fast", "Balanced", "Full", "Max Fidelity", "Raster Image",
            "Custom...", "Shop Drawing", "Technical Drawing",
        ):
            self.assertNotIn(
                f'"{label}"', self.source,
                f"Operator still references legacy preset label {label!r}.",
            )


class TestTextDefaults(unittest.TestCase):
    """Core config defaults must not silently return to Labels."""

    def test_embedded_configs_default_to_3d_text(self) -> None:
        for path in (ADDON_CONFIG_PY, CLI_CONFIG_PY):
            source = path.read_text(encoding="utf-8")
            self.assertIn('text_mode: str = "3d_text"', source)
            self.assertNotIn('text_mode: str = "labels"', source)

    def test_legacy_addon_entrypoint_has_text_mode_not_arc_dial(self) -> None:
        source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        self.assertIn('name="Text Mode"', source)
        self.assertIn('default="3d_text"', source)
        self.assertNotIn('detect_arcs: BoolProperty', source)
        self.assertNotIn('layout.prop(self, "detect_arcs")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
