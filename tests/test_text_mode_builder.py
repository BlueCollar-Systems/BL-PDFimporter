# -*- coding: utf-8 -*-
"""Headless checks for Blender text_mode routing helpers."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(
        types=types.SimpleNamespace(
            Collection=object,
            Material=object,
            Object=object,
            VectorFont=object,
        )
    )

from pdf_vector_importer.bl_text_builder import (
    _calibrated_text_size_mm,
    _normalize_text_mode,
    _resolve_text_mode,
    _text_extrusion_depth,
    _warn_unknown_text_mode_once,
)
from pdf_vector_importer.pdfcadcore.primitives import NormalizedText


def test_unknown_text_mode_is_normalized_loudly_not_silently():
    """TEXTMODE-1 item 12: unknown input still delivers the 3d_text default,
    but the normalization is flagged so build_text can warn and report it —
    never a silent coercion."""
    # Empty input is the documented default, NOT a normalization event.
    assert _resolve_text_mode("") == ("3d_text", "3d_text", False)
    # Unknown strings are flagged with the original requested value preserved.
    assert _resolve_text_mode("bogus") == ("3d_text", "bogus", True)
    # Legacy helper keeps returning the delivered mode for compat callers.
    assert _normalize_text_mode("bogus") == "3d_text"


def test_unknown_text_mode_warns_once_per_import(caplog):
    opts = types.SimpleNamespace()
    logger_name = "pdf_vector_importer.bl_text_builder"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        _warn_unknown_text_mode_once(opts, "bogus")
        _warn_unknown_text_mode_once(opts, "bogus")
    warnings = [
        record
        for record in caplog.records
        if "Unknown Blender text mode" in record.getMessage()
    ]
    assert len(warnings) == 1
    # The seen-set feeds the report's extra.text_mode_normalized_from note.
    assert opts._text_mode_normalization_warnings == {"bogus"}


@pytest.mark.parametrize(
    "mode",
    ["labels", "3d_text", "glyphs", "geometry"],
)
def test_normalize_text_mode_accepts_all_four(mode: str):
    assert _normalize_text_mode(mode) == mode
    assert _normalize_text_mode(mode.upper()) == mode


def test_extrusion_depth_positive_for_3d_text():
    depth = _text_extrusion_depth(0.01)
    assert depth > 0.0


def test_calibrated_text_size_preserves_nominal():
    item = NormalizedText(
        id=1,
        text="LONG CALLOUT TEXT",
        normalized="LONG CALLOUT TEXT",
        insertion=(0.0, 0.0),
        bbox=(0.0, 0.0, 10.0, 3.0),
        font_size=12.0,
        rotation=0.0,
    )

    assert _calibrated_text_size_mm(item) == item.font_size


def test_calibrated_text_size_preserves_vertical_nominal():
    item = NormalizedText(
        id=2,
        text="3 3/8",
        normalized="3 3/8",
        insertion=(0.0, 0.0),
        bbox=(0.0, 0.0, 2.0, 24.0),
        font_size=10.0,
        rotation=90.0,
    )

    assert _calibrated_text_size_mm(item) == item.font_size
