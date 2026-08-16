"""Blender dash patterns: pdfcadcore delivers PDF points, the builder draws mm.

1011 visual-oracle finding (Blender, all text modes): the column centerline
'[19.2 4.8 2.4 4.8] 0' pt rendered with a 31.2 MM pitch instead of 31.2 pt
(x2.83 = 72/25.4), hidden lines '[6 6]' were ~3x too coarse and short
bolt-line centerlines came out solid because the first 'dash' (19.2 mm)
was longer than the line.  ``primitive_extractor`` converts ``width`` with
``MM_PER_PT * scale`` but hands ``dash_pattern``/``dash_phase`` through in
raw points (the shared core stays byte-identical); the Blender adapter must
convert once where the primitive is queued.
"""
from __future__ import annotations

import importlib
import math
import sys
import types
import unittest
from unittest.mock import patch

import pytest


def _builder():
    bpy = sys.modules.setdefault("bpy", types.SimpleNamespace())
    if not hasattr(bpy, "types"):
        bpy.types = types.SimpleNamespace()
    for name in ("Collection", "Material", "Object"):
        if not hasattr(bpy.types, name):
            setattr(bpy.types, name, object)
    sys.modules.setdefault("bmesh", types.SimpleNamespace())
    builder = importlib.import_module("pdf_vector_importer.bl_geometry_builder")
    primitives = importlib.import_module("pdf_vector_importer.pdfcadcore.primitives")
    return builder, primitives


MM_PER_PT = 25.4 / 72.0


def _run_lengths(runs) -> list:
    lengths = []
    for run in runs:
        total = 0.0
        for (x0, y0), (x1, y1) in zip(run, run[1:]):  # noqa: B905 - pairwise
            total += math.hypot(x1 - x0, y1 - y0)
        lengths.append(total)
    return lengths


def _build_dashed_line(
    builder,
    primitives,
    *,
    dash_pattern,
    dash_phase=0.0,
    length_mm=50.0,
    config=None,
):
    """Queue one dashed line through build_page and capture the batched runs."""
    primitive = primitives.Primitive(
        id=5,
        type="line",
        points=[(0.0, 0.0), (length_mm, 0.0)],
        bbox=(0.0, 0.0, length_mm, 0.0),
        stroke_color=(0.0, 0.0, 0.0),
        dash_pattern=list(dash_pattern),
        dash_phase=float(dash_phase),
        line_width=0.25,
        closed=False,
    )
    page = primitives.PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        primitives=[primitive],
    )
    captured = {}

    def fake_multi_poly(name, runs, collection, line_width, material, z_offset_m=0.0):
        captured["runs"] = [list(run) for run in runs]
        return object()

    with (
        patch.object(builder, "_resolve_collection", return_value=object()),
        patch.object(builder, "_get_or_create_material", return_value=object()),
        patch.object(builder, "_create_multi_poly_curve", side_effect=fake_multi_poly),
    ):
        stats = builder.build_page(page, object(), config)
    return stats, captured.get("runs", [])


