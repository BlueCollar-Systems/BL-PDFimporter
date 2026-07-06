#!/usr/bin/env python3
"""Tests for bcs.parts_bootstrap/1.0 sidecar emission in Blender hosts."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace()
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blender_pdf_vector_importer.core.document import DocumentExtraction, ExtractedPage  # noqa: E402
from blender_pdf_vector_importer.importer import ImportRun, write_import_report  # noqa: E402
from pdf_vector_importer.bl_import_engine import write_import_report as bl_write_import_report  # noqa: E402
from pdf_vector_importer.pdfcadcore.import_config import ImportConfig  # noqa: E402
from pdf_vector_importer.pdfcadcore.primitives import NormalizedText, PageData  # noqa: E402

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402


def _bom_text_items() -> list[NormalizedText]:
    return [
        NormalizedText(1, "1017FR1", "1017FR1", insertion=(10, 100), page_number=1),
        NormalizedText(2, "1", "1", insertion=(20, 100), page_number=1),
        NormalizedText(3, "W12X30", "W12X30", insertion=(30, 100), page_number=1),
        NormalizedText(4, "13'-11 1/4\"", "13'-11 1/4\"", insertion=(40, 100), page_number=1),
        NormalizedText(5, "417", "417", insertion=(50, 100), page_number=1),
        NormalizedText(6, "GALV.", "GALV.", insertion=(60, 100), page_number=1),
        NormalizedText(7, "A992", "A992", insertion=(70, 100), page_number=1),
    ]


def _write_blank_pdf(path: Path) -> None:
    doc = fitz.open()
    doc.new_page(width=200, height=120)
    doc.save(str(path))
    doc.close()


class TestBlenderPartsBootstrapEmitters(unittest.TestCase):
    def test_headless_write_import_report_emits_parts_bootstrap_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_parts_bootstrap_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_blank_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"
            texts = _bom_text_items()
            page = ExtractedPage(
                page_data=PageData(page_number=1, width=200, height=120, text_items=texts),
                profile=SimpleNamespace(titleblock_likely=False),
                resolved_mode="vector",
            )
            run = ImportRun(
                extraction=DocumentExtraction(str(pdf_path), pages=[page], requested_mode="vector"),
                config=ImportConfig.vector(),
            )

            write_import_report(run, str(report_path), elapsed_ms=15.0)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["extra"]["parts_bootstrap"]["row_count"], 1)
            sidecar = json.loads((tmp_path / "parts_bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["rows"][0]["piece_mark"], "1017FR1")
            self.assertEqual(sidecar["rows"][0]["profile_hint"], "W12X30")
            self.assertIn("report_sha256", sidecar["import_build_stamp"])

    @mock.patch("pdf_vector_importer.bl_import_engine._pymupdf_version", return_value="1.24.0")
    @mock.patch("pdf_vector_importer.bl_import_engine._blender_host_version", return_value="4.2.0")
    @mock.patch("pdf_vector_importer.bl_import_engine._importer_version", return_value="1.0.59")
    def test_addon_write_import_report_emits_parts_bootstrap_sidecar(
        self,
        _importer_version: mock.MagicMock,
        _host_version: mock.MagicMock,
        _fitz_version: mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_addon_parts_bootstrap_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_blank_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"

            bl_write_import_report(
                str(pdf_path),
                {"import_text": True, "text_mode": "labels"},
                {
                    "pages_imported": 1,
                    "primitives": 1,
                    "text_items": len(_bom_text_items()),
                    "collections": 1,
                    "elapsed": 0.01,
                    "parts_bootstrap_text_items": _bom_text_items(),
                },
                output_path=str(report_path),
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["extra"]["parts_bootstrap"]["row_count"], 1)
            sidecar = json.loads((tmp_path / "parts_bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["rows"][0]["piece_mark"], "1017FR1")
            self.assertEqual(sidecar["rows"][0]["profile_hint"], "W12X30")
            self.assertIn("report_sha256", sidecar["import_build_stamp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
