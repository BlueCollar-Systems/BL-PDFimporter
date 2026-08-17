#!/usr/bin/env python3
"""Regression: a both-sides weld symbol is TWO stacked fractions, not one.

Geometry is the pdfcadcore extraction of "1011 (1 OF 2) - Rev 0.pdf" page 1,
weld symbol at PDF pt (953.9, 937.8): '316' [949.5,927.5,966.8,941.5] + '/'
above the reference line and '316' [949.5,940.5,966.8,954.6] + '/' below it
(14 such stacked-over-stacked pairs on that sheet, 96 across the local
corpus).  Before this fix ``_merge_stacked_fractions`` Pattern A let the first
slash swallow both '316' spans (nearest_distance + _FRAC_Y_SPREAD_MM window)
and ``_dedupe_fraction_overlays`` then deleted the second, now bare, slash
(5.0/5.5 mm centre tolerance), so one '3/16' survived per symbol.
"""
from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path

MOD_ROOT = Path(__file__).resolve().parents[1] / "pdf_vector_importer"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.primitive_extractor import _merge_stacked_fractions  # noqa: E402
from pdfcadcore.primitives import NormalizedText, TextCharLayout  # noqa: E402


_MM_PER_PT = 25.4 / 72.0
_PDF_PAGE_HEIGHT_PT = 1726.3


def _model_quad(box: tuple[float, float, float, float]):
    return (
        (box[0], box[3]),
        (box[2], box[3]),
        (box[2], box[1]),
        (box[0], box[1]),
    )


def _rotate_target(point: tuple[float, float], rotate90: bool):
    return (-point[1], point[0]) if rotate90 else point


def _source_point(point: tuple[float, float]):
    return (
        point[0] / _MM_PER_PT,
        _PDF_PAGE_HEIGHT_PT - point[1] / _MM_PER_PT,
    )


def _character_truth(
    text: str,
    glyph_id: int,
    target_origin: tuple[float, float],
    target_quad,
    *,
    rotate90: bool,
) -> TextCharLayout:
    source_origin = _source_point(target_origin)
    source_quad = tuple(_source_point(point) for point in target_quad)
    source_x = [point[0] for point in source_quad]
    source_y = [point[1] for point in source_quad]
    rotated_origin = _rotate_target(target_origin, rotate90)
    rotated_quad = tuple(_rotate_target(point, rotate90) for point in target_quad)
    return TextCharLayout(
        text=text,
        glyph_id=glyph_id,
        source_origin_pdf=source_origin,
        source_bbox_pdf=(min(source_x), min(source_y), max(source_x), max(source_y)),
        source_quad_pdf=source_quad,
        target_origin=rotated_origin,
        target_quad=rotated_quad,
        advance_width=(
            (rotated_quad[1][0] - rotated_quad[0][0]) ** 2
            + (rotated_quad[1][1] - rotated_quad[0][1]) ** 2
        ) ** 0.5,
        glyph_height=(
            (rotated_quad[3][0] - rotated_quad[0][0]) ** 2
            + (rotated_quad[3][1] - rotated_quad[0][1]) ** 2
        ) ** 0.5,
    )


def _producer_layout(
    text: str,
    box: tuple[float, float, float, float],
    insertion: tuple[float, float],
    glyph_start: int,
    *,
    rotate90: bool,
) -> tuple[TextCharLayout, ...]:
    if text == "316":
        x0, y0, x1, y1 = box
        width = x1 - x0
        height = y1 - y0
        specs = (
            (
                "3",
                (x0 + 0.37 * width, y0 + 0.59 * height),
                _model_quad((x0 + 0.20 * width, y0 + 0.50 * height, x0 + 0.65 * width, y1)),
            ),
            (
                "1",
                (x0 + 0.17 * width, y0 + 0.03 * height),
                _model_quad((x0, y0, x0 + 0.46 * width, y0 + 0.50 * height)),
            ),
            (
                "6",
                (x0 + 0.58 * width, y0 + 0.03 * height),
                _model_quad((x0 + 0.54 * width, y0, x1, y0 + 0.50 * height)),
            ),
        )
    else:
        specs = ((text, insertion, _model_quad(box)),)
    return tuple(
        _character_truth(
            char,
            glyph_start + offset,
            origin,
            quad,
            rotate90=rotate90,
        )
        for offset, (char, origin, quad) in enumerate(specs)
    )