class TestBlenderDashPatternUnits(unittest.TestCase):
    def test_point_dash_pattern_is_converted_to_mm_before_splitting(self) -> None:
        # 1011 column centerline family: '[19.2 4.8]' pt -> 6.7733 mm dash,
        # 1.6933 mm gap (a 8.4667 mm cycle), NOT 19.2 mm / 4.8 mm.
        builder, primitives = _builder()
        stats, runs = _build_dashed_line(
            builder, primitives, dash_pattern=[19.2, 4.8], length_mm=50.0
        )
        self.assertEqual(stats["batched_curve_primitives"], 1)
        self.assertGreaterEqual(len(runs), 5)
        expected_dash = 19.2 * MM_PER_PT  # 6.7733 mm
        expected_gap = 4.8 * MM_PER_PT  # 1.6933 mm
        lengths = _run_lengths(runs)
        for length in lengths[:-1]:
            self.assertAlmostEqual(length, expected_dash, places=6)
        # gap between consecutive runs
        for first, second in zip(runs, runs[1:]):  # noqa: B905 - pairwise
            gap = second[0][0] - first[-1][0]
            self.assertAlmostEqual(gap, expected_gap, places=6)
        # A 50 mm line holds 5 full 8.4667 mm cycles + a 7.667 mm tail:
        # 6 dashes, the last one full (7.667 > 6.773).
        self.assertEqual(len(runs), 6)
        self.assertAlmostEqual(lengths[-1], expected_dash, places=6)

    def test_short_centerline_is_still_dashed_after_unit_fix(self) -> None:
        # 1011 bolt-line centerline: 43.7 pt = 15.4 mm long with
        # '[19.2 4.8 2.4 4.8]' pt.  Consumed as mm the first dash (19.2 mm)
        # covers the whole line -> solid.  In mm it shows its gaps.
        builder, primitives = _builder()
        length_mm = 43.7 * MM_PER_PT
        _stats, runs = _build_dashed_line(
            builder,
            primitives,
            dash_pattern=[19.2, 4.8, 2.4, 4.8],
            length_mm=length_mm,
        )
        # dash 6.77, gap 1.69, dot 0.85, gap 1.69 = 11.0 mm cycle -> the
        # 15.4 mm line carries dash, dot, dash(partial): three visible runs.
        self.assertEqual(len(runs), 3)
        lengths = _run_lengths(runs)
        self.assertAlmostEqual(lengths[0], 19.2 * MM_PER_PT, places=6)
        self.assertAlmostEqual(lengths[1], 2.4 * MM_PER_PT, places=6)

    def test_dash_phase_is_converted_with_the_pattern(self) -> None:
        # phase 19.2 pt == one full first dash: the line must START with the
        # 1.6933 mm gap and the first visible run begins at 1.6933 mm.
        builder, primitives = _builder()
        _stats, runs = _build_dashed_line(
            builder,
            primitives,
            dash_pattern=[19.2, 4.8],
            dash_phase=19.2,
            length_mm=50.0,
        )
        self.assertGreaterEqual(len(runs), 2)
        self.assertAlmostEqual(runs[0][0][0], 4.8 * MM_PER_PT, places=6)
        self.assertAlmostEqual(_run_lengths(runs)[0], 19.2 * MM_PER_PT, places=6)

    def test_page_scale_multiplies_dash_lengths_like_geometry(self) -> None:
        # With user_scale 2.0 the extractor doubles every model coordinate and
        # width; dash lengths must follow (2 * 6.7733 mm) or the pattern
        # visibly halves against the geometry.
        builder, primitives = _builder()
        _stats, runs = _build_dashed_line(
            builder,
            primitives,
            dash_pattern=[19.2, 4.8],
            length_mm=100.0,
            config={"pt_to_model_mm": MM_PER_PT * 2.0},
        )
        lengths = _run_lengths(runs)
        self.assertAlmostEqual(lengths[0], 2.0 * 19.2 * MM_PER_PT, places=6)
        gap = runs[1][0][0] - runs[0][-1][0]
        self.assertAlmostEqual(gap, 2.0 * 4.8 * MM_PER_PT, places=6)

    def test_import_engine_hands_the_page_scale_to_the_builder(self) -> None:
        # The engine already treats width/coordinates as MM_PER_PT * user_scale;
        # the builder config must carry the same factor for dashes.
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "pdf_vector_importer"
            / "bl_import_engine.py"
        ).read_text(encoding="utf-8")
        needle = '"pt_to_model_mm": _MM_PER_PT * import_cfg.user_scale'
        self.assertTrue(
            needle in source,
            "builder_config must carry pt_to_model_mm = _MM_PER_PT * user_scale",
        )


def test_dash_pattern_mm_helper_scales_pattern_and_phase() -> None:
    builder, _primitives = _builder()
    pattern, phase = builder._dash_pattern_to_model_mm([19.2, 4.8], 9.6, MM_PER_PT)
    assert pattern == pytest.approx([19.2 * MM_PER_PT, 4.8 * MM_PER_PT])
    assert phase == pytest.approx(9.6 * MM_PER_PT)
    assert builder._dash_pattern_to_model_mm(None, 0.0, MM_PER_PT) == (None, 0.0)
    assert builder._dash_pattern_to_model_mm([], 3.0, MM_PER_PT) == (None, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
