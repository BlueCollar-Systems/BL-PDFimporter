from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import fitz

from blender_pdf_vector_importer.core.PDFPrimitiveExtractor import _norm_color
from blender_pdf_vector_importer.core.document import ExtractionOptions, extract_document
from blender_pdf_vector_importer.importer import apply_uniform_scale, run_import


class TestCorePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bl_pdf_importer_test_")
        self.tmp_path = Path(self._tmp.name)
        self.pdf_path = self.tmp_path / "sample.pdf"
        self.image_dir = self.tmp_path / "images"
        self._build_sample_pdf(self.pdf_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_sample_pdf(self, out_path: Path) -> None:
        doc = fitz.open()
        page = doc.new_page(width=600, height=400)

        # Basic vector line
        page.draw_line((50, 50), (300, 50), color=(0, 0, 0), width=1.0)

        # Circle approximated by polyline to exercise arc promotion
        center = (200, 220)
        radius = 55
        pts = []
        for i in range(16):
            ang = (2 * math.pi * i) / 16
            pts.append((center[0] + radius * math.cos(ang), center[1] + radius * math.sin(ang)))
        pts.append(pts[0])
        page.draw_polyline(pts, color=(1, 0, 0), width=1.0)

        # Text
        page.insert_text((70, 140), "AISC W12x26", fontsize=12)

        # Embedded image
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), 0)
        pix.clear_with(0x00CC66)
        img_bytes = pix.tobytes("png")
        page.insert_image(fitz.Rect(380, 40, 460, 120), stream=img_bytes)

        # Second page to validate default page-selection behavior.
        page2 = doc.new_page(width=300, height=200)
        page2.draw_line((20, 20), (200, 20), color=(0, 0, 1), width=1.0)

        doc.save(str(out_path))

    def test_extract_document_summary(self) -> None:
        extraction = extract_document(
            str(self.pdf_path),
            ExtractionOptions(
                pages="1",
                import_text=True,
                import_images=True,
                detect_arcs=True,
                image_dir=str(self.image_dir),
            ),
        )

        summary = extraction.summary()
        self.assertEqual(summary["pages"], 1)
        self.assertGreaterEqual(summary["primitives"], 2)
        self.assertGreaterEqual(summary["text_items"], 1)
        self.assertGreaterEqual(summary["images"], 1)

        page = extraction.pages[0].page_data
        prim_types = {p.type for p in page.primitives}
        self.assertTrue({"arc", "circle", "polyline", "closed_loop"}.intersection(prim_types))

    def test_default_page_selection_imports_all_pages(self) -> None:
        extraction = extract_document(
            str(self.pdf_path),
            ExtractionOptions(import_text=False, import_images=False),
        )
        self.assertEqual(len(extraction.pages), 2)

    def test_raster_only_mode_renders_page_image(self) -> None:
        run = run_import(str(self.pdf_path), preset="raster_only", overrides={"pages": "1"})
        summary = run.extraction.summary()
        self.assertEqual(summary["pages"], 1)
        self.assertEqual(summary["primitives"], 0)
        self.assertEqual(summary["text_items"], 0)
        self.assertGreaterEqual(summary["images"], 1)

    def test_reference_scale_transform(self) -> None:
        run = run_import(str(self.pdf_path), preset="general", overrides={"pages": "1"})
        page = run.extraction.pages[0].page_data
        width_before = page.width
        apply_uniform_scale(run.extraction, 2.0)
        self.assertAlmostEqual(run.extraction.pages[0].page_data.width, width_before * 2.0, places=6)

    def test_cmyk_color_normalization(self) -> None:
        rgb = _norm_color((0.0, 1.0, 1.0, 0.0))
        self.assertAlmostEqual(rgb[0], 1.0, places=3)
        self.assertAlmostEqual(rgb[1], 0.0, places=3)
        self.assertAlmostEqual(rgb[2], 0.0, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
