"""BCS-ARCH-001 clean-break contract: old --preset and quality-tier flags are gone."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_PY = REPO_ROOT / "pdf_vector_importer" / "operators.py"
LEGACY_ADDON_INIT_PY = REPO_ROOT / "blender_pdf_vector_importer" / "__init__.py"
ADDON_CONFIG_PY = REPO_ROOT / "pdf_vector_importer" / "pdfcadcore" / "import_config.py"
IMPORT_ENGINE_PY = REPO_ROOT / "pdf_vector_importer" / "bl_import_engine.py"
TEXT_BUILDER_PY = REPO_ROOT / "pdf_vector_importer" / "bl_text_builder.py"
BUILD_RELEASE_PY = REPO_ROOT / "build_release.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "blender_pdf_vector_importer.cli", *args]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _write_argparse_pdf(tmp: str) -> Path:
    path = Path(tmp) / "argparse-anchor.pdf"
    path.write_bytes(b"%PDF-1.4\n% argparse-only portable fixture\n")
    return path


class TestCleanBreak(unittest.TestCase):
    """``--preset`` must have been deleted per BCS-ARCH-001 -- no shim."""

    def test_old_preset_flag_errors_out(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_clean_break_") as tmp:
            result = _run_cli(str(_write_argparse_pdf(tmp)), "--preset", "shop")
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

    def test_removed_flags_error_out(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_removed_flags_") as tmp:
            pdf_path = _write_argparse_pdf(tmp)
            for flag in self.REMOVED_FLAGS:
                with self.subTest(flag=flag):
                    # Most flags need a value; pass "x" so argparse rejects on flag itself first.
                    result = _run_cli(str(pdf_path), flag, "x")
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
        source = ADDON_CONFIG_PY.read_text(encoding="utf-8")
        self.assertIn('text_mode: str = "3d_text"', source)
        self.assertNotIn('text_mode: str = "labels"', source)

    def test_engine_passes_text_mode_to_builder(self) -> None:
        source = IMPORT_ENGINE_PY.read_text(encoding="utf-8")
        self.assertIn("text_mode=import_cfg.text_mode", source)

    def test_text_builder_modes_have_distinct_outputs(self) -> None:
        source = TEXT_BUILDER_PY.read_text(encoding="utf-8")
        self.assertIn('text_mode: str = "3d_text"', source)
        self.assertIn('if mode == "3d_text":', source)
        self.assertIn("font_data.extrude = _text_extrusion_depth(font_data.size)", source)
        self.assertIn('if mode in {"glyphs", "geometry"}:', source)
        self.assertIn("def _meshify_text_object(", source)

    def test_glyph_mode_copy_matches_current_builder_contract(self) -> None:
        operator_source = OPERATORS_PY.read_text(encoding="utf-8")
        legacy_source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        compat_source = (REPO_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")

        for source in (operator_source, legacy_source, compat_source):
            self.assertNotIn("per-character vector glyphs", source)
            self.assertNotIn("Per-character vector curves", source)
        self.assertIn("Convert text runs to non-editable outline meshes", operator_source)
        self.assertIn("Convert text runs to non-editable outline meshes", legacy_source)
        self.assertIn("do **not** create one separate object per character", compat_source)

    def test_legacy_addon_entrypoint_has_text_mode_not_arc_dial(self) -> None:
        source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        self.assertIn('name="Text Mode"', source)
        self.assertIn('default="3d_text"', source)
        self.assertNotIn('detect_arcs: BoolProperty', source)
        self.assertNotIn('layout.prop(self, "detect_arcs")', source)


class TestBlenderVersionFloor(unittest.TestCase):
    """bl_info minimum Blender version must match COMPATIBILITY.md (3.0+)."""

    ADDON_INIT = REPO_ROOT / "pdf_vector_importer" / "__init__.py"

    def test_primary_addon_declares_blender_3_0(self) -> None:
        source = self.ADDON_INIT.read_text(encoding="utf-8")
        self.assertIn('"blender": (3, 0, 0)', source)

    def test_legacy_entrypoint_matches_primary_floor(self) -> None:
        source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        self.assertIn('"blender": (3, 0, 0)', source)


class TestReleasePackaging(unittest.TestCase):
    """Release packaging must work on Linux CI while bundling Windows runtime."""

    def test_non_windows_ci_does_not_import_windows_pymupdf_binary(self) -> None:
        source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
        self.assertIn('if sys.platform != "win32":', source)
        self.assertIn("skipping binary import check", source)
        self.assertIn('_VENDORED_LIB / "pymupdf" / "_extra.pyd"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
