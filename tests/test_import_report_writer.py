from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace()
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer.bl_import_engine import write_import_report  # noqa: E402
from pdf_vector_importer import bl_info  # noqa: E402


class TestImportReportWriter(unittest.TestCase):
    def test_write_import_report_records_raster_fallback_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_import_report_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            stats = {
                "pages_imported": 2,
                "primitives": 1,
                "text_items": 0,
                "collections": 1,
                "elapsed": 0.1,
            }
            with patch(
                "pdf_vector_importer.bl_import_engine._pymupdf_version",
                return_value="",
            ):
                write_import_report(
                    str(Path(tmp) / "sample.pdf"),
                    {},
                    stats,
                    import_mode="auto",
                    raster_pages=2,
                    output_path=str(report_path),
                )
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(data["fallback"]["used"])
            self.assertEqual(data["fallback"]["reason"], "raster_fallback_2_pages")

    def test_write_import_report_uses_shared_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_import_report_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            stats = {
                "pages_imported": 2,
                "primitives": 9,
                "text_items": 3,
                "collections": 4,
                "elapsed": 0.25,
                "performance_phases": {
                    "open_pdf_ms": 3.0,
                    "pages_import_ms": 240.0,
                },
                "text_source_spans": 4,
                "text_glyph_estimate": 22,
                "curves": 5,
                "meshes": 1,
                "images": 0,
            }

            with patch(
                "pdf_vector_importer.bl_import_engine._pymupdf_version",
                return_value="",
            ):
                result = write_import_report(
                    str(Path(tmp) / "sample.pdf"),
                    {"import_text": True, "text_mode": "glyphs"},
                    stats,
                    import_mode="vector",
                    output_path=str(report_path),
                )

            self.assertEqual(result, str(report_path))
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "bcs.import_report/1.1")
            self.assertEqual(data["host"]["app"], "blender")
            self.assertEqual(data["host"]["version"], "4.1.0")
            expected_version = ".".join(str(part) for part in bl_info["version"])
            self.assertEqual(data["importer"]["version"], expected_version)
            self.assertEqual(data["result"]["primitives"], 9)
            self.assertEqual(data["result"]["text_entities"], 3)
            self.assertEqual(data["result"]["layers"], 4)
            self.assertEqual(data["performance"]["phases"]["open_pdf_ms"], 3.0)
            self.assertEqual(data["performance"]["phases"]["pages_import_ms"], 240.0)
            self.assertEqual(data["performance"]["phases"]["total_ms"], 250.0)
            self.assertEqual(data["extra"]["curves"], 5)
            self.assertEqual(data["extra"]["import_text"], True)
            self.assertEqual(data["extra"]["text_mode"], "glyphs")
            self.assertEqual(data["extra"]["text_source_spans"], 4)
            self.assertEqual(data["extra"]["text_glyph_estimate"], 22)
            self.assertEqual(data["extra"]["actual_text_entity_types"]["entity_type"], "glyphs")
            self.assertEqual(data["extra"]["actual_text_entity_types"]["outline_curve_or_mesh"], 3)
            diagnostics = data["extra"]["diagnostics"]
            self.assertEqual(diagnostics["quality_level"], "low")
            self.assertIn("text_mode_glyphs", diagnostics["signals"])
            self.assertTrue(diagnostics["recommended_actions"])

    def test_import_report_diagnostics_for_fallback_and_dense_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_import_report_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            stats = {
                "pages_imported": 1,
                "primitives": 0,
                "text_items": 0,
                "collections": 0,
                "elapsed": 0.2,
                "text_source_spans": 14,
                "text_glyph_estimate": 1200,
            }
            with patch(
                "pdf_vector_importer.bl_import_engine._pymupdf_version",
                return_value="",
            ):
                write_import_report(
                    str(Path(tmp) / "scan.pdf"),
                    {"import_text": True, "text_mode": "glyphs"},
                    stats,
                    import_mode="auto",
                    raster_pages=1,
                    output_path=str(report_path),
                )
            data = json.loads(report_path.read_text(encoding="utf-8"))
            diagnostics = data["extra"]["diagnostics"]
            self.assertEqual(diagnostics["quality_level"], "empty")
            self.assertIn("fallback_used", diagnostics["signals"])
            self.assertIn("source_text_seen_but_no_text_entities_created", diagnostics["signals"])
            self.assertIn("dense_text_glyph_workload", diagnostics["signals"])
            self.assertTrue(
                any("Vector or Hybrid" in action for action in diagnostics["recommended_actions"])
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
