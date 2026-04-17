"""BCS-ARCH-001 CLI contract: --mode auto produces an auto_mode block."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TEST_PDF = Path(r"C:\Users\Rowdy Payton\Desktop\PDFTest Files\1015 - Rev 0.pdf")


class TestModeCli(unittest.TestCase):
    """Smoke-test the ``--mode auto`` CLI contract for BCS-ARCH-001."""

    @unittest.skipUnless(TEST_PDF.is_file(), f"Test PDF not available: {TEST_PDF}")
    def test_auto_mode_produces_auto_mode_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bl_mode_cli_") as tmp:
            out_path = Path(tmp) / "summary.json"
            cmd = [
                sys.executable,
                "-m",
                "blender_pdf_vector_importer.cli",
                str(TEST_PDF),
                "--mode",
                "auto",
                "--json",
                str(out_path),
            ]
            result = subprocess.run(
                cmd,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"CLI failed: stdout={result.stdout!r} stderr={result.stderr!r}",
            )
            self.assertTrue(out_path.is_file(), f"Expected output file: {out_path}")

            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("auto_mode", data, "Summary JSON missing auto_mode block")

            block = data["auto_mode"]
            self.assertEqual(block.get("requested"), "auto")
            self.assertIsInstance(block.get("per_page"), list)
            self.assertGreaterEqual(len(block["per_page"]), 1)
            first = block["per_page"][0]
            self.assertIn(first.get("resolved"), {"vector", "raster", "hybrid"})
            self.assertIn("reason", first)
            self.assertIsInstance(block.get("summary"), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
