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


if __name__ == "__main__":
    unittest.main(verbosity=2)