def _both_sides_1011_items(rotate90: bool = False) -> list[NormalizedText]:
    # (text, insertion, bbox, font_size) -- model mm, 1011 p1 as extracted
    raw = [
        ("316", (334.95, 279.05), (334.95, 276.86, 341.06, 281.81), 3.55),
        ("/", (336.51, 278.16), (336.51, 277.22, 337.69, 281.45), 4.23),
        ("316", (334.95, 274.44), (334.95, 272.25, 341.06, 277.20), 3.55),
        ("/", (336.51, 273.51), (336.51, 272.56, 337.69, 276.79), 4.23),
    ]
    items = []
    for idx, (text, ins, box, size) in enumerate(raw, start=1):
        source_span_quad = tuple(_source_point(point) for point in _model_quad(box))
        source_x = [point[0] for point in source_span_quad]
        source_y = [point[1] for point in source_span_quad]
        target_span_quad = tuple(
            _rotate_target(point, rotate90) for point in _model_quad(box)
        )
        target_x = [point[0] for point in target_span_quad]
        target_y = [point[1] for point in target_span_quad]
        layout = _producer_layout(
            text,
            box,
            ins,
            idx * 10,
            rotate90=rotate90,
        )
        items.append(
            NormalizedText(
                id=idx,
                text=text,
                normalized=text,
                insertion=_rotate_target(ins, rotate90),
                bbox=(min(target_x), min(target_y), max(target_x), max(target_y)),
                font_size=size,
                rotation=90.0 if rotate90 else 0.0,
                font_name="SyntheticWeldFraction",
                color=(0.0, 0.0, 0.0),
                page_number=1,
                source_bbox_pdf=(
                    min(source_x),
                    min(source_y),
                    max(source_x),
                    max(source_y),
                ),
                source_quad_pdf=source_span_quad,
                target_quad_model=target_span_quad,
                advance_width=(
                    (target_span_quad[1][0] - target_span_quad[0][0]) ** 2
                    + (target_span_quad[1][1] - target_span_quad[0][1]) ** 2
                ) ** 0.5,
                glyph_height=(
                    (target_span_quad[3][0] - target_span_quad[0][0]) ** 2
                    + (target_span_quad[3][1] - target_span_quad[0][1]) ** 2
                ) ** 0.5,
                source_char_layout=layout,
                requires_individual_positioning=True,
            )
        )
    return items


def _assert_refused_without_mutation(items: list[NormalizedText]) -> None:
    snapshots = [repr(vars(item)) for item in items]

    result = _merge_stacked_fractions(items)

    assert result is items
    assert all(actual is original for actual, original in zip(result, items, strict=True))
    assert [repr(vars(item)) for item in result] == snapshots


def test_layout_free_both_sides_weld_items_are_refused_identity_preserved() -> None:
    items = _both_sides_1011_items()
    for item in items:
        item.source_char_layout = ()
        item.requires_individual_positioning = False

    _assert_refused_without_mutation(items)


def test_partial_character_layout_is_refused_identity_preserved() -> None:
    items = _both_sides_1011_items()
    items[0].source_char_layout = items[0].source_char_layout[:1]

    _assert_refused_without_mutation(items)


def test_both_sides_weld_symbol_keeps_both_stacked_fractions() -> None:
    merged = _merge_stacked_fractions(_both_sides_1011_items())
    assert [item.text for item in merged] == ["3/16", "3/16"]
    top, bottom = sorted(merged, key=lambda it: -it.insertion[1])
    # Each fraction sits at its own slash; its bbox does not span the other half.
    assert top.insertion == (336.51, 278.16)
    assert bottom.insertion == (336.51, 273.51)
    assert top.bbox[1] >= 276.8 and bottom.bbox[3] <= 277.3
    # Both retain the same observed slash font size; no synthetic 0.6x clamp.
    assert abs(top.font_size - bottom.font_size) < 1e-6


def test_both_sides_weld_symbol_rotated_90_keeps_both() -> None:
    merged = _merge_stacked_fractions(_both_sides_1011_items(rotate90=True))
    assert [item.text for item in merged] == ["3/16", "3/16"]


def test_coincident_overlay_stack_still_merges_once() -> None:
    # A printer that draws the SAME stack twice at the SAME place is an overlay;
    # that (and only that) still collapses to one fraction.
    a = _both_sides_1011_items()[:2]
    b = [
        replace(
            item,
            id=index,
            source_char_layout=tuple(replace(char) for char in item.source_char_layout),
        )
        for index, item in enumerate(a, start=3)
    ]
    assert [item.text for item in _merge_stacked_fractions(a + b)] == ["3/16"]


def test_leftover_slash_inside_its_own_fraction_is_dropped_but_not_a_neighbour() -> None:
    all_items = _both_sides_1011_items()
    top = all_items[:2]
    dup_slash = replace(
        top[1],
        id=9,
        source_char_layout=tuple(replace(char) for char in top[1].source_char_layout),
    )
    far_slash = replace(
        all_items[3],
        id=10,
        source_char_layout=tuple(replace(char) for char in all_items[3].source_char_layout),
    )
    merged = _merge_stacked_fractions(top + [dup_slash, far_slash])
    assert [item.text for item in merged] == ["3/16", "/"]
