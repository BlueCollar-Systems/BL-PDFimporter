from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "pdf_vector_importer" / "pdfcadcore"
sys.path.insert(0, str(REPO_ROOT / "pdf_vector_importer"))

from pdfcadcore import primitive_extractor  # noqa: E402


APPROVED_SHA256 = "aadbe4dbb83b24720e853e14b290dcdc7d4d7952ad3c0e6b48f17380ba5cec8d"


def test_shared_core_is_exact_independently_approved_successor() -> None:
    raw = (CORE_ROOT / "primitive_extractor.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == APPROVED_SHA256


def test_obsolete_stacked_fraction_scale_clamp_is_absent() -> None:
    source = inspect.getsource(primitive_extractor)
    assert "_FRAC_STACKED_SCALE" not in source
    assert "font_size * 0.6" not in source
