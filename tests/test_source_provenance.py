#!/usr/bin/env python3
"""Tests for bcs.source_provenance/1.0 sidecar emission (Blender host)."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
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

from blender_pdf_vector_importer.importer import run_import, write_import_report  # noqa: E402
from pdfcadcore.import_config import ImportConfig  # noqa: E402
from pdfcadcore.source_provenance import (  # noqa: E402
    ensure_import_session_id,
    record_text_span_provenance,
)
from pdf_vector_importer.bl_import_engine import write_import_report as bl_write_import_report  # noqa: E402

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402


def _write_sample_pdf(pdf_path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    page.insert_text((30, 60), "Provenance sample", fontsize=10)
    doc.save(str(pdf_path))
    doc.close()


class TestBlenderSourceProvenance(unittest.TestCase):
    def test_headless_write_import_report_emits_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_source_provenance_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_sample_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"

            run = run_import(
                str(pdf_path),
                mode="vector",
                overrides={"import_text": True, "text_mode": "labels"},
            )
            ensure_import_session_id(run.config)
            record_text_span_provenance(
                run.config,
                page=1,
                span={"bbox": [30, 50, 120, 62], "text": "Provenance sample"},
                text="Provenance sample",
                created_entity_type="native_3d_text",
                text_mode="labels",
            )
            write_import_report(run, str(report_path), elapsed_ms=5.0)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("source_provenance", report["extra"])
            sidecar_path = tmp_path / "source_provenance.json"
            self.assertTrue(sidecar_path.is_file())

    @mock.patch("pdf_vector_importer.bl_import_engine._pymupdf_version", return_value="1.24.0")
    @mock.patch("pdf_vector_importer.bl_import_engine._blender_host_version", return_value="4.2.0")
    @mock.patch("pdf_vector_importer.bl_import_engine._importer_version", return_value="1.0.56")
    def test_bl_import_engine_write_import_report_emits_sidecar(
        self,
        _importer_version: mock.MagicMock,
        _host_version: mock.MagicMock,
        _fitz_version: mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_engine_provenance_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_sample_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"
            cfg = ImportConfig.vector()
            cfg.import_text = True
            cfg.text_mode = "labels"
            ensure_import_session_id(cfg)
            record_text_span_provenance(
                cfg,
                page=1,
                span={"bbox": [30, 50, 120, 62], "text": "Provenance sample"},
                text="Provenance sample",
                created_entity_type="native_3d_text",
            )

            bl_write_import_report(
                str(pdf_path),
                {"import_text": True, "text_mode": "labels"},
                {
                    "pages_imported": 1,
                    "primitives": 1,
                    "text_items": 1,
                    "collections": 1,
                    "elapsed": 0.01,
                },
                output_path=str(report_path),
                provenance_opts=cfg,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("source_provenance", report["extra"])
            self.assertTrue((tmp_path / "source_provenance.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
