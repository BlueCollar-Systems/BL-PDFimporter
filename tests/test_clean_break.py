"""BCS-ARCH-001 clean-break contract: old --preset and quality-tier flags are gone."""
from __future__ import annotations

import ast
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
REPRESENTATION_FIDELITY_MD = REPO_ROOT / "REPRESENTATION_FIDELITY.md"


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

    def test_all_six_text_modes_in_ui(self) -> None:
        tree = ast.parse(self.source)
        mode_items = None
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "_TEXT_MODE_ITEMS" for target in statement.targets):
                mode_items = ast.literal_eval(statement.value)
                break
        self.assertIsNotNone(mode_items)
        self.assertEqual(
            [item[0] for item in mode_items],
            ["labels", "text", "3d_text", "glyphs", "geometry", "raster"],
        )


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
        # RB-11: the old file-wide quoted-word ban also fired on unrelated
        # docstrings, tooltips, or comments. The behavior this test protects
        # is narrower: the operator must not OFFER a legacy preset as a UI
        # choice, so scope the ban to strings inside EnumProperty items.
        tree = ast.parse(self.source)
        module_assigns: dict[str, ast.AST] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        module_assigns[target.id] = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                if isinstance(stmt.target, ast.Name):
                    module_assigns[stmt.target.id] = stmt.value
        enum_item_strings: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "EnumProperty":
                continue
            for keyword in node.keywords:
                if keyword.arg != "items":
                    continue
                value = keyword.value
                if isinstance(value, ast.Name):
                    value = module_assigns.get(value.id)
                if value is None:
                    continue
                for sub in ast.walk(value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        enum_item_strings.append(sub.value)
        self.assertTrue(
            enum_item_strings,
            "No EnumProperty items found in operators.py — scoping broke.",
        )
        for label in (
            "Fast", "Balanced", "Full", "Max Fidelity", "Raster Image",
            "Custom...", "Shop Drawing", "Technical Drawing",
        ):
            self.assertNotIn(
                label, enum_item_strings,
                f"EnumProperty items still offer legacy preset label {label!r}.",
            )


class TestTextDefaults(unittest.TestCase):
    """Core config defaults must not silently return to Labels."""

    def test_embedded_configs_default_to_3d_text(self) -> None:
        # RB-11: value-lock the synced pdfcadcore default instead of grepping
        # the literal source declaration — the exact-string lock constrained
        # implementation wording rather than the default value it protects.
        from pdf_vector_importer.pdfcadcore.import_config import ImportConfig

        self.assertEqual(ImportConfig().text_mode, "3d_text")

    def test_engine_passes_text_mode_to_builder(self) -> None:
        source = IMPORT_ENGINE_PY.read_text(encoding="utf-8")
        self.assertIn("text_mode=import_cfg.text_mode", source)

    def test_text_builder_modes_have_distinct_outputs(self) -> None:
        source = TEXT_BUILDER_PY.read_text(encoding="utf-8")
        self.assertIn('text_mode: str = "3d_text"', source)
        self.assertIn("def _attempt_native_font(", source)
        self.assertIn("def _attempt_glyphs(", source)
        self.assertIn("def _attempt_geometry(", source)
        self.assertIn('expected_type="CURVE"', source)
        self.assertIn('expected_type="MESH"', source)
        self.assertIn("deliver_item(", source)

    def test_glyph_mode_copy_matches_current_builder_contract(self) -> None:
        operator_source = OPERATORS_PY.read_text(encoding="utf-8")
        legacy_source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        compat_source = (REPO_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")

        for source in (operator_source, legacy_source, compat_source):
            self.assertNotIn("per-character vector glyphs", source)
            self.assertNotIn("Per-character vector curves", source)
        self.assertIn("non-editable CURVE glyph outlines", operator_source)
        self.assertIn("Exact-font CURVE glyph outlines", legacy_source)
        self.assertIn("real Blender `CURVE`", compat_source)

    def test_legacy_addon_entrypoint_has_text_mode_not_arc_dial(self) -> None:
        source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        self.assertIn('name="Text Mode"', source)
        self.assertIn('default="3d_text"', source)
        self.assertNotIn('detect_arcs: BoolProperty', source)
        self.assertNotIn('layout.prop(self, "detect_arcs")', source)


class TestRepresentationFidelityDocumentation(unittest.TestCase):
    """The owner-required ladder contract must remain explicit and host-specific."""

    def test_public_contract_documents_every_representation_and_required_oracle(self) -> None:
        source = REPRESENTATION_FIDELITY_MD.read_text(encoding="utf-8")
        for mode in ("Labels", "Text", "3D Text", "Glyphs", "Geometry", "Raster"):
            self.assertIn(f"| **{mode}** |", source)
        for required in (
            "Impossibility evidence",
            "Verification oracle",
            "Rollback ownership",
            "`extra.text_delivery`",
            "tests/test_representation_fidelity_blender.py",
            "tests/test_terminal_raster_delivery_blender.py",
        ):
            self.assertIn(required, source)

    def test_readme_lists_all_six_options_and_links_the_contract(self) -> None:
        source = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**6 Text Representation Options**", source)
        self.assertIn("[REPRESENTATION_FIDELITY.md](REPRESENTATION_FIDELITY.md)", source)
        self.assertNotIn("**4 Text Rendering Options**", source)


class TestModel3DGeneration(unittest.TestCase):
    """Optional 3D generation is explicit, additive, and reportable."""

    GEOMETRY_BUILDER_PY = REPO_ROOT / "pdf_vector_importer" / "bl_geometry_builder.py"

    def test_operator_exposes_model3d_controls(self) -> None:
        source = OPERATORS_PY.read_text(encoding="utf-8")
        self.assertIn("SHAPE_EXTRUSION_UI_ENABLED", source)
        self.assertIn("model3d_mode", source)
        self.assertIn("model3d_depth_mm", source)
        self.assertIn("Auto (if drawing has 3D evidence)", source)
        self.assertIn("Extrude closed shapes", source)

    def test_engine_threads_model3d_to_builder_and_report(self) -> None:
        source = IMPORT_ENGINE_PY.read_text(encoding="utf-8")
        self.assertIn('"model3d_mode": getattr(import_cfg, "model3d_mode", "off")', source)
        self.assertIn('"model3d_depth_m"', source)
        self.assertIn('"model_3d_intent"', source)
        self.assertIn('"model_3d"', source)

    def test_geometry_builder_creates_extruded_meshes(self) -> None:
        source = self.GEOMETRY_BUILDER_PY.read_text(encoding="utf-8")
        self.assertIn("def _create_extruded_mesh", source)
        self.assertIn('"model3d_solids"', source)
        self.assertIn('obj_name + "_solid"', source)


class TestBlenderVersionFloor(unittest.TestCase):
    """bl_info minimum Blender version must match COMPATIBILITY.md (3.1+)."""

    ADDON_INIT = REPO_ROOT / "pdf_vector_importer" / "__init__.py"

    def test_primary_addon_declares_blender_3_1(self) -> None:
        source = self.ADDON_INIT.read_text(encoding="utf-8")
        self.assertIn('"blender": (3, 1, 0)', source)

    def test_legacy_entrypoint_matches_primary_floor(self) -> None:
        source = LEGACY_ADDON_INIT_PY.read_text(encoding="utf-8")
        self.assertIn('"blender": (3, 1, 0)', source)

    def test_vendored_pymupdf_python_floor_matches_declared_host(self) -> None:
        """Packaged wheel must not require a newer CPython than Blender 3.1."""
        lib = REPO_ROOT / "pdf_vector_importer" / "lib"
        metas = sorted(lib.glob("pymupdf-*.dist-info/METADATA")) + sorted(
            lib.glob("PyMuPDF-*.dist-info/METADATA")
        )
        self.assertTrue(metas, "expected vendored PyMuPDF METADATA")
        text = metas[0].read_text(encoding="utf-8", errors="replace")
        requires = [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.lower().startswith("requires-python:")
        ]
        self.assertTrue(requires, "expected Requires-Python in PyMuPDF METADATA")
        # Blender 3.1 ships Python 3.10; refuse a wheel that needs 3.11+.
        req = requires[0].replace(" ", "")
        self.assertTrue(
            req.startswith(">=") and tuple(int(p) for p in req[2:].split(".")[:2])
            <= (3, 10),
            f"PyMuPDF Requires-Python {requires[0]!r} exceeds Blender 3.1 Python 3.10",
        )


class TestReleasePackaging(unittest.TestCase):
    """Release packaging must work on Linux CI while bundling Windows runtime."""

    def test_non_windows_ci_does_not_import_windows_pymupdf_binary(self) -> None:
        source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
        self.assertIn('if sys.platform != "win32":', source)
        self.assertIn("skipping binary import check", source)
        self.assertIn('_VENDORED_LIB / "pymupdf" / "_extra.pyd"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
