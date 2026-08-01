# -*- coding: utf-8 -*-
"""Open-time PDF gate: Unicode filenames must open via stream on Windows."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pdf_vector_importer.pdfcadcore.fitz_loader import PdfOpenError, safe_open


class TestPdfOpenGateBlender(unittest.TestCase):
    def test_em_dash_filename_opens_via_stream(self) -> None:
        minimal_pdf = (
            b"%PDF-1.1\n"
            b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
            b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 3 3] >>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"0000000009 00000 n \n0000000068 00000 n \n0000000125 00000 n \n"
            b"trailer<< /Size 4 /Root 1 0 R >>\nstartxref\n196\n%%EOF\n"
        )
        with tempfile.TemporaryDirectory(prefix="bl_open_gate_") as tmp:
            path = Path(tmp) / "Drawing\u2014Rev0.pdf"
            path.write_bytes(minimal_pdf)
            doc = safe_open(str(path))
            try:
                self.assertGreaterEqual(int(doc.page_count), 1)
            finally:
                doc.close()

    def test_empty_file_rejects_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_open_gate_") as tmp:
            path = Path(tmp) / "empty.pdf"
            path.write_bytes(b"")
            with self.assertRaises(PdfOpenError) as ctx:
                safe_open(str(path))
            self.assertEqual(ctx.exception.reason, "empty_file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
