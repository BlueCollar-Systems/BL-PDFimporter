from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "pdf_vector_importer" / "pdfcadcore"
sys.path.insert(0, str(REPO_ROOT / "pdf_vector_importer"))

from pdfcadcore import primitive_extractor  # noqa: E402


# This is the reviewed integration output: current main plus the fraction-core
# port. It deliberately does not claim that these combined bytes were approved
# independently before the merge review.
REVIEWED_COMBINED_SUCCESSOR_SHA256 = (
    "c3a868d15a906d48b8455db93d68486dc8f8c2d321a32dd2a865e808368418cd"
)


def test_shared_core_matches_reviewed_combined_successor() -> None:
    raw = (CORE_ROOT / "primitive_extractor.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == REVIEWED_COMBINED_SUCCESSOR_SHA256


def test_obsolete_stacked_fraction_scale_clamp_is_absent() -> None:
    source = inspect.getsource(primitive_extractor)
    assert "_FRAC_STACKED_SCALE" not in source
    assert "font_size * 0.6" not in source
