# -*- coding: utf-8 -*-
"""Verified item-scoped Blender text representation delivery."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Optional, Tuple

import bpy

from .packed_assets import PackedAssetError, pack_and_verify_bytes, verify_packed_sha256
from .pdfcadcore.primitives import NormalizedText
from .pdfcadcore.text_scale import calibrate_text_size_to_bbox
from .text_delivery import (
    IMPORTER_ID,
    ZERO_INK_CHARACTER_MANIFEST_SCHEMA,
    ZERO_INK_SOURCE_MANIFEST_SCHEMA,
    AttemptOutcome,
    ZeroInkReconciliationAuthority,
    classify_text_ink,
    deliver_item,
    freeze_zero_ink_source_manifest,
    make_zero_ink_character_manifest,
    normalize_representation,
    text_has_visible_ink,
    text_is_unicode_whitespace,
    text_is_zero_ink,
)


MM_TO_M = 0.001
_FONT_SIZE_SCALE = 1.0
_FONT_CACHE: Dict[str, bpy.types.VectorFont] = {}
_EXACT_GLYPH_DESIGN_BOUNDS_CACHE: Dict[
    Tuple[str, int, int], Optional[Tuple[float, float, float, float]]
] = {}
_EXACT_GLYPH_CUBIC_CONTOURS_CACHE: Dict[Tuple[str, int, int], tuple] = {}
_TEXT_MODES = {"labels", "text", "3d_text", "glyphs", "geometry", "raster"}
_METRIC_INK_BOUNDS_TOLERANCE_M = 5e-5
LOGGER = logging.getLogger(__name__)


class _OwnedConstructionError(RuntimeError):
    """Construction failed and an attempt-owned host datablock still exists."""

    def __init__(self, message: str, *, owned_objects=(), owned_datablocks=()):
        super().__init__(message)
        self.owned_objects = tuple(owned_objects)
        self.owned_datablocks = tuple(owned_datablocks)


def _proof_identity(item_id: str, page_number: int, source_span_id: int) -> Dict[str, Any]:
    return {
        "importer_id": IMPORTER_ID,
        "item_id": str(item_id),
        "page_number": int(page_number),
        "source_span_id": int(source_span_id),
    }


def _host_capability_evidence(
    item_id: str,
    page_number: int,
    source_span_id: int,
    capability: str,
) -> Dict[str, Any]:
    version = getattr(getattr(bpy, "app", None), "version", None)
    return {
        **_proof_identity(item_id, page_number, source_span_id),
        "host": "blender",
        "host_version": (
            list(version)
            if isinstance(version, (list, tuple))
            else str(version or "")
        ),
        "capability": str(capability),
        "capability_present": False,
    }


def _normalize_style(style: str) -> str:
    key = (style or "source").strip().lower()
    return key if key in {"source", "blueprint", "high_contrast"} else "source"


def _resolve_text_mode(text_mode: str) -> Tuple[str, str, bool]:
    requested = normalize_representation(text_mode)
    return requested, requested, False


def _normalize_text_mode(text_mode: str) -> str:
    return _resolve_text_mode(text_mode)[0]


def _text_extrusion_depth(font_size: float) -> float:
    return max(float(font_size) * 0.12, 0.00025)


def _calibrated_text_size_mm(text_item: NormalizedText) -> float:
    return calibrate_text_size_to_bbox(
        str(getattr(text_item, "text", "") or ""),
        float(getattr(text_item, "font_size", 0.0) or 0.0),
        getattr(text_item, "bbox", None),
        float(getattr(text_item, "rotation", 0.0) or 0.0),
        min_size=0.1,
    )


def _styled_text_color(
    style: str,
    source_color: Optional[Tuple[float, float, float]] = None,
) -> Tuple[float, float, float]:
    style_key = _normalize_style(style)
    if style_key == "blueprint":
        return (0.36, 0.74, 0.98)
    if style_key == "high_contrast":
        return (0.95, 0.95, 0.95)
    return source_color if source_color is not None else (0.06, 0.06, 0.06)


def _should_center_anchor(
    text_item: NormalizedText,
    *,
    strict_text_fidelity: bool = True,
) -> bool:
    del text_item, strict_text_fidelity
    # Source insertion is a PDF baseline anchor. Re-centering changes identity.
    return False


def _get_or_create_text_material(
    style: str,
    source_color: Optional[Tuple[float, float, float]] = None,
) -> bpy.types.Material:
    style_key = _normalize_style(style)
    r, g, b = _styled_text_color(style_key, source_color=source_color)
    if style_key == "source" and source_color is not None:
        mat_name = f"PDF_Text_{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"
    else:
        mat_name = f"PDF_Text_{style_key}"
    # Every attempt owns its material. Reusing a deterministic user material
    # can silently inherit the wrong color or node graph.
    material = bpy.data.materials.new(name=mat_name)
    try:
        material.diffuse_color = (r, g, b, 1.0)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        shader = nodes.new(type="ShaderNodeBsdfPrincipled")
        output = nodes.new(type="ShaderNodeOutputMaterial")
        shader.inputs["Base Color"].default_value = (r, g, b, 1.0)
        shader.inputs["Alpha"].default_value = 1.0
        links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    except Exception as exc:
        try:
            bpy.data.materials.remove(material)
        except Exception as cleanup_exc:
            raise _OwnedConstructionError(
                "text material construction and rollback failed: "
                f"creation={type(exc).__name__}:{exc}; "
                f"cleanup={type(cleanup_exc).__name__}:{cleanup_exc}",
                owned_datablocks=(material,),
            ) from exc
        raise
    return material


def _fit_text_to_bbox(obj: bpy.types.Object, text_item: NormalizedText) -> None:
    """Correct source width and height within the requested representation."""
    try:
        current_width = abs(float(obj.dimensions[0]))
        current_height = abs(float(obj.dimensions[1]))
        target_width = float(getattr(text_item, "advance_width", 0.0) or 0.0) * MM_TO_M
        target_height = float(getattr(text_item, "glyph_height", 0.0) or 0.0) * MM_TO_M
    except (AttributeError, IndexError, TypeError, ValueError):
        return
    if target_width > 1e-9 and current_width > 1e-9:
        obj.scale[0] *= target_width / current_width
    if target_height > 1e-9 and current_height > 1e-9:
        obj.scale[1] *= target_height / current_height
    try:
        obj["pdf_target_width_mm"] = target_width / MM_TO_M if target_width else 0.0
        obj["pdf_target_height_mm"] = target_height / MM_TO_M if target_height else 0.0
    except Exception:
        pass


def _quad_affine_coefficients(quad, *, unit_scale: float = MM_TO_M):
    """Return x'=ax+by+tx, y'=cx+dy+ty for a PDF-ordered quad.

    Normalized local corners map as (0,1)=UL, (1,1)=UR,
    (1,0)=LR, and (0,0)=LL. Negative and sheared axes are retained.
    """
    if quad is None or len(quad) != 4:
        raise ValueError("a four-corner target quad is required")
    ul, ur, lr, ll = (
        (float(point[0]) * unit_scale, float(point[1]) * unit_scale)
        for point in quad
    )
    values = (
        lr[0] - ll[0],
        ul[0] - ll[0],
        lr[1] - ll[1],
        ul[1] - ll[1],
        ll[0],
        ll[1],
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("target quad contains non-finite affine values")
    # A PDF text transform must form a parallelogram. Refuse to hide a bad
    # extraction by silently averaging incompatible fourth corners.
    predicted_ur = (values[0] + values[1] + values[4], values[2] + values[3] + values[5])
    tolerance = max(1e-9, max(abs(value) for value in values) * 1e-6)
    if abs(predicted_ur[0] - ur[0]) > tolerance or abs(predicted_ur[1] - ur[1]) > tolerance:
        raise ValueError("target text quad is not affine/parallelogram shaped")
    return values


def _apply_affine_2d(coefficients, x: float, y: float):
    a, b, c, d, tx, ty = (float(value) for value in coefficients)
    return a * float(x) + b * float(y) + tx, c * float(x) + d * float(y) + ty


def _affine_matrix_values(
    *,
    local_bounds,
    target_quad,
    z: float,
    unit_scale: float = MM_TO_M,
):
    """Map an evaluated local rectangle onto an exact PDF affine quad."""
    x0, y0, x1, y1 = (float(value) for value in local_bounds)
    width = x1 - x0
    height = y1 - y0
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1, z)):
        raise ValueError("local text bounds contain non-finite values")
    if abs(width) <= 1e-12 or abs(height) <= 1e-12:
        raise ValueError("local text bounds have zero affine extent")
    normalized = _quad_affine_coefficients(target_quad, unit_scale=unit_scale)
    nx, ny, mx, my, tx, ty = normalized
    # Normalized x=(local_x-x0)/width and y=(local_y-y0)/height.
    ax = nx / width
    bx = ny / height
    ay = mx / width
    by = my / height
    translate_x = tx - ax * x0 - bx * y0
    translate_y = ty - ay * x0 - by * y0
    matrix = (
        (ax, bx, 0.0, translate_x),
        (ay, by, 0.0, translate_y),
        (0.0, 0.0, 1.0, float(z)),
        (0.0, 0.0, 0.0, 1.0),
    )
    if not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("computed text affine matrix is non-finite")
    return matrix


def _metric_character_matrix_values(
    *,
    local_advance: float,
    local_line_height: float,
    target_origin,
    target_quad,
    z: float,
    local_baseline_y: float = 0.0,
    unit_scale: float = MM_TO_M,
    allow_zero_advance: bool = False,
):
    """Map exact-font advance/line axes while pinning the PDF baseline origin."""
    advance = float(local_advance)
    line_height = float(local_line_height)
    baseline_y = float(local_baseline_y)
    if (
        not math.isfinite(advance)
        or not math.isfinite(line_height)
        or not math.isfinite(baseline_y)
        or advance < 0.0
        or (advance <= 1e-12 and not allow_zero_advance)
        or line_height <= 1e-12
    ):
        raise ValueError("local exact-font character metrics are invalid")
    if target_quad is None or len(target_quad) != 4:
        raise ValueError("a four-corner character target quad is required")
    if target_origin is None or len(target_origin) < 2:
        raise ValueError("a character baseline origin is required")
    # This also rejects a non-affine fourth corner instead of averaging it.
    _quad_affine_coefficients(target_quad, unit_scale=unit_scale)
    ul, ur, _lr, ll = tuple(
        (float(point[0]) * unit_scale, float(point[1]) * unit_scale)
        for point in target_quad
    )
    origin = (
        float(target_origin[0]) * unit_scale,
        float(target_origin[1]) * unit_scale,
    )
    hx, hy = ur[0] - ul[0], ur[1] - ul[1]
    vx, vy = ul[0] - ll[0], ul[1] - ll[1]
    if advance <= 1e-12:
        axis_tolerance = 1e-12
        if math.hypot(hx, hy) > axis_tolerance:
            raise ValueError(
                "zero local advance requires a degenerate target horizontal axis"
            )
        ax = ay = 0.0
    else:
        ax, ay = hx / advance, hy / advance
    bx, by = vx / line_height, vy / line_height
    tx = origin[0] - bx * baseline_y
    ty = origin[1] - by * baseline_y
    matrix = (
        (ax, bx, 0.0, tx),
        (ay, by, 0.0, ty),
        (0.0, 0.0, 1.0, float(z)),
        (0.0, 0.0, 0.0, 1.0),
    )
    if not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("computed metric character matrix is non-finite")
    return matrix


def _factor_affine_matrix_values(matrix_values):
    """Factor a 2D affine into two shear-free Blender object transforms."""
    matrix = tuple(tuple(float(value) for value in row) for row in matrix_values)
    if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
        raise ValueError("a 4x4 affine matrix is required")
    if not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("affine matrix contains non-finite values")
    a, b = matrix[0][0], matrix[0][1]
    c, d = matrix[1][0], matrix[1][1]
    determinant = a * d - b * c
    if abs(determinant) <= 1e-15:
        raise ValueError("affine character transform is singular")

    # V diagonalizes A^T A.  Parent=A*V has orthogonal columns and child=V^T
    # is a pure rotation, so Blender can store both without dropping shear.
    p = a * a + c * c
    q = a * b + c * d
    r = b * b + d * d
    theta = 0.5 * math.atan2(2.0 * q, p - r)
    cosine = math.cos(theta)
    sine = math.sin(theta)
    parent = (
        (
            a * cosine + b * sine,
            -a * sine + b * cosine,
            0.0,
            matrix[0][3],
        ),
        (
            c * cosine + d * sine,
            -c * sine + d * cosine,
            0.0,
            matrix[1][3],
        ),
        (0.0, 0.0, matrix[2][2], matrix[2][3]),
        (0.0, 0.0, 0.0, 1.0),
    )
    child = (
        (cosine, sine, 0.0, 0.0),
        (-sine, cosine, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    return parent, child


def _matrix_requires_affine_carrier(matrix_values) -> bool:
    a, b = float(matrix_values[0][0]), float(matrix_values[0][1])
    c, d = float(matrix_values[1][0]), float(matrix_values[1][1])
    first_length = math.hypot(a, c)
    second_length = math.hypot(b, d)
    if first_length <= 1e-15 or second_length <= 1e-15:
        return True
    normalized_dot = (a * b + c * d) / (first_length * second_length)
    return abs(normalized_dot) > 1e-8


def _quad_requires_full_affine(quad) -> bool:
    if quad is None or len(quad) != 4:
        return False
    ul, ur, _lr, ll = tuple(
        (float(point[0]), float(point[1])) for point in quad
    )
    x_axis = (ur[0] - ul[0], ur[1] - ul[1])
    y_axis = (ul[0] - ll[0], ul[1] - ll[1])
    x_len = math.hypot(*x_axis)
    y_len = math.hypot(*y_axis)
    if x_len <= 1e-12 or y_len <= 1e-12:
        return True
    dot = (x_axis[0] * y_axis[0] + x_axis[1] * y_axis[1]) / (x_len * y_len)
    determinant = x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0]
    return abs(dot) > 1e-6 or determinant < 0.0


def _exact_glyph_design_bounds(asset, glyph_id: int):
    """Return source-font design bounds for one glyph, never host-derived bounds."""
    try:
        data = bytes(asset.usable_bytes)
        digest = str(asset.usable_sha256 or "")
        expected_upem = int(asset.units_per_em)
        glyph_id = int(glyph_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("exact embedded glyph bound source is unavailable") from exc
    if (
        not data
        or not digest
        or sha256(data).hexdigest() != digest
        or expected_upem <= 0
        or glyph_id < 0
    ):
        raise RuntimeError("exact embedded glyph bound source is invalid")
    cache_key = (digest, glyph_id, expected_upem)
    if cache_key in _EXACT_GLYPH_DESIGN_BOUNDS_CACHE:
        return _EXACT_GLYPH_DESIGN_BOUNDS_CACHE[cache_key]

    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.ttLib import TTFont, TTLibError
    except ImportError as exc:
        raise RuntimeError("fontTools is unavailable for exact glyph bounds") from exc

    font = None
    try:
        font = TTFont(BytesIO(data), lazy=False, recalcTimestamp=False)
        if int(font["head"].unitsPerEm) != expected_upem:
            raise RuntimeError("embedded font metric metadata does not match glyph design units")
        glyph_order = tuple(font.getGlyphOrder())
        glyph_name = glyph_order[glyph_id]
        glyph_set = font.getGlyphSet()
        pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        if pen.bounds is None:
            bounds = None
        else:
            bounds = tuple(float(value) for value in pen.bounds)
            if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
                raise RuntimeError("exact embedded glyph bounds are non-finite")
    except RuntimeError:
        raise
    except (AttributeError, IndexError, KeyError, OSError, TTLibError, TypeError, ValueError) as exc:
        raise RuntimeError("exact embedded glyph bounds are unavailable") from exc
    finally:
        if font is not None:
            font.close()
    _EXACT_GLYPH_DESIGN_BOUNDS_CACHE[cache_key] = bounds
    return bounds


def _exact_glyph_cubic_contours(asset, glyph_id: int):
    """Return decomposed source-font contours as exact cubic segments."""
    try:
        data = bytes(asset.usable_bytes)
        digest = str(asset.usable_sha256 or "")
        expected_upem = int(asset.units_per_em)
        glyph_id = int(glyph_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("exact embedded glyph contour source is unavailable") from exc
    if (
        not data
        or not digest
        or sha256(data).hexdigest() != digest
        or expected_upem <= 0
        or glyph_id < 0
    ):
        raise RuntimeError("exact embedded glyph contour source is invalid")
    cache_key = (digest, glyph_id, expected_upem)
    if cache_key in _EXACT_GLYPH_CUBIC_CONTOURS_CACHE:
        return _EXACT_GLYPH_CUBIC_CONTOURS_CACHE[cache_key]

    try:
        from fontTools.pens.basePen import BasePen
        from fontTools.ttLib import TTFont, TTLibError
    except ImportError as exc:
        raise RuntimeError("fontTools is unavailable for exact glyph contours") from exc

    class ExactCubicContourPen(BasePen):
        def __init__(self, glyph_set):
            super().__init__(glyph_set)
            self.contours = []
            self._contour_start = None
            self._segments = []

        @staticmethod
        def _point(value):
            point = (float(value[0]), float(value[1]))
            if not all(math.isfinite(component) for component in point):
                raise RuntimeError("exact embedded glyph contour is non-finite")
            return point

        @staticmethod
        def _line_controls(start, end):
            return (
                (
                    start[0] + (end[0] - start[0]) / 3.0,
                    start[1] + (end[1] - start[1]) / 3.0,
                ),
                (
                    start[0] + 2.0 * (end[0] - start[0]) / 3.0,
                    start[1] + 2.0 * (end[1] - start[1]) / 3.0,
                ),
            )

        def _append_line(self, end):
            start = self._point(self._getCurrentPoint())
            end = self._point(end)
            control_1, control_2 = self._line_controls(start, end)
            self._segments.append((start, control_1, control_2, end))

        def _moveTo(self, point):
            if self._contour_start is not None or self._segments:
                raise RuntimeError("exact embedded glyph contour nesting is invalid")
            self._contour_start = self._point(point)

        def _lineTo(self, point):
            self._append_line(point)

        def _curveToOne(self, control_1, control_2, end):
            self._segments.append((
                self._point(self._getCurrentPoint()),
                self._point(control_1),
                self._point(control_2),
                self._point(end),
            ))

        def _qCurveToOne(self, control, end):
            start = self._point(self._getCurrentPoint())
            control = self._point(control)
            end = self._point(end)
            control_1 = (
                start[0] + (2.0 / 3.0) * (control[0] - start[0]),
                start[1] + (2.0 / 3.0) * (control[1] - start[1]),
            )
            control_2 = (
                end[0] + (2.0 / 3.0) * (control[0] - end[0]),
                end[1] + (2.0 / 3.0) * (control[1] - end[1]),
            )
            self._segments.append((start, control_1, control_2, end))

        def _closePath(self):
            if self._contour_start is None:
                raise RuntimeError("exact embedded glyph contour has no start point")
            current = self._point(self._getCurrentPoint())
            if current != self._contour_start:
                self._append_line(self._contour_start)
            if not self._segments:
                raise RuntimeError("exact embedded glyph contour has no segments")
            self.contours.append(tuple(self._segments))
            self._contour_start = None
            self._segments = []

        def _endPath(self):
            raise RuntimeError("open embedded glyph contours are unsupported")

    font = None
    try:
        font = TTFont(BytesIO(data), lazy=False, recalcTimestamp=False)
        if int(font["head"].unitsPerEm) != expected_upem:
            raise RuntimeError(
                "embedded font metric metadata does not match glyph design units"
            )
        glyph_order = tuple(font.getGlyphOrder())
        glyph_name = glyph_order[glyph_id]
        glyph_set = font.getGlyphSet()
        pen = ExactCubicContourPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        contours = tuple(pen.contours)
        if not contours:
            raise RuntimeError("visible embedded glyph has no exact contours")
    except RuntimeError:
        raise
    except (
        AttributeError,
        IndexError,
        KeyError,
        OSError,
        TTLibError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError("exact embedded glyph contours are unavailable") from exc
    finally:
        if font is not None:
            font.close()
    _EXACT_GLYPH_CUBIC_CONTOURS_CACHE[cache_key] = contours
    return contours


def _cubic_axis_values_at_extrema(control_values):
    p0, p1, p2, p3 = (float(value) for value in control_values)
    cubic = -p0 + 3.0 * p1 - 3.0 * p2 + p3
    quadratic = 3.0 * p0 - 6.0 * p1 + 3.0 * p2
    linear = -3.0 * p0 + 3.0 * p1
    derivative_a = 3.0 * cubic
    derivative_b = 2.0 * quadratic
    derivative_c = linear
    coefficient_scale = max(
        1.0,
        abs(derivative_a),
        abs(derivative_b),
        abs(derivative_c),
    )
    epsilon = coefficient_scale * 1e-14
    roots = []
    if abs(derivative_a) <= epsilon:
        if abs(derivative_b) > epsilon:
            roots.append(-derivative_c / derivative_b)
    else:
        discriminant = derivative_b * derivative_b - (
            4.0 * derivative_a * derivative_c
        )
        discriminant_epsilon = coefficient_scale * coefficient_scale * 1e-14
        if discriminant >= -discriminant_epsilon:
            root = math.sqrt(max(0.0, discriminant))
            denominator = 2.0 * derivative_a
            roots.extend((
                (-derivative_b - root) / denominator,
                (-derivative_b + root) / denominator,
            ))
    parameters = [0.0, 1.0]
    parameters.extend(value for value in roots if 0.0 < value < 1.0)
    return tuple(
        ((cubic * value + quadratic) * value + linear) * value + p0
        for value in parameters
    )


def _metric_expected_world_ink_bounds(metric_evidence, matrix_values):
    """Transform exact source glyph ink through the intended metric affine."""
    source_bounds = metric_evidence.get("source_ink_bounds_design_units")
    if source_bounds is None:
        return None
    x0, y0, x1, y1 = (float(value) for value in source_bounds)
    unit_scale = float(metric_evidence["design_unit_scale"])
    baseline_y = float(metric_evidence["local_baseline_y"])
    local_bounds = (
        x0 * unit_scale,
        y0 * unit_scale + baseline_y,
        x1 * unit_scale,
        y1 * unit_scale + baseline_y,
    )
    matrix = tuple(tuple(float(value) for value in row) for row in matrix_values)
    if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
        raise ValueError("a 4x4 metric affine matrix is required for glyph bounds")
    source_contours = metric_evidence.get("source_ink_contours_design_units")
    if source_contours is not None:
        world_x_values = []
        world_y_values = []
        for contour in tuple(source_contours):
            for segment in tuple(contour):
                if len(segment) != 4:
                    raise ValueError("an exact cubic glyph segment requires four points")
                controls = []
                for point in segment:
                    design_x, design_y = float(point[0]), float(point[1])
                    local_x = design_x * unit_scale
                    local_y = design_y * unit_scale + baseline_y
                    controls.append((
                        matrix[0][0] * local_x
                        + matrix[0][1] * local_y
                        + matrix[0][3],
                        matrix[1][0] * local_x
                        + matrix[1][1] * local_y
                        + matrix[1][3],
                    ))
                world_x_values.extend(
                    _cubic_axis_values_at_extrema(
                        tuple(point[0] for point in controls)
                    )
                )
                world_y_values.extend(
                    _cubic_axis_values_at_extrema(
                        tuple(point[1] for point in controls)
                    )
                )
        if not world_x_values or not world_y_values:
            raise ValueError("exact source glyph contours contain no ink segments")
        values = (*world_x_values, *world_y_values)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("computed exact glyph world bounds are non-finite")
        return (
            min(world_x_values),
            min(world_y_values),
            max(world_x_values),
            max(world_y_values),
        )

    points = tuple(
        (
            matrix[0][0] * x + matrix[0][1] * y + matrix[0][3],
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][3],
        )
        for x, y in (
            (local_bounds[0], local_bounds[1]),
            (local_bounds[2], local_bounds[1]),
            (local_bounds[2], local_bounds[3]),
            (local_bounds[0], local_bounds[3]),
        )
    )
    values = tuple(value for point in points for value in point)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("computed exact glyph world bounds are non-finite")
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _positioned_font_axis_metrics_values(
    text_item,
    *,
    size: float,
    baseline_alignment: str,
) -> Dict[str, Any]:
    """Return the exact local axes used by both source proof and runtime."""
    asset = getattr(text_item, "font_asset", None)
    glyph_id = getattr(text_item, "source_glyph_id", None)
    source_text = str(getattr(text_item, "text", "") or "")
    zero_ink_identity = classify_text_ink(source_text) == "zero_ink"
    if zero_ink_identity and (
        glyph_id is None or not text_is_unicode_whitespace(source_text)
    ):
        try:
            local_advance = abs(float(text_item.advance_width)) * MM_TO_M
            local_line_height = abs(float(text_item.glyph_height)) * MM_TO_M
            local_baseline_y = (
                abs(float(getattr(text_item, "baseline_descent", 0.0) or 0.0))
                * MM_TO_M
                if baseline_alignment == "BOTTOM"
                else 0.0
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "source-layout metrics are unavailable for positioned zero-ink character"
            ) from exc
        if (
            not math.isfinite(local_advance)
            or not math.isfinite(local_line_height)
            or not math.isfinite(local_baseline_y)
            or local_advance < 0.0
            or local_line_height <= 0.0
        ):
            raise RuntimeError(
                "source-layout metrics are invalid for positioned zero-ink character"
            )
        return {
            "glyph_id": int(glyph_id) if glyph_id is not None else -1,
            "units_per_em": 0,
            "ascender": 0,
            "descender": 0,
            "advance_units": 0,
            "line_height_units": 0,
            "design_unit_scale": 0.0,
            "local_advance": local_advance,
            "local_matrix_horizontal_extent": local_advance,
            "matrix_horizontal_extent_source": "source_layout_advance",
            "local_line_height": local_line_height,
            "local_baseline_y": local_baseline_y,
            "source_ink_bounds_design_units": None,
            "metric_source": "source_layout_zero_ink",
            "zero_ink_identity": True,
            "zero_advance_logical_proof": local_advance <= 1e-12,
        }
    try:
        glyph_id = int(glyph_id)
        ascender = int(asset.ascender)
        descender = int(asset.descender)
        units_per_em = int(asset.units_per_em)
        advances = tuple(asset.glyph_advances)
        advance_units = int(advances[glyph_id])
        size = float(size)
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "exact embedded font metrics are unavailable for positioned character"
        ) from exc
    line_height_units = ascender - descender
    if (
        glyph_id < 0
        or units_per_em <= 0
        or line_height_units <= 0
        or advance_units < 0
        or not math.isfinite(size)
        or size <= 0.0
    ):
        raise RuntimeError("exact embedded font metrics are invalid for positioned character")
    design_unit_scale = size / float(units_per_em)
    source_ink_bounds = (
        None if zero_ink_identity else _exact_glyph_design_bounds(asset, glyph_id)
    )
    if source_ink_bounds is None and not zero_ink_identity:
        raise RuntimeError("visible positioned character has no exact source glyph ink bounds")
    local_advance = float(advance_units) * design_unit_scale
    local_matrix_horizontal_extent = local_advance
    matrix_horizontal_extent_source = "embedded_font_glyph_advance"
    if advance_units == 0 and not zero_ink_identity:
        ink_x0, _ink_y0, ink_x1, _ink_y1 = source_ink_bounds
        local_matrix_horizontal_extent = (
            float(ink_x1) - float(ink_x0)
        ) * design_unit_scale
        if (
            not math.isfinite(local_matrix_horizontal_extent)
            or local_matrix_horizontal_extent <= 1e-12
        ):
            raise RuntimeError(
                "visible zero-advance glyph has no finite exact contour extent"
            )
        matrix_horizontal_extent_source = "exact_source_glyph_ink_width"
    local_baseline_y = (
        -float(descender) * design_unit_scale
        if baseline_alignment == "BOTTOM"
        else 0.0
    )
    return {
        "glyph_id": glyph_id,
        "units_per_em": units_per_em,
        "ascender": ascender,
        "descender": descender,
        "advance_units": advance_units,
        "line_height_units": line_height_units,
        "design_unit_scale": design_unit_scale,
        "local_advance": local_advance,
        "local_matrix_horizontal_extent": local_matrix_horizontal_extent,
        "matrix_horizontal_extent_source": matrix_horizontal_extent_source,
        "local_line_height": float(line_height_units) * design_unit_scale,
        "local_baseline_y": local_baseline_y,
        "source_ink_bounds_design_units": source_ink_bounds,
        "metric_source": "embedded_font_glyph_metrics",
        "zero_ink_identity": zero_ink_identity,
        "zero_advance_logical_proof": (
            zero_ink_identity and local_advance <= 1e-12
        ),
    }


def _positioned_font_axis_metrics(obj, text_item) -> Dict[str, Any]:
    return _positioned_font_axis_metrics_values(
        text_item,
        size=float(obj.data.size),
        baseline_alignment=str(obj.get("pdf_baseline_alignment", "") or ""),
    )


def _probe_positioned_baseline_alignment() -> str:
    """Select and cleanly prove the exact FONT baseline enum before sealing proof."""
    curves = getattr(getattr(bpy, "data", None), "curves", None)
    new = getattr(curves, "new", None)
    remove = getattr(curves, "remove", None)
    if not callable(new) or not callable(remove):
        raise RuntimeError("Blender FONT baseline capability probe is unavailable")
    probe = None
    selected = None
    probe_error = None
    cleanup_error = None
    try:
        probe = new(name="BCPDF_PositionedBaselineProbe", type="FONT")
        for alignment in ("BOTTOM_BASELINE", "BOTTOM"):
            try:
                probe.align_y = alignment
                if str(getattr(probe, "align_y", "") or "") == alignment:
                    selected = alignment
                    break
            except Exception as exc:
                probe_error = exc
        if selected is None:
            raise RuntimeError(
                "Blender supports neither required positioned baseline alignment"
            ) from probe_error
    finally:
        if probe is not None:
            for _attempt in range(2):
                try:
                    remove(probe)
                    cleanup_error = None
                    break
                except Exception as exc:
                    cleanup_error = exc
    if cleanup_error is not None:
        raise RuntimeError(
            "Blender FONT baseline capability probe cleanup failed"
        ) from cleanup_error
    return selected


def _apply_target_quad_affine(
    obj,
    text_item,
    z_offset_m: float,
    collection=None,
    positioned_metric_evidence=None,
):
    target_quad = getattr(text_item, "target_quad_model", None)
    positioned_character = bool(getattr(text_item, "positioned_character", False))
    if (
        not positioned_character
        and (
            text_is_zero_ink(getattr(text_item, "text", ""))
            or not _quad_requires_full_affine(target_quad)
        )
    ):
        obj["pdf_full_affine_applied"] = False
        obj["pdf_metric_affine_applied"] = False
        return None
    try:
        from mathutils import Matrix
    except ImportError as exc:  # CPython fake-host tests never exercise this path.
        raise RuntimeError("Blender mathutils unavailable for required text affine") from exc
    metric_evidence: Dict[str, Any] = {}
    if positioned_character:
        metric_evidence = (
            dict(positioned_metric_evidence)
            if positioned_metric_evidence is not None
            else _positioned_font_axis_metrics(obj, text_item)
        )
        matrix_values = _metric_character_matrix_values(
            local_advance=metric_evidence.get(
                "local_matrix_horizontal_extent",
                metric_evidence["local_advance"],
            ),
            local_line_height=metric_evidence["local_line_height"],
            local_baseline_y=metric_evidence["local_baseline_y"],
            target_origin=getattr(text_item, "insertion", None),
            target_quad=target_quad,
            z=float(z_offset_m),
            allow_zero_advance=bool(metric_evidence.get("zero_ink_identity")),
        )
        expected_ink_bounds = _metric_expected_world_ink_bounds(
            metric_evidence, matrix_values
        )
        metric_evidence.pop("source_ink_bounds_design_units", None)
        metric_evidence.pop("source_ink_contours_design_units", None)
        if expected_ink_bounds is not None:
            metric_evidence["expected_world_ink_bounds_m"] = list(expected_ink_bounds)
    else:
        try:
            corners = list(obj.bound_box)
            xs = [float(corner[0]) for corner in corners]
            ys = [float(corner[1]) for corner in corners]
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("evaluated local text bounds unavailable for affine placement") from exc
        matrix_values = _affine_matrix_values(
            local_bounds=(min(xs), min(ys), max(xs), max(ys)),
            target_quad=target_quad,
            z=float(z_offset_m),
        )

    carrier = None
    try:
        zero_advance_logical_proof = bool(
            positioned_character
            and metric_evidence.get("zero_ink_identity") is True
            and float(metric_evidence.get("local_advance", math.inf)) <= 1e-12
        )
        if zero_advance_logical_proof:
            # There is no physical glyph to carry. Store the finite singular
            # matrix directly so a zero-advance source remains truthful.
            obj.matrix_world = Matrix(matrix_values)
        elif _matrix_requires_affine_carrier(matrix_values):
            parent_values, child_values = _factor_affine_matrix_values(matrix_values)
            target_collection = collection
            if target_collection is None:
                target_collection = next(iter(getattr(obj, "users_collection", ()) or ()), None)
            if target_collection is None:
                raise RuntimeError("affine carrier target collection is unavailable")
            carrier = bpy.data.objects.new(f"{obj.name}_AffineCarrier", None)
            target_collection.objects.link(carrier)
            carrier.matrix_world = Matrix(parent_values)
            obj.parent = carrier
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = Matrix(child_values)
            carrier["pdf_affine_carrier_for"] = str(obj.name)
            carrier["pdf_source_item_id"] = str(obj.get("pdf_source_item_id", "") or "")
            obj["pdf_affine_carrier"] = str(carrier.name)
            obj["pdf_affine_carrier_owned"] = True
        else:
            obj.matrix_world = Matrix(matrix_values)
        obj["pdf_full_affine_applied"] = True
        obj["pdf_metric_affine_applied"] = positioned_character
        obj["pdf_target_quad_model"] = [
            float(value) for point in target_quad for value in point
        ]
        obj["pdf_affine_matrix"] = [
            float(value) for row in matrix_values for value in row
        ]
        for key, value in metric_evidence.items():
            obj[f"pdf_metric_{key}"] = value
        if positioned_character:
            ul, ur, _lr, ll = tuple(
                (float(point[0]) * MM_TO_M, float(point[1]) * MM_TO_M)
                for point in target_quad
            )
            origin = getattr(text_item, "insertion", (0.0, 0.0))
            obj["pdf_metric_target_origin_m"] = [
                float(origin[0]) * MM_TO_M,
                float(origin[1]) * MM_TO_M,
            ]
            obj["pdf_metric_target_horizontal_axis_m"] = [
                ur[0] - ul[0],
                ur[1] - ul[1],
            ]
            obj["pdf_metric_target_vertical_axis_m"] = [
                ul[0] - ll[0],
                ul[1] - ll[1],
            ]
        return carrier
    except Exception as exc:
        if carrier is not None:
            try:
                bpy.data.objects.remove(carrier, do_unlink=True)
            except Exception as cleanup_exc:
                raise _OwnedConstructionError(
                    "affine carrier construction and rollback failed: "
                    f"creation={type(exc).__name__}:{exc}; "
                    f"cleanup={type(cleanup_exc).__name__}:{cleanup_exc}",
                    owned_objects=(carrier,),
                ) from exc
        raise


def _text_span_dict(text_item: NormalizedText) -> Dict[str, Any]:
    source_bbox = getattr(text_item, "source_bbox_pdf", None)
    target_bbox = getattr(text_item, "bbox", None)
    insertion = getattr(text_item, "insertion", (0.0, 0.0)) or (0.0, 0.0)
    return {
        "text": str(getattr(text_item, "text", "") or ""),
        "bbox": list(source_bbox) if source_bbox else None,
        "target_bbox_model": list(target_bbox) if target_bbox else None,
        "origin": [float(insertion[0]), float(insertion[1])],
        "size": float(getattr(text_item, "font_size", 0.0) or 0.0),
    }


def _provenance_entity_type(text_mode: str) -> str:
    return {
        "labels": "blender_label",
        "text": "blender_font_text",
        "3d_text": "blender_font_3d_text",
        "glyphs": "blender_curve_glyphs",
        "geometry": "blender_mesh_geometry",
        "raster": "blender_raster_plane",
    }[_normalize_text_mode(text_mode)]


def _record_text_mode_fallback(
    provenance_opts: Any,
    *,
    requested: str,
    delivered: str,
    reason: str,
) -> None:
    if provenance_opts is None or requested == delivered:
        return
    records = getattr(provenance_opts, "_text_mode_fallbacks", None)
    if not isinstance(records, list):
        records = []
        provenance_opts._text_mode_fallbacks = records  # noqa: B010
    records.append({
        "requested": requested,
        "delivered": delivered,
        "reason": reason,
        "count": 1,
    })


def _record_delivered_text_entity(provenance_opts: Any, delivered_mode: str) -> None:
    if provenance_opts is None:
        return
    counts = getattr(provenance_opts, "_text_delivered_entity_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        provenance_opts._text_delivered_entity_counts = counts  # noqa: B010
    bucket = {
        "labels": "native_label",
        "text": "native_text",
        "3d_text": "native_3d_text",
        "glyphs": "glyph_curve",
        "geometry": "geometry_mesh",
        "raster": "raster_patch",
    }[_normalize_text_mode(delivered_mode)]
    counts[bucket] = int(counts.get(bucket, 0) or 0) + 1


def _warn_unknown_text_mode_once(provenance_opts: Any, requested_mode: str) -> None:
    del provenance_opts
    raise ValueError(
        f"Unknown requested representation: {requested_mode!r}; no scene mutation was performed."
    )


def _record_text_provenance(
    provenance_opts: Any,
    *,
    page_number: int,
    text_item: NormalizedText,
    requested_text_mode: str,
    delivered_text_mode: str,
    parent_handle: str = "",
    zero_ink_delivery: bool = False,
) -> None:
    if provenance_opts is None:
        return
    try:
        from .pdfcadcore.source_provenance import record_text_span_provenance

        created_entity_type = (
            f"blender_zero_ink_{_normalize_text_mode(delivered_text_mode)}_identity"
            if zero_ink_delivery
            else _provenance_entity_type(delivered_text_mode)
        )
        record_text_span_provenance(
            provenance_opts,
            page=int(page_number),
            span=_text_span_dict(text_item),
            text=str(text_item.text or ""),
            created_entity_type=created_entity_type,
            parent_handle=str(parent_handle or ""),
            import_mode=str(getattr(provenance_opts, "import_mode", "") or ""),
            text_mode=str(requested_text_mode or ""),
            span_index=int(getattr(text_item, "id", 0) or 0),
        )
    except (ImportError, TypeError, ValueError):
        pass


def _font_asset_evidence(text_item: NormalizedText) -> Dict[str, Any]:
    asset = getattr(text_item, "font_asset", None)
    if asset is not None:
        return {
            "asset_id": str(getattr(asset, "asset_id", "") or ""),
            "source_sha256": str(getattr(asset, "source_sha256", "") or ""),
            "usable_sha256": str(getattr(asset, "usable_sha256", "") or ""),
            "source_xref": getattr(asset, "source_xref", None),
            "font_name": str(getattr(asset, "base_font_name", "") or ""),
            "font_asset_page_number": getattr(asset, "page_number", None),
            "font_asset_span_font_name": str(
                getattr(asset, "span_font_name", "") or ""
            ),
            "font_units_per_em": int(getattr(asset, "units_per_em", 0) or 0),
            "font_ascender": int(getattr(asset, "ascender", 0) or 0),
            "font_descender": int(getattr(asset, "descender", 0) or 0),
        }
    failure = getattr(text_item, "font_failure", None)
    return {
        "reason": str(getattr(failure, "reason", "no_exact_font_asset") or "no_exact_font_asset"),
        "source_xref": getattr(failure, "source_xref", None),
        "font_failure_page_number": getattr(failure, "page_number", None),
        "font_failure_span_font_name": str(
            getattr(failure, "span_font_name", "") or ""
        ),
        "error_type": str(getattr(failure, "error_type", "") or ""),
        "detail": str(getattr(failure, "detail", "") or ""),
        "proof_category": str(getattr(failure, "proof_category", "") or ""),
        "font_name": str(getattr(text_item, "font_name", "") or ""),
    }


def _load_exact_font(text_item: NormalizedText, item_id: str, page_number: int):
    asset = getattr(text_item, "font_asset", None)
    if asset is None:
        return None, AttemptOutcome.impossible(
            "exact_source_font_unavailable_for_item",
            evidence={
                **_proof_identity(item_id, page_number, int(text_item.id)),
                **_font_asset_evidence(text_item),
            },
        )
    asset_page = getattr(asset, "page_number", None)
    asset_span_font = str(getattr(asset, "span_font_name", "") or "")
    expected_span_font = str(getattr(text_item, "font_name", "") or "")
    try:
        asset_page_matches = int(asset_page) == int(page_number)
    except (TypeError, ValueError):
        asset_page_matches = False
    if not asset_page_matches or asset_span_font != expected_span_font:
        return None, AttemptOutcome.failed(
            "exact_font_asset_identity_mismatch",
            evidence={
                **_proof_identity(item_id, page_number, int(text_item.id)),
                **_font_asset_evidence(text_item),
                "expected_span_font_name": expected_span_font,
            },
        )
    font_bytes = bytes(getattr(asset, "usable_bytes", b"") or b"")
    expected_sha = str(getattr(asset, "usable_sha256", "") or "")
    actual_sha = sha256(font_bytes).hexdigest() if font_bytes else ""
    if not font_bytes or not expected_sha or actual_sha != expected_sha:
        return None, AttemptOutcome.failed(
            "exact_font_asset_hash_verification_failed",
            evidence={
                "item_id": item_id,
                "expected_sha256": expected_sha,
                "actual_sha256": actual_sha,
            },
        )
    cached = _FONT_CACHE.get(expected_sha)
    if cached is not None:
        try:
            verify_packed_sha256(cached, expected_sha)
        except (PackedAssetError, ReferenceError, AttributeError):
            _FONT_CACHE.pop(expected_sha, None)
        else:
            return cached, None
    extension = str(getattr(asset, "usable_format", "") or "").lower().lstrip(".")
    if extension not in {"cff", "otf", "ttf"}:
        return None, AttemptOutcome.failed(
            "exact_font_asset_format_unverified",
            evidence={"item_id": item_id, "format": extension},
        )
    cache_dir = Path(tempfile.gettempdir()) / "bc_bl_pdf_exact_fonts"
    path = cache_dir / f"{expected_sha}.{extension}"
    font = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_matches = (
            path.exists()
            and sha256(path.read_bytes()).hexdigest() == expected_sha
        )
        if not cache_matches:
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(cache_dir),
                    prefix=f".{expected_sha}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    temp_file.write(font_bytes)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, path)
            finally:
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        # A prior failed import may have left Blender's path cache pointing at
        # different packed bytes even after the on-disk cache was repaired.
        # Load a fresh attempt-owned datablock and verify its packed payload.
        font = bpy.data.fonts.load(str(path), check_existing=False)
        pack_and_verify_bytes(font, font_bytes)
    except Exception as exc:
        cleanup_error = ""
        if font is not None:
            try:
                bpy.data.fonts.remove(font)
            except Exception as cleanup_exc:
                cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        owned_font = (font,) if font is not None and cleanup_error else ()
        return None, AttemptOutcome.failed(
            "exact_font_host_load_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "path": str(path),
                "exception_type": type(exc).__name__,
                "detail": str(exc),
                "cleanup_error": cleanup_error,
                **_font_asset_evidence(text_item),
            },
            owned_artifacts=(_artifact(None, font),) if owned_font else (),
            owned_datablocks=owned_font,
        )
    _FONT_CACHE[expected_sha] = font
    return font, None


def _artifact(obj=None, data=None) -> Dict[str, str]:
    try:
        object_id = str(getattr(obj, "name", "") or "")
    except ReferenceError:
        object_id = "<removed>"
    try:
        datablock_id = str(getattr(data, "name", "") or "")
    except ReferenceError:
        datablock_id = "<removed>"
    return {
        "object_id": object_id,
        "datablock_id": datablock_id,
        "ownership": "created_by_this_item_attempt",
    }


def _raster_artifact(obj) -> Dict[str, Any]:
    artifact: Dict[str, Any] = _artifact(obj, getattr(obj, "data", None))
    for output_key, property_key in (
        ("file_path", "pdf_image_path"),
        ("material_id", "pdf_image_material"),
        ("image_id", "pdf_image_datablock"),
    ):
        try:
            value = str(obj.get(property_key, "") or "")
        except (AttributeError, ReferenceError, TypeError):
            value = ""
        if value:
            artifact[output_key] = value
    return artifact


def _valid_owned_ref(value) -> bool:
    if value is None:
        return False
    try:
        getattr(value, "name", None)
        return True
    except ReferenceError:
        return False


def _owned_objects_for_text_entity(obj) -> tuple:
    objects = [obj] if _valid_owned_ref(obj) else []
    try:
        carrier_owned = bool(obj.get("pdf_affine_carrier_owned", False))
        carrier = getattr(obj, "parent", None) if carrier_owned else None
    except (AttributeError, ReferenceError, TypeError):
        carrier = None
    if _valid_owned_ref(carrier) and all(carrier is not value for value in objects):
        objects.append(carrier)
    return tuple(objects)


def _owned_artifacts_for_text_entity(obj, data) -> tuple:
    objects = _owned_objects_for_text_entity(obj)
    artifacts = [_artifact(obj, data)] if obj is not None or data is not None else []
    artifacts.extend(_artifact(value, None) for value in objects if value is not obj)
    return tuple(artifacts)


def _unique_owned_objects(*objects) -> tuple:
    result = []
    for obj in objects:
        for value in _owned_objects_for_text_entity(obj):
            if all(value is not existing for existing in result):
                result.append(value)
    return tuple(result)


def _datablock_kind(data) -> str:
    try:
        data_type = str(getattr(data, "type", "") or "").upper()
    except ReferenceError:
        return ""
    if data_type in {"FONT", "CURVE", "SURFACE"}:
        return "CURVE"
    if data_type == "MESH":
        return "MESH"
    try:
        identifier = str(getattr(getattr(data, "bl_rna", None), "identifier", "") or "").upper()
    except ReferenceError:
        return ""
    if identifier in {"CURVE", "TEXTCURVE", "SURFACECURVE"}:
        return "CURVE"
    if identifier == "MESH":
        return "MESH"
    if identifier == "MATERIAL":
        return "MATERIAL"
    class_name = type(data).__name__.upper()
    if (
        "VECTORFONT" in class_name
        or (
            hasattr(data, "filepath")
            and hasattr(data, "packed_file")
            and not hasattr(data, "materials")
        )
    ):
        return "FONT"
    if "CURVE" in class_name or "FONT" in class_name:
        return "CURVE"
    if "MESH" in class_name:
        return "MESH"
    if "MATERIAL" in class_name:
        return "MATERIAL"
    return ""


def _copy_text_material_metadata(source, target) -> None:
    for key in (
        "pdf_text_material",
        "pdf_text_material_owned",
        "pdf_text_expected_rgba",
    ):
        try:
            target[key] = source.get(key)
        except (AttributeError, ReferenceError, TypeError):
            pass


def _verify_text_material(obj) -> tuple[list[str], Dict[str, Any]]:
    failures: list[str] = []
    evidence: Dict[str, Any] = {}
    try:
        material_name = str(obj.get("pdf_text_material", "") or "")
        material_owned = bool(obj.get("pdf_text_material_owned", False))
        expected = tuple(float(value) for value in obj.get("pdf_text_expected_rgba", ()))
        assigned = list(getattr(obj.data, "materials", []) or [])
    except (AttributeError, ReferenceError, TypeError, ValueError):
        material_name = ""
        material_owned = False
        expected = ()
        assigned = []
    material = next(
        (
            candidate
            for candidate in assigned
            if str(getattr(candidate, "name", "") or "") == material_name
        ),
        None,
    )
    try:
        actual = tuple(float(value) for value in material.diffuse_color)
    except (AttributeError, ReferenceError, TypeError, ValueError):
        actual = ()
    try:
        nodes = list(material.node_tree.nodes)
        links = list(material.node_tree.links)
    except (AttributeError, ReferenceError, TypeError):
        nodes = []
        links = []
    shaders = [
        node for node in nodes if str(getattr(node, "type", "")) == "BSDF_PRINCIPLED"
    ]
    outputs = [
        node for node in nodes if str(getattr(node, "type", "")) == "OUTPUT_MATERIAL"
    ]
    try:
        node_rgba = tuple(
            float(value) for value in shaders[0].inputs["Base Color"].default_value
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        node_rgba = ()
    shader_linked = any(
        getattr(link, "from_node", None) in shaders
        and getattr(link, "to_node", None) in outputs
        for link in links
    )
    evidence.update(
        text_material=material_name,
        text_material_owned=material_owned,
        expected_text_rgba=list(expected),
        actual_text_rgba=list(actual),
        actual_text_node_rgba=list(node_rgba),
        text_shader_to_output_linked=shader_linked,
    )
    if material is None or not material_owned:
        failures.append("text_material_assignment_unverified")
    if (
        len(expected) != 4
        or len(actual) != 4
        or len(node_rgba) != 4
        or any(not math.isfinite(value) for value in (*expected, *actual, *node_rgba))
        or any(
            abs(left - right) > 1e-6
            for left, right in zip(actual, expected)  # noqa: B905
        )
        or any(
            abs(left - right) > 1e-6
            for left, right in zip(node_rgba, expected)  # noqa: B905
        )
    ):
        failures.append("text_material_color_mismatch")
    if (
        material is None
        or not bool(getattr(material, "use_nodes", False))
        or not shader_linked
    ):
        failures.append("text_material_node_mode_unverified")
    return failures, evidence


def _set_object_metadata(
    obj,
    *,
    item_id: str,
    requested: str,
    delivered: str,
    text_item: NormalizedText,
) -> None:
    obj["pdf_text_mode"] = delivered
    obj["pdf_text_requested_mode"] = requested
    obj["pdf_source_item_id"] = item_id
    obj["pdf_source_span_id"] = int(getattr(text_item, "id", 0) or 0)
    object_size_mm = (
        float(getattr(text_item, "font_size", 0.0) or 0.0)
        if bool(getattr(text_item, "positioned_character", False))
        else float(_calibrated_text_size_mm(text_item))
    )
    obj["pdf_text_size_mm"] = object_size_mm
    asset = getattr(text_item, "font_asset", None)
    if asset is not None:
        obj["pdf_exact_font_sha256"] = str(getattr(asset, "usable_sha256", "") or "")
        obj["pdf_exact_font_packed"] = True


def _create_font_candidate(
    text_item: NormalizedText,
    collection,
    *,
    page_number: int,
    requested: str,
    delivered: str,
    item_id: str,
    visual_style: str,
    z_offset_m: float,
    entity_suffix: str = "",
    defer_host_update: bool = False,
    baseline_alignment: Optional[str] = None,
):
    font, font_outcome = _load_exact_font(text_item, item_id, page_number)
    if font_outcome is not None:
        return None, None, font_outcome
    data = None
    obj = None
    material = None
    affine_carrier = None
    positioned_character = bool(getattr(text_item, "positioned_character", False))
    try:
        name = f"P{page_number}_text_{delivered}_{int(text_item.id)}{entity_suffix}"
        data = bpy.data.curves.new(name=name, type="FONT")
        data.body = str(text_item.text)
        requested_size_mm = (
            float(getattr(text_item, "font_size", 0.0) or 0.0)
            if positioned_character
            else float(_calibrated_text_size_mm(text_item))
        )
        data.size = max(requested_size_mm * MM_TO_M, 0.0001)
        data.font = font
        data.align_x = "LEFT"
        sealed_baseline_alignment = baseline_alignment
        baseline_compensation_m = 0.0
        if positioned_character:
            if sealed_baseline_alignment not in {"BOTTOM_BASELINE", "BOTTOM"}:
                raise RuntimeError(
                    "positioned FONT baseline alignment was not sealed"
                )
            try:
                data.align_y = sealed_baseline_alignment
            except Exception as baseline_exc:
                raise RuntimeError(
                    "sealed positioned FONT baseline alignment is unavailable"
                ) from baseline_exc
            if str(getattr(data, "align_y", "") or "") != sealed_baseline_alignment:
                raise RuntimeError(
                    "positioned FONT baseline alignment changed after sealing"
                )
        else:
            sealed_baseline_alignment = "BOTTOM_BASELINE"
            try:
                data.align_y = "BOTTOM_BASELINE"
            except Exception as baseline_exc:
                data.align_y = "BOTTOM"
                sealed_baseline_alignment = "BOTTOM"
                if str(getattr(data, "align_y", "") or "") != "BOTTOM":
                    raise RuntimeError(
                        "fallback FONT baseline alignment is unavailable"
                    ) from baseline_exc
        if sealed_baseline_alignment == "BOTTOM":
            baseline_compensation_m = (
                float(getattr(text_item, "baseline_descent", 0.0) or 0.0) * MM_TO_M
            )
            if not math.isfinite(baseline_compensation_m) or baseline_compensation_m <= 0.0:
                raise RuntimeError(
                    "BOTTOM_BASELINE unavailable and source baseline descent is not "
                    "available for measured compensation"
                )
        data.extrude = _text_extrusion_depth(data.size) if delivered == "3d_text" else 0.0
        data.resolution_u = max(int(getattr(data, "resolution_u", 12) or 12), 24)
        obj = bpy.data.objects.new(name, data)
        _set_object_metadata(
            obj,
            item_id=item_id,
            requested=requested,
            delivered=delivered,
            text_item=text_item,
        )
        obj["pdf_baseline_alignment"] = sealed_baseline_alignment
        obj["pdf_baseline_compensation_m"] = baseline_compensation_m
        x, y = text_item.insertion
        angle_rad = math.radians(float(getattr(text_item, "rotation", 0.0) or 0.0))
        obj.location = (
            float(x) * MM_TO_M + math.sin(angle_rad) * baseline_compensation_m,
            float(y) * MM_TO_M - math.cos(angle_rad) * baseline_compensation_m,
            float(z_offset_m),
        )
        material = _get_or_create_text_material(visual_style, source_color=text_item.color)
        expected_rgb = _styled_text_color(visual_style, source_color=text_item.color)
        expected_rgba = (*tuple(float(value) for value in expected_rgb), 1.0)
        obj["pdf_text_material"] = str(getattr(material, "name", "") or "")
        obj["pdf_text_material_owned"] = True
        obj["pdf_text_expected_rgba"] = list(expected_rgba)
        if len(data.materials) == 0:
            data.materials.append(material)
        else:
            data.materials[0] = material
        obj.color = material.diffuse_color
        collection.objects.link(obj)
        if not positioned_character:
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            _fit_text_to_bbox(obj, text_item)
        obj.rotation_euler = (
            0.0,
            0.0,
            angle_rad,
        )
        if not positioned_character:
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
        affine_carrier = _apply_target_quad_affine(
            obj,
            text_item,
            z_offset_m,
            collection=collection,
        )
        if not defer_host_update:
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
        return obj, data, None
    except Exception as exc:
        construction_objects = tuple(getattr(exc, "owned_objects", ()) or ())
        owned_objects_list = []
        for value in (obj, affine_carrier, *construction_objects):
            if value is not None and all(value is not existing for existing in owned_objects_list):
                owned_objects_list.append(value)
        owned_objects = tuple(owned_objects_list)
        construction_data = tuple(getattr(exc, "owned_datablocks", ()) or ())
        owned_data = []
        for value in (*tuple(value for value in (data, material) if value is not None), *construction_data):
            if value is not None and all(value is not existing for existing in owned_data):
                owned_data.append(value)
        owned_data = tuple(owned_data)
        artifacts = tuple(
            [_artifact(obj, data)] if obj is not None or data is not None else []
        ) + tuple(
            _artifact(value, None)
            for value in (affine_carrier, *construction_objects)
            if value is not None and value is not obj
        ) + tuple(_artifact(None, value) for value in construction_data)
        return None, None, AttemptOutcome.failed(
            "font_object_creation_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=artifacts,
            owned_objects=owned_objects,
            owned_datablocks=owned_data,
        )


def _evaluated_world_ink_bounds(
    obj,
    evaluated,
    matrix,
    depsgraph,
    vector_factory,
) -> tuple[tuple[float, float, float, float] | None, Dict[str, Any]]:
    """Measure physical ink without certifying a CURVE's Bezier control hull."""
    try:
        object_type = str(getattr(obj, "type", "") or "")
        exact_contour = (
            object_type in {"CURVE", "MESH"}
            and str(obj.get("pdf_exact_contour_source", "") or "")
            == "embedded_font_glyph_outline"
        )
    except (AttributeError, ReferenceError, TypeError):
        object_type = ""
        exact_contour = False

    evidence: Dict[str, Any] = {}
    if exact_contour and object_type == "MESH":
        evidence["evaluated_ink_bounds_source"] = "evaluated_mesh_vertices"
        try:
            vertices = tuple(getattr(evaluated.data, "vertices", ()) or ())
            if not vertices:
                raise ValueError("evaluated exact-contour mesh has no vertices")
            world_points = tuple(
                matrix
                @ vector_factory(
                    tuple(float(value) for value in tuple(vertex.co)[:3])
                )
                for vertex in vertices
            )
            xs = tuple(float(point[0]) for point in world_points)
            ys = tuple(float(point[1]) for point in world_points)
            bounds = (min(xs), min(ys), max(xs), max(ys))
            if not all(math.isfinite(value) for value in bounds):
                raise ValueError("evaluated exact-contour mesh bounds are non-finite")
            return bounds, evidence
        except (AttributeError, IndexError, TypeError, ValueError):
            return None, evidence

    if exact_contour and object_type == "CURVE":
        render_source = f"evaluated_{object_type.lower()}_render_mesh"
        cleared_key = f"{render_source}_cleared"
        error_key = f"{render_source}_error"
        cleanup_error_key = f"{render_source}_cleanup_error"
        evidence["evaluated_ink_bounds_source"] = render_source
        evidence[cleared_key] = False
        to_mesh = getattr(evaluated, "to_mesh", None)
        clear_mesh = getattr(evaluated, "to_mesh_clear", None)
        if not callable(to_mesh) or not callable(clear_mesh):
            evidence[error_key] = (
                "temporary render-mesh capability unavailable"
            )
            return None, evidence

        bounds = None
        measurement_error = None
        try:
            render_mesh = to_mesh(
                preserve_all_data_layers=False,
                depsgraph=depsgraph,
            )
            vertices = tuple(getattr(render_mesh, "vertices", ()) or ())
            if not vertices:
                raise ValueError("evaluated render mesh has no vertices")
            world_points = tuple(
                matrix
                @ vector_factory(
                    tuple(float(value) for value in tuple(vertex.co)[:3])
                )
                for vertex in vertices
            )
            xs = tuple(float(point[0]) for point in world_points)
            ys = tuple(float(point[1]) for point in world_points)
            bounds = (min(xs), min(ys), max(xs), max(ys))
            if not all(math.isfinite(value) for value in bounds):
                raise ValueError("evaluated render-mesh bounds are non-finite")
        except Exception as exc:
            measurement_error = exc
            bounds = None
        finally:
            try:
                clear_mesh()
                evidence[cleared_key] = True
            except Exception as exc:
                bounds = None
                evidence[cleanup_error_key] = {
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                }
        if measurement_error is not None:
            evidence[error_key] = {
                "exception_type": type(measurement_error).__name__,
                "detail": str(measurement_error),
            }
        return bounds, evidence

    evidence["evaluated_ink_bounds_source"] = "evaluated_object_bound_box"
    try:
        evaluated_corners = tuple(evaluated.bound_box)
        if not evaluated_corners:
            raise ValueError("evaluated glyph has no bound-box corners")
        evaluated_world_points = tuple(
            matrix
            @ vector_factory(tuple(float(value) for value in corner[:3]))
            for corner in evaluated_corners
        )
        xs = tuple(float(point[0]) for point in evaluated_world_points)
        ys = tuple(float(point[1]) for point in evaluated_world_points)
        bounds = (min(xs), min(ys), max(xs), max(ys))
        if not all(math.isfinite(value) for value in bounds):
            raise ValueError("evaluated glyph bounds are non-finite")
        return bounds, evidence
    except (AttributeError, IndexError, TypeError, ValueError):
        return None, evidence


def _verify_metric_character_transform(obj, text_item) -> tuple[list[str], Dict[str, Any]]:
    failures: list[str] = []
    evidence: Dict[str, Any] = {
        "full_affine_applied": True,
        "metric_affine_applied": True,
    }
    try:
        from mathutils import Vector

        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        local_advance = float(obj.get("pdf_metric_local_advance"))
        local_line_height = float(obj.get("pdf_metric_local_line_height"))
        local_baseline_y = float(obj.get("pdf_metric_local_baseline_y", 0.0) or 0.0)
        actual_baseline_vec = matrix @ Vector((0.0, local_baseline_y, 0.0))
        actual_advance_vec = matrix @ Vector((local_advance, local_baseline_y, 0.0))
        actual_line_vec = matrix @ Vector(
            (0.0, local_baseline_y + local_line_height, 0.0)
        )
        actual_baseline = (float(actual_baseline_vec[0]), float(actual_baseline_vec[1]))
        actual_advance = (float(actual_advance_vec[0]), float(actual_advance_vec[1]))
        actual_line = (float(actual_line_vec[0]), float(actual_line_vec[1]))

        target_quad = tuple(
            (float(point[0]) * MM_TO_M, float(point[1]) * MM_TO_M)
            for point in text_item.target_quad_model
        )
        target_origin = (
            float(text_item.insertion[0]) * MM_TO_M,
            float(text_item.insertion[1]) * MM_TO_M,
        )
        target_horizontal = (
            target_quad[1][0] - target_quad[0][0],
            target_quad[1][1] - target_quad[0][1],
        )
        target_vertical = (
            target_quad[0][0] - target_quad[3][0],
            target_quad[0][1] - target_quad[3][1],
        )
        expected_advance = (
            target_origin
            if local_advance <= 1e-12
            else (
                target_origin[0] + target_horizontal[0],
                target_origin[1] + target_horizontal[1],
            )
        )
        expected_line = (
            target_origin[0] + target_vertical[0],
            target_origin[1] + target_vertical[1],
        )
        evaluated_matrix = [float(value) for row in matrix for value in row]
        intended_matrix = [float(value) for value in obj.get("pdf_affine_matrix", [])]
        zero_ink_identity = bool(obj.get("pdf_metric_zero_ink_identity", False))
        zero_advance_logical_proof = bool(
            obj.get("pdf_metric_zero_advance_logical_proof", False)
        )
        expected_world_ink_bounds = None
        actual_world_ink_bounds = None
        ink_bounds_verified = zero_ink_identity
        if not zero_ink_identity:
            try:
                expected_world_ink_bounds = tuple(
                    float(value)
                    for value in obj.get("pdf_metric_expected_world_ink_bounds_m", [])
                )
                if (
                    len(expected_world_ink_bounds) != 4
                    or not all(
                        math.isfinite(value) for value in expected_world_ink_bounds
                    )
                    or expected_world_ink_bounds[0] > expected_world_ink_bounds[2]
                    or expected_world_ink_bounds[1] > expected_world_ink_bounds[3]
                ):
                    raise ValueError("invalid exact source glyph world bounds")
            except (AttributeError, TypeError, ValueError):
                expected_world_ink_bounds = None
                failures.append("exact_source_glyph_ink_bounds_unavailable")
            actual_world_ink_bounds, ink_bounds_evidence = _evaluated_world_ink_bounds(
                obj,
                evaluated,
                matrix,
                depsgraph,
                Vector,
            )
            evidence.update(ink_bounds_evidence)
            if actual_world_ink_bounds is None:
                failures.append("evaluated_glyph_ink_bounds_unverifiable")
            if (
                expected_world_ink_bounds is not None
                and actual_world_ink_bounds is not None
            ):
                # Blender tessellates font curves. 0.05 mm permits that finite
                # host approximation while remaining far below visible overscale.
                tolerance = _METRIC_INK_BOUNDS_TOLERANCE_M
                outside = (
                    actual_world_ink_bounds[0]
                    < expected_world_ink_bounds[0] - tolerance
                    or actual_world_ink_bounds[1]
                    < expected_world_ink_bounds[1] - tolerance
                    or actual_world_ink_bounds[2]
                    > expected_world_ink_bounds[2] + tolerance
                    or actual_world_ink_bounds[3]
                    > expected_world_ink_bounds[3] + tolerance
                )
                mismatch = any(
                    abs(actual - expected) > tolerance
                    for actual, expected in zip(  # noqa: B905
                        actual_world_ink_bounds, expected_world_ink_bounds
                    )
                )
                if outside:
                    failures.append(
                        "evaluated_ink_bounds_outside_exact_source_glyph_bounds"
                    )
                elif mismatch:
                    failures.append("evaluated_ink_bounds_mismatch_exact_source_glyph_bounds")
                else:
                    ink_bounds_verified = True
        finite_values = (
            *actual_baseline,
            *actual_advance,
            *actual_line,
            *target_origin,
            *expected_advance,
            *expected_line,
            *evaluated_matrix,
            local_advance,
            local_line_height,
            local_baseline_y,
        )
        evidence.update(
            expected_location_m=list(target_origin),
            actual_location_m=[float(matrix[0][3]), float(matrix[1][3])],
            actual_baseline_anchor_m=list(actual_baseline),
            expected_advance_endpoint_m=list(expected_advance),
            actual_advance_endpoint_m=list(actual_advance),
            expected_line_axis_endpoint_m=list(expected_line),
            actual_line_axis_endpoint_m=list(actual_line),
            target_horizontal_axis_m=list(target_horizontal),
            target_vertical_axis_m=list(target_vertical),
            local_advance_m=local_advance,
            local_line_height_m=local_line_height,
            local_baseline_y_m=local_baseline_y,
            evaluated_affine_matrix=evaluated_matrix,
            intended_affine_matrix=intended_matrix,
            zero_ink_identity=zero_ink_identity,
            zero_advance_logical_proof=zero_advance_logical_proof,
            expected_world_ink_bounds_m=(
                list(expected_world_ink_bounds)
                if expected_world_ink_bounds is not None
                else None
            ),
            actual_world_ink_bounds_m=(
                list(actual_world_ink_bounds)
                if actual_world_ink_bounds is not None
                else None
            ),
            ink_bounds_tolerance_m=_METRIC_INK_BOUNDS_TOLERANCE_M,
            evaluated_ink_bounds_verified=ink_bounds_verified,
            affine_carrier=str(obj.get("pdf_affine_carrier", "") or ""),
            baseline_alignment=str(obj.get("pdf_baseline_alignment", "") or ""),
            evaluated_bounds_verified=True,
            target_dimensions_m=[math.hypot(*target_horizontal), math.hypot(*target_vertical)],
            actual_dimensions_m=[abs(float(obj.dimensions[0])), abs(float(obj.dimensions[1]))],
            evaluated_dimensions_m=[
                abs(float(evaluated.dimensions[0])),
                abs(float(evaluated.dimensions[1])),
            ],
        )
        if not all(math.isfinite(value) for value in finite_values):
            failures.append("nonfinite_metric_character_transform")
        if zero_advance_logical_proof != (
            zero_ink_identity and local_advance <= 1e-12
        ):
            failures.append("zero_advance_logical_proof_mismatch")
        tolerance = 1e-7
        for actual, expected, reason in (
            (actual_baseline, target_origin, "evaluated_baseline_anchor_mismatch"),
            (actual_advance, expected_advance, "evaluated_font_advance_axis_mismatch"),
            (actual_line, expected_line, "evaluated_font_line_axis_mismatch"),
        ):
            if any(
                abs(left - right) > tolerance
                for left, right in zip(actual, expected)  # noqa: B905
            ):
                failures.append(reason)
        if len(evaluated_matrix) != 16 or len(intended_matrix) != 16 or any(
            abs(actual - expected) > 1e-6
            for actual, expected in zip(  # noqa: B905
                evaluated_matrix, intended_matrix
            )
        ):
            failures.append("evaluated_affine_matrix_mismatch")
        carrier_name = str(obj.get("pdf_affine_carrier", "") or "")
        if carrier_name and (
            getattr(obj, "parent", None) is None
            or str(getattr(obj.parent, "name", "") or "") != carrier_name
        ):
            failures.append("affine_carrier_identity_mismatch")
    except (AttributeError, ImportError, IndexError, TypeError, ValueError):
        failures.append("evaluated_metric_character_transform_unverifiable")
    return failures, evidence


def _verify_full_affine_transform(obj, text_item) -> tuple[list[str], Dict[str, Any]]:
    failures: list[str] = []
    evidence: Dict[str, Any] = {"full_affine_applied": True}
    try:
        from mathutils import Vector

        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        corners = list(evaluated.bound_box)
        xs = [float(corner[0]) for corner in corners]
        ys = [float(corner[1]) for corner in corners]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        local_quad = (
            (xmin, ymax, 0.0),
            (xmax, ymax, 0.0),
            (xmax, ymin, 0.0),
            (xmin, ymin, 0.0),
        )
        actual_quad = tuple(
            tuple(float(value) for value in (evaluated.matrix_world @ Vector(point))[:2])
            for point in local_quad
        )
        expected_quad = tuple(
            (float(point[0]) * MM_TO_M, float(point[1]) * MM_TO_M)
            for point in text_item.target_quad_model
        )
        evidence["expected_world_quad_m"] = [list(point) for point in expected_quad]
        evidence["evaluated_world_quad_m"] = [list(point) for point in actual_quad]
        quad_tolerance = 1e-7
        if len(actual_quad) != len(expected_quad) or any(
            abs(actual - expected) > quad_tolerance
            for actual_point, expected_point in zip(  # noqa: B905
                actual_quad, expected_quad
            )
            for actual, expected in zip(  # noqa: B905
                actual_point, expected_point
            )
        ):
            failures.append("evaluated_affine_quad_mismatch")

        baseline_alignment = str(obj.get("pdf_baseline_alignment", "") or "")
        baseline_local_y = (
            float(getattr(text_item, "baseline_descent", 0.0) or 0.0) * MM_TO_M
            if baseline_alignment == "BOTTOM"
            else 0.0
        )
        actual_baseline_vec = evaluated.matrix_world @ Vector((0.0, baseline_local_y, 0.0))
        actual_baseline = (float(actual_baseline_vec[0]), float(actual_baseline_vec[1]))
        expected_baseline = (
            float(text_item.insertion[0]) * MM_TO_M,
            float(text_item.insertion[1]) * MM_TO_M,
        )
        evidence["expected_location_m"] = list(expected_baseline)
        evidence["actual_location_m"] = [float(obj.location[0]), float(obj.location[1])]
        evidence["actual_baseline_anchor_m"] = list(actual_baseline)
        evidence["baseline_alignment"] = baseline_alignment
        evidence["baseline_compensation_m"] = float(
            obj.get("pdf_baseline_compensation_m", 0.0) or 0.0
        )
        evidence["evaluated_bounds_verified"] = True
        # The extracted insertion and the evaluated exact-font origin must agree;
        # 0.05 mm allows host float/font tessellation noise, not visible drift.
        if any(
            abs(actual - expected) > 5e-5
            for actual, expected in zip(  # noqa: B905
                actual_baseline, expected_baseline
            )
        ):
            failures.append("evaluated_baseline_anchor_mismatch")
        target_width = math.dist(expected_quad[0], expected_quad[1])
        target_height = math.dist(expected_quad[0], expected_quad[3])
        actual_width = math.dist(actual_quad[0], actual_quad[1])
        actual_height = math.dist(actual_quad[0], actual_quad[3])
        evidence["target_dimensions_m"] = [target_width, target_height]
        evidence["actual_dimensions_m"] = [actual_width, actual_height]
        evidence["evaluated_dimensions_m"] = [actual_width, actual_height]
        expected_rotation = math.atan2(
            expected_quad[1][1] - expected_quad[0][1],
            expected_quad[1][0] - expected_quad[0][0],
        )
        actual_rotation = math.atan2(
            actual_quad[1][1] - actual_quad[0][1],
            actual_quad[1][0] - actual_quad[0][0],
        )
        evidence["expected_rotation_rad"] = expected_rotation
        evidence["actual_rotation_rad"] = actual_rotation
    except (AttributeError, ImportError, IndexError, TypeError, ValueError):
        failures.append("evaluated_affine_transform_unverifiable")
    return failures, evidence


def _verify_transform_and_dimensions(obj, text_item) -> tuple[list[str], Dict[str, Any]]:
    try:
        full_affine = bool(obj.get("pdf_full_affine_applied", False))
        metric_affine = bool(obj.get("pdf_metric_affine_applied", False))
    except (AttributeError, ReferenceError, TypeError):
        full_affine = False
        metric_affine = False
    if metric_affine:
        return _verify_metric_character_transform(obj, text_item)
    if full_affine:
        return _verify_full_affine_transform(obj, text_item)
    failures: list[str] = []
    evidence: Dict[str, Any] = {}
    expected_location = (
        float(text_item.insertion[0]) * MM_TO_M,
        float(text_item.insertion[1]) * MM_TO_M,
    )
    try:
        actual_location = (float(obj.location[0]), float(obj.location[1]))
        evidence["expected_location_m"] = list(expected_location)
        evidence["actual_location_m"] = list(actual_location)
        expected_rotation = math.radians(float(text_item.rotation or 0.0))
        actual_rotation = float(obj.rotation_euler[2])
        evidence["expected_rotation_rad"] = expected_rotation
        evidence["actual_rotation_rad"] = actual_rotation
        if abs(actual_rotation - expected_rotation) > 1e-6:
            failures.append("rotation_mismatch")
        baseline_alignment = str(obj.get("pdf_baseline_alignment", "") or "")
        baseline_compensation_m = float(
            obj.get("pdf_baseline_compensation_m", 0.0) or 0.0
        )
        actual_baseline = (
            actual_location[0] - math.sin(actual_rotation) * baseline_compensation_m,
            actual_location[1] + math.cos(actual_rotation) * baseline_compensation_m,
        )
        evidence["baseline_alignment"] = baseline_alignment
        evidence["baseline_compensation_m"] = baseline_compensation_m
        evidence["actual_baseline_anchor_m"] = list(actual_baseline)
        # Blender stores object transforms as finite-precision host values.
        # 1e-7 m is 0.0001 mm: well below PDF/display resolution while avoiding
        # false failures caused solely by host float quantization.
        if abs(actual_baseline[0] - expected_location[0]) > 1e-7:
            failures.append("x_anchor_mismatch")
        if abs(actual_baseline[1] - expected_location[1]) > 1e-7:
            failures.append("y_anchor_mismatch")
        if baseline_alignment not in {"BOTTOM_BASELINE", "BOTTOM"}:
            failures.append("baseline_alignment_unverified")
        actual_scale_x = float(obj.scale[0])
        actual_scale_y = float(obj.scale[1])
        if actual_scale_x <= 0.0 or actual_scale_y <= 0.0:
            failures.append("nonpositive_text_scale")
        target_width = float(getattr(text_item, "advance_width", 0.0) or 0.0) * MM_TO_M
        target_height = float(getattr(text_item, "glyph_height", 0.0) or 0.0) * MM_TO_M
        actual_width = abs(float(obj.dimensions[0]))
        actual_height = abs(float(obj.dimensions[1]))
        try:
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            evaluated_width = abs(float(evaluated.dimensions[0]))
            evaluated_height = abs(float(evaluated.dimensions[1]))
        except (AttributeError, IndexError, TypeError, ValueError):
            evaluated_width = actual_width
            evaluated_height = actual_height
        finite_values = (
            *expected_location,
            *actual_location,
            expected_rotation,
            actual_rotation,
            actual_scale_x,
            actual_scale_y,
            target_width,
            target_height,
            actual_width,
            actual_height,
            evaluated_width,
            evaluated_height,
            baseline_compensation_m,
            *actual_baseline,
        )
        if not all(math.isfinite(value) for value in finite_values):
            failures.append("nonfinite_text_transform_or_dimensions")
        evidence["target_dimensions_m"] = [target_width, target_height]
        evidence["actual_dimensions_m"] = [actual_width, actual_height]
        evidence["evaluated_dimensions_m"] = [evaluated_width, evaluated_height]
        evidence["evaluated_bounds_verified"] = True
        has_visible_source = text_has_visible_ink(getattr(text_item, "text", ""))
        evidence["source_has_visible_characters"] = has_visible_source
        # A whitespace-only PDF span legitimately has no rendered bounding
        # geometry. Preserve its exact editable body and transform; do not
        # invent visible geometry or downgrade its requested representation.
        if has_visible_source and target_width > 1e-9:
            width_tolerance = max(1e-7, target_width * 1e-3)
            if abs(actual_width - target_width) > width_tolerance:
                failures.append("width_scale_mismatch")
        if has_visible_source and target_height > 1e-9:
            height_tolerance = max(1e-7, target_height * 1e-3)
            if abs(actual_height - target_height) > height_tolerance:
                failures.append("height_scale_mismatch")
        if abs(evaluated_width - actual_width) > max(1e-7, actual_width * 1e-3):
            failures.append("evaluated_width_mismatch")
        if abs(evaluated_height - actual_height) > max(1e-7, actual_height * 1e-3):
            failures.append("evaluated_height_mismatch")
    except (AttributeError, IndexError, TypeError, ValueError):
        failures.append("transform_unverifiable")
    return failures, evidence


def _verify_font_candidate(obj, text_item, *, delivered: str, item_id: str):
    failures = []
    verification_evidence: Dict[str, Any] = {"item_id": item_id}
    if str(getattr(obj, "type", "")) != "FONT":
        failures.append("actual_object_type_not_FONT")
    if str(getattr(getattr(obj, "data", None), "body", "")) != str(text_item.text):
        failures.append("source_text_mismatch")
    extrusion = float(getattr(getattr(obj, "data", None), "extrude", 0.0) or 0.0)
    if not math.isfinite(extrusion):
        failures.append("nonfinite_text_extrusion")
    if delivered == "3d_text" and extrusion <= 0.0:
        failures.append("3d_text_has_no_positive_extrusion")
    if delivered == "text" and abs(extrusion) > 1e-12:
        failures.append("flat_text_has_extrusion")
    expected_font_sha = str(
        getattr(getattr(text_item, "font_asset", None), "usable_sha256", "") or ""
    )
    try:
        packed_font_sha = verify_packed_sha256(obj.data.font, expected_font_sha)
    except (AttributeError, PackedAssetError):
        packed_font_sha = ""
        failures.append("exact_font_not_packed_with_verified_bytes")
    verification_evidence["packed_font_sha256"] = packed_font_sha
    material_failures, material_evidence = _verify_text_material(obj)
    failures.extend(material_failures)
    verification_evidence.update(material_evidence)
    transform_failures, transform_evidence = _verify_transform_and_dimensions(obj, text_item)
    failures.extend(transform_failures)
    verification_evidence.update(transform_evidence)
    if failures:
        return AttemptOutcome.failed(
            "requested_font_representation_visual_verification_failed",
            evidence={**verification_evidence, "failures": failures},
            owned_artifacts=_owned_artifacts_for_text_entity(obj, obj.data),
            owned_objects=_owned_objects_for_text_entity(obj),
            owned_datablocks=(obj.data,),
        )
    return AttemptOutcome.delivered(
        obj,
        entity_ids=(obj.name,),
        evidence={
            **_font_asset_evidence(text_item),
            **verification_evidence,
            "actual_object_type": "FONT",
            "body_verified": True,
            "font_sha256": str(obj.get("pdf_exact_font_sha256", "")),
            "font_packed_sha256": packed_font_sha,
            "anchor_verified": True,
            "rotation_verified": True,
            "width_mm": float(getattr(text_item, "advance_width", 0.0) or 0.0),
            "height_mm": float(getattr(text_item, "glyph_height", 0.0) or 0.0),
            "extrusion_m": extrusion,
        },
        owned_artifacts=_owned_artifacts_for_text_entity(obj, obj.data),
        owned_objects=_owned_objects_for_text_entity(obj),
        owned_datablocks=(obj.data,),
    )


def _verify_converted_candidate(final, data, text_item, *, expected_type: str, item_id: str):
    failures = []
    evidence: Dict[str, Any] = {
        "item_id": item_id,
        "actual_object_type": str(getattr(final, "type", "")),
    }
    if evidence["actual_object_type"] != expected_type:
        failures.append(f"actual_object_type_not_{expected_type}")
    try:
        source_text = str(final.get("pdf_text_source", ""))
    except (AttributeError, ReferenceError, TypeError):
        source_text = ""
    evidence["source_text_preserved"] = source_text
    if source_text != str(text_item.text):
        failures.append("source_text_metadata_mismatch")
    material_failures, material_evidence = _verify_text_material(final)
    failures.extend(material_failures)
    evidence.update(material_evidence)
    transform_failures, transform_evidence = _verify_transform_and_dimensions(final, text_item)
    failures.extend(transform_failures)
    evidence.update(transform_evidence)
    if failures:
        return AttemptOutcome.failed(
            "converted_representation_visual_verification_failed",
            evidence={**evidence, "failures": failures},
            owned_artifacts=_owned_artifacts_for_text_entity(final, data),
            owned_objects=_owned_objects_for_text_entity(final),
            owned_datablocks=(data,),
        ), evidence
    return None, evidence


def _remove_object_and_data(obj, data, collection):
    """Remove a superseded candidate and return cleanup plus remaining refs."""
    removed = []
    object_name = str(getattr(obj, "name", "") or "")
    data_name = str(getattr(data, "name", "") or "")
    data_type = _datablock_kind(data)
    if data_type == "CURVE":
        registry = getattr(bpy.data, "curves", None)
    elif data_type == "MESH":
        registry = getattr(bpy.data, "meshes", None)
    else:
        registry = None
    remove = getattr(registry, "remove", None)
    if not callable(remove):
        return (
            {
                "status": "failed",
                "removed": removed,
                "detail": f"no datablock remover for kind {data_type or '<unknown>'}",
            },
            (obj,),
            (data,),
        )
    try:
        collection.objects.unlink(obj)
    except Exception:
        pass
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        removed.append(object_name)
        remaining_objects = ()
    except Exception as exc:
        return (
            {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            (obj,),
            (data,) if data is not None else (),
        )
    try:
        remove(data)
        removed.append(data_name)
    except Exception as exc:
        return (
            {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            remaining_objects,
            (data,) if data is not None else (),
        )
    return {"status": "complete", "removed": removed}, (), ()


def _copy_object_transform(source, target) -> None:
    """Copy placement after evaluated conversion, without reapplying baked scale."""
    for name in ("location", "rotation_euler", "color"):
        try:
            value = getattr(source, name)
            setattr(target, name, value.copy() if hasattr(value, "copy") else value)
        except Exception:
            pass
    try:
        target.scale = [1.0, 1.0, 1.0]
    except Exception:
        pass
    for key in (
        "pdf_baseline_alignment",
        "pdf_baseline_compensation_m",
        "pdf_full_affine_applied",
        "pdf_metric_affine_applied",
        "pdf_target_quad_model",
        "pdf_affine_matrix",
        "pdf_affine_carrier",
        "pdf_affine_carrier_owned",
        "pdf_metric_glyph_id",
        "pdf_metric_units_per_em",
        "pdf_metric_ascender",
        "pdf_metric_descender",
        "pdf_metric_advance_units",
        "pdf_metric_line_height_units",
        "pdf_metric_design_unit_scale",
        "pdf_metric_local_advance",
        "pdf_metric_local_matrix_horizontal_extent",
        "pdf_metric_matrix_horizontal_extent_source",
        "pdf_metric_local_line_height",
        "pdf_metric_local_baseline_y",
        "pdf_metric_metric_source",
        "pdf_metric_zero_ink_identity",
        "pdf_metric_zero_advance_logical_proof",
        "pdf_metric_expected_world_ink_bounds_m",
        "pdf_metric_target_origin_m",
        "pdf_metric_target_horizontal_axis_m",
        "pdf_metric_target_vertical_axis_m",
    ):
        try:
            target[key] = source.get(key)
        except (AttributeError, ReferenceError, TypeError):
            pass
    try:
        if bool(source.get("pdf_full_affine_applied", False)):
            carrier_owned = bool(source.get("pdf_affine_carrier_owned", False))
            carrier = getattr(source, "parent", None) if carrier_owned else None
            if carrier is not None:
                target.parent = carrier
                inverse = source.matrix_parent_inverse
                basis = source.matrix_basis
                target.matrix_parent_inverse = (
                    inverse.copy() if hasattr(inverse, "copy") else inverse
                )
                target.matrix_basis = basis.copy() if hasattr(basis, "copy") else basis
                carrier["pdf_affine_carrier_for"] = str(target.name)
            else:
                matrix = source.matrix_world
                target.matrix_world = matrix.copy() if hasattr(matrix, "copy") else matrix
    except (AttributeError, ReferenceError, TypeError):
        pass


def _visible_zero_advance_exact_contour_required(text_item) -> bool:
    try:
        advance = float(text_item.advance_width)
        glyph_id = int(text_item.source_glyph_id)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        bool(getattr(text_item, "positioned_character", False))
        and advance == 0.0
        and glyph_id >= 0
        and text_has_visible_ink(getattr(text_item, "text", ""))
    )


def _positioned_item_uses_only_exact_contours(text_item, requested: str) -> bool:
    if requested not in {"glyphs", "geometry"}:
        return False
    layouts = tuple(getattr(text_item, "source_char_layout", ()) or ())
    if not layouts:
        return False
    for layout in layouts:
        try:
            advance = float(layout.advance_width)
            glyph_id = int(layout.glyph_id)
        except (AttributeError, TypeError, ValueError):
            return False
        if (
            advance != 0.0
            or glyph_id < 0
            or not text_has_visible_ink(getattr(layout, "text", ""))
        ):
            return False
    return True


_EXACT_CONTOUR_METADATA_KEYS = (
    "pdf_exact_contour_source",
    "pdf_exact_contour_font_sha256",
    "pdf_exact_contour_glyph_id",
    "pdf_exact_contour_count",
    "pdf_exact_contour_segment_count",
    "pdf_exact_contour_design_bounds",
    "pdf_exact_contour_bypassed_host_font_shaping",
    "pdf_exact_contour_self_contained",
)


def _copy_exact_contour_metadata(source, target) -> None:
    for key in _EXACT_CONTOUR_METADATA_KEYS:
        try:
            target[key] = source.get(key)
        except (AttributeError, ReferenceError, TypeError):
            pass


def _populate_exact_embedded_glyph_curve(curve_data, contours, metric_evidence):
    design_unit_scale = float(metric_evidence["design_unit_scale"])
    baseline_y = float(metric_evidence["local_baseline_y"])
    if (
        not math.isfinite(design_unit_scale)
        or design_unit_scale <= 0.0
        or not math.isfinite(baseline_y)
    ):
        raise RuntimeError("exact embedded glyph local metric domain is invalid")

    curve_data.dimensions = "2D"
    curve_data.fill_mode = "BOTH"
    curve_data.resolution_u = max(
        int(getattr(curve_data, "resolution_u", 12) or 12),
        24,
    )
    if hasattr(curve_data, "render_resolution_u"):
        curve_data.render_resolution_u = max(
            int(getattr(curve_data, "render_resolution_u", 0) or 0),
            24,
        )

    def local_point(point):
        return (
            float(point[0]) * design_unit_scale,
            float(point[1]) * design_unit_scale + baseline_y,
            0.0,
        )

    segment_count = 0
    for contour in contours:
        if not contour:
            raise RuntimeError("exact embedded glyph contour is empty")
        for index, segment in enumerate(contour):
            next_segment = contour[(index + 1) % len(contour)]
            if tuple(segment[3]) != tuple(next_segment[0]):
                raise RuntimeError("exact embedded glyph contour is discontinuous")
        spline = curve_data.splines.new("BEZIER")
        spline.bezier_points.add(len(contour) - 1)
        spline.use_cyclic_u = True
        spline.resolution_u = max(
            int(getattr(spline, "resolution_u", 12) or 12),
            24,
        )
        for index, segment in enumerate(contour):
            point = spline.bezier_points[index]
            point.co = local_point(segment[0])
            point.handle_left_type = "FREE"
            point.handle_right_type = "FREE"
            # A new Blender curve point carries radius=1 even when the curve
            # has no bevel or extrusion. Blender 5.2 includes that dormant
            # radius in evaluated CURVE bounds, inflating an exact millimeter
            # outline by one meter. Zero is the truthful radial thickness for
            # this flat embedded-font contour and leaves the contour unchanged.
            point.radius = 0.0
        for index, segment in enumerate(contour):
            point = spline.bezier_points[index]
            next_point = spline.bezier_points[(index + 1) % len(contour)]
            point.handle_right = local_point(segment[1])
            next_point.handle_left = local_point(segment[2])
        segment_count += len(contour)
    return len(contours), segment_count


def _create_exact_embedded_glyph_candidate(
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    visual_style,
    z_offset_m,
    entity_suffix,
    baseline_alignment,
    mesh_factory=None,
):
    """Emit a visible zero-advance glyph without host FONT shaping."""
    curve_obj = None
    curve_data = None
    final = None
    final_data = None
    material = None
    try:
        glyph_id = int(text_item.source_glyph_id)
        asset = text_item.font_asset
        asset_page = int(asset.page_number)
        asset_span_font = str(asset.span_font_name or "")
        expected_span_font = str(text_item.font_name or "")
        asset_format = str(asset.usable_format or "").lower().lstrip(".")
        if (
            asset_page != int(page_number)
            or asset_span_font != expected_span_font
            or asset_format not in {"cff", "otf", "ttf"}
        ):
            raise RuntimeError("exact embedded glyph contour asset identity is invalid")
        metrics = _positioned_font_axis_metrics_values(
            text_item,
            size=float(text_item.font_size) * MM_TO_M,
            baseline_alignment=baseline_alignment,
        )
        if (
            metrics.get("zero_ink_identity") is not False
            or float(metrics.get("local_advance", math.nan)) != 0.0
            or metrics.get("matrix_horizontal_extent_source")
            != "exact_source_glyph_ink_width"
        ):
            raise RuntimeError(
                "exact contour routing requires a visible zero-advance source glyph"
            )
        contours = _exact_glyph_cubic_contours(asset, glyph_id)
        metrics["source_ink_contours_design_units"] = contours
        design_bounds = _exact_glyph_design_bounds(asset, glyph_id)
        if design_bounds is None:
            raise RuntimeError("visible embedded glyph has no exact design bounds")

        name = f"P{page_number}_text_{delivered}_{int(text_item.id)}{entity_suffix}"
        curve_data = bpy.data.curves.new(name=name, type="CURVE")
        contour_count, segment_count = _populate_exact_embedded_glyph_curve(
            curve_data,
            contours,
            metrics,
        )
        curve_obj = bpy.data.objects.new(name, curve_data)
        _set_object_metadata(
            curve_obj,
            item_id=item_id,
            requested=requested,
            delivered=delivered,
            text_item=text_item,
        )
        curve_obj["pdf_baseline_alignment"] = baseline_alignment
        curve_obj["pdf_baseline_compensation_m"] = 0.0
        curve_obj["pdf_text_source"] = str(text_item.text)
        curve_obj["pdf_exact_contour_source"] = "embedded_font_glyph_outline"
        curve_obj["pdf_exact_contour_font_sha256"] = str(asset.usable_sha256)
        curve_obj["pdf_exact_contour_glyph_id"] = glyph_id
        curve_obj["pdf_exact_contour_count"] = contour_count
        curve_obj["pdf_exact_contour_segment_count"] = segment_count
        curve_obj["pdf_exact_contour_design_bounds"] = [
            float(value) for value in design_bounds
        ]
        curve_obj["pdf_exact_contour_bypassed_host_font_shaping"] = True
        curve_obj["pdf_exact_contour_self_contained"] = True
        curve_obj["pdf_exact_font_packed"] = False

        material = _get_or_create_text_material(
            visual_style,
            source_color=text_item.color,
        )
        expected_rgb = _styled_text_color(
            visual_style,
            source_color=text_item.color,
        )
        curve_obj["pdf_text_material"] = str(getattr(material, "name", "") or "")
        curve_obj["pdf_text_material_owned"] = True
        curve_obj["pdf_text_expected_rgba"] = [
            *tuple(float(value) for value in expected_rgb),
            1.0,
        ]
        curve_data.materials.append(material)
        curve_obj.color = material.diffuse_color
        collection.objects.link(curve_obj)
        _apply_target_quad_affine(
            curve_obj,
            text_item,
            z_offset_m,
            collection=collection,
            positioned_metric_evidence=metrics,
        )
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        if delivered == "glyphs":
            final = curve_obj
            final_data = curve_data
        elif delivered == "geometry":
            if not callable(mesh_factory):
                raise RuntimeError("exact contour mesh conversion capability is unavailable")
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = curve_obj.evaluated_get(depsgraph)
            final_data = mesh_factory(evaluated, depsgraph=depsgraph)
            if not list(getattr(final_data, "vertices", []) or []):
                raise RuntimeError("exact contour mesh has no vertices")
            final_data.name = f"{name}_mesh"
            final = bpy.data.objects.new(name, final_data)
            _copy_object_transform(curve_obj, final)
            _set_object_metadata(
                final,
                item_id=item_id,
                requested=requested,
                delivered=delivered,
                text_item=text_item,
            )
            _copy_text_material_metadata(curve_obj, final)
            _copy_exact_contour_metadata(curve_obj, final)
            final["pdf_exact_font_packed"] = False
            final["pdf_text_source"] = str(text_item.text)
            collection.objects.link(final)
            cleanup, remaining_objects, remaining_data = _remove_object_and_data(
                curve_obj,
                curve_data,
                collection,
            )
            if cleanup.get("status") != "complete":
                raise _OwnedConstructionError(
                    "superseded exact contour curve cleanup failed",
                    owned_objects=(*remaining_objects, final),
                    owned_datablocks=(*remaining_data, final_data),
                )
            curve_obj = None
            curve_data = None
        else:  # pragma: no cover - caller constrains the rung
            raise ValueError(f"unsupported exact contour representation: {delivered}")

        evidence = {
            "exact_contour_source": "embedded_font_glyph_outline",
            "exact_contour_font_sha256": str(asset.usable_sha256),
            "exact_contour_glyph_id": glyph_id,
            "exact_contour_count": contour_count,
            "exact_contour_segment_count": segment_count,
            "exact_contour_design_bounds": [
                float(value) for value in design_bounds
            ],
            "exact_contour_bypassed_host_font_shaping": True,
            "exact_contour_self_contained": True,
        }
        return final, final_data, None, evidence
    except Exception as exc:
        construction_objects = tuple(getattr(exc, "owned_objects", ()) or ())
        construction_data = tuple(getattr(exc, "owned_datablocks", ()) or ())
        objects = _unique_owned_objects(curve_obj, final, *construction_objects)
        datablocks = []
        for value in (
            curve_data,
            final_data,
            material,
            *construction_data,
        ):
            if _valid_owned_ref(value) and all(
                value is not existing for existing in datablocks
            ):
                datablocks.append(value)
        failure = AttemptOutcome.failed(
            "exact_embedded_glyph_contour_construction_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
                "exact_contour_bypassed_host_font_shaping": True,
            },
            owned_artifacts=tuple(
                _artifact(obj, getattr(obj, "data", None)) for obj in objects
            ) + tuple(
                _artifact(None, data)
                for data in datablocks
                if all(data is not getattr(obj, "data", None) for obj in objects)
            ),
            owned_objects=objects,
            owned_datablocks=tuple(datablocks),
        )
        return final, final_data, failure, None


def _attempt_labels(
    item_id: str,
    page_number: int,
    source_span_id: int,
) -> AttemptOutcome:
    return AttemptOutcome.impossible(
        "blender_has_no_persistent_renderable_model_label_entity_for_item",
        evidence={
            **_host_capability_evidence(
                item_id,
                page_number,
                source_span_id,
                "persistent_renderable_model_label",
            ),
            "persistent": False,
            "model_scaled": False,
            "renderable": False,
        },
    )


def _attempt_native_font(
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    visual_style,
    z_offset_m,
    entity_suffix="",
):
    obj, _data, failure = _create_font_candidate(
        text_item,
        collection,
        page_number=page_number,
        requested=requested,
        delivered=delivered,
        item_id=item_id,
        visual_style=visual_style,
        z_offset_m=z_offset_m,
        entity_suffix=entity_suffix,
    )
    if failure is not None:
        return failure
    return _verify_font_candidate(obj, text_item, delivered=delivered, item_id=item_id)


def _attempt_glyphs(
    text_item,
    collection,
    *,
    page_number,
    requested,
    item_id,
    visual_style,
    z_offset_m,
    entity_suffix="",
):
    curve_data = None
    final = None
    obj, data, failure = _create_font_candidate(
        text_item,
        collection,
        page_number=page_number,
        requested=requested,
        delivered="glyphs",
        item_id=item_id,
        visual_style=visual_style,
        z_offset_m=z_offset_m,
        entity_suffix=entity_suffix,
    )
    if failure is not None:
        return failure
    source_verification = _verify_font_candidate(
        obj, text_item, delivered="text", item_id=item_id
    )
    if source_verification.status != "delivered":
        return source_verification
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        to_curve = getattr(evaluated, "to_curve", None)
        if not callable(to_curve):
            return AttemptOutcome.impossible(
                "evaluated_font_to_curve_capability_absent_for_item",
                evidence=_host_capability_evidence(
                    item_id,
                    page_number,
                    int(text_item.id),
                    "Object.to_curve",
                ),
                owned_artifacts=_owned_artifacts_for_text_entity(obj, data),
                owned_objects=_owned_objects_for_text_entity(obj),
                owned_datablocks=(data,),
            )
        converted = to_curve(depsgraph, apply_modifiers=False)
        try:
            curve_data = converted.copy()
        finally:
            clear = getattr(evaluated, "to_curve_clear", None)
            if callable(clear):
                clear()
        if not list(getattr(curve_data, "splines", []) or []):
            return AttemptOutcome.failed(
                "glyph_curve_has_no_verified_splines",
                evidence={"item_id": item_id},
                owned_artifacts=(
                    *_owned_artifacts_for_text_entity(obj, data),
                    _artifact(None, curve_data),
                ),
                owned_objects=_owned_objects_for_text_entity(obj),
                owned_datablocks=(data, curve_data),
            )
        curve_data.name = f"{obj.name}_glyph_curve"
        final = bpy.data.objects.new(obj.name, curve_data)
        _copy_object_transform(obj, final)
        _set_object_metadata(
            final,
            item_id=item_id,
            requested=requested,
            delivered="glyphs",
            text_item=text_item,
        )
        _copy_text_material_metadata(obj, final)
        final["pdf_text_source"] = str(text_item.text)
        collection.objects.link(final)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        if not bool(getattr(text_item, "positioned_character", False)):
            _fit_text_to_bbox(final, text_item)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        superseded_cleanup, remaining_objects, remaining_data = _remove_object_and_data(
            obj, data, collection
        )
        if superseded_cleanup.get("status") != "complete":
            return AttemptOutcome.failed(
                "superseded_font_candidate_cleanup_failed",
                evidence={"item_id": item_id, "cleanup": superseded_cleanup},
                owned_artifacts=(
                    *_owned_artifacts_for_text_entity(obj, data),
                    *_owned_artifacts_for_text_entity(final, curve_data),
                ),
                owned_objects=_unique_owned_objects(*remaining_objects, final),
                owned_datablocks=tuple(remaining_data) + (curve_data,),
            )
    except Exception as exc:
        owned_objects = _unique_owned_objects(obj, final)
        owned_data = tuple(value for value in (data, curve_data) if _valid_owned_ref(value))
        artifacts = (
            *_owned_artifacts_for_text_entity(obj, data),
            *_owned_artifacts_for_text_entity(final, curve_data),
        )
        return AttemptOutcome.failed(
            "glyph_curve_conversion_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=artifacts,
            owned_objects=owned_objects,
            owned_datablocks=owned_data,
        )
    verification_failure, verification_evidence = _verify_converted_candidate(
        final, curve_data, text_item, expected_type="CURVE", item_id=item_id
    )
    if verification_failure is not None:
        return verification_failure
    return AttemptOutcome.delivered(
        final,
        entity_ids=(final.name,),
        evidence={
            "item_id": item_id,
            **verification_evidence,
            "spline_count": len(curve_data.splines),
        },
        owned_artifacts=_owned_artifacts_for_text_entity(final, curve_data),
        owned_objects=_owned_objects_for_text_entity(final),
        owned_datablocks=(curve_data,),
    )


def _attempt_geometry(
    text_item,
    collection,
    *,
    page_number,
    requested,
    item_id,
    visual_style,
    z_offset_m,
    entity_suffix="",
):
    mesh_factory = getattr(getattr(bpy.data, "meshes", None), "new_from_object", None)
    if not callable(mesh_factory):
        return AttemptOutcome.impossible(
            "evaluated_font_to_mesh_capability_absent_for_item",
            evidence=_host_capability_evidence(
                item_id,
                page_number,
                int(text_item.id),
                "meshes.new_from_object",
            ),
        )
    mesh = None
    final = None
    obj, data, failure = _create_font_candidate(
        text_item,
        collection,
        page_number=page_number,
        requested=requested,
        delivered="geometry",
        item_id=item_id,
        visual_style=visual_style,
        z_offset_m=z_offset_m,
        entity_suffix=entity_suffix,
    )
    if failure is not None:
        return failure
    source_verification = _verify_font_candidate(
        obj, text_item, delivered="text", item_id=item_id
    )
    if source_verification.status != "delivered":
        return source_verification
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = mesh_factory(evaluated, depsgraph=depsgraph)
        if not list(getattr(mesh, "vertices", []) or []):
            return AttemptOutcome.failed(
                "geometry_mesh_has_no_verified_vertices",
                evidence={"item_id": item_id},
                owned_artifacts=(
                    *_owned_artifacts_for_text_entity(obj, data),
                    _artifact(None, mesh),
                ),
                owned_objects=_owned_objects_for_text_entity(obj),
                owned_datablocks=(data, mesh),
            )
        mesh.name = f"{obj.name}_mesh"
        final = bpy.data.objects.new(obj.name, mesh)
        _copy_object_transform(obj, final)
        _set_object_metadata(
            final,
            item_id=item_id,
            requested=requested,
            delivered="geometry",
            text_item=text_item,
        )
        _copy_text_material_metadata(obj, final)
        final["pdf_text_source"] = str(text_item.text)
        collection.objects.link(final)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        if not bool(getattr(text_item, "positioned_character", False)):
            _fit_text_to_bbox(final, text_item)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        superseded_cleanup, remaining_objects, remaining_data = _remove_object_and_data(
            obj, data, collection
        )
        if superseded_cleanup.get("status") != "complete":
            return AttemptOutcome.failed(
                "superseded_font_candidate_cleanup_failed",
                evidence={"item_id": item_id, "cleanup": superseded_cleanup},
                owned_artifacts=(
                    *_owned_artifacts_for_text_entity(obj, data),
                    *_owned_artifacts_for_text_entity(final, mesh),
                ),
                owned_objects=_unique_owned_objects(*remaining_objects, final),
                owned_datablocks=tuple(remaining_data) + (mesh,),
            )
    except Exception as exc:
        owned_objects = _unique_owned_objects(obj, final)
        owned_data = tuple(value for value in (data, mesh) if _valid_owned_ref(value))
        artifacts = (
            *_owned_artifacts_for_text_entity(obj, data),
            *_owned_artifacts_for_text_entity(final, mesh),
        )
        return AttemptOutcome.failed(
            "geometry_mesh_conversion_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=artifacts,
            owned_objects=owned_objects,
            owned_datablocks=owned_data,
        )
    verification_failure, verification_evidence = _verify_converted_candidate(
        final, mesh, text_item, expected_type="MESH", item_id=item_id
    )
    if verification_failure is not None:
        return verification_failure
    return AttemptOutcome.delivered(
        final,
        entity_ids=(final.name,),
        evidence={
            "item_id": item_id,
            **verification_evidence,
            "vertex_count": len(mesh.vertices),
        },
        owned_artifacts=_owned_artifacts_for_text_entity(final, mesh),
        owned_objects=_owned_objects_for_text_entity(final),
        owned_datablocks=(mesh,),
    )


def _attempt_raster_impl(
    text_item,
    collection,
    *,
    page_number,
    requested,
    item_id,
    terminal_raster_callback,
):
    if not callable(terminal_raster_callback):
        return AttemptOutcome.failed(
            "terminal_raster_callback_unavailable",
            evidence={"item_id": item_id, "source_bbox_pdf": getattr(text_item, "source_bbox_pdf", None)},
        )
    try:
        obj = terminal_raster_callback(text_item, collection, page_number, item_id)
    except Exception as exc:
        return AttemptOutcome.failed(
            "terminal_raster_attempt_raised",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
        )
    if obj is None:
        return AttemptOutcome.failed(
            "terminal_raster_not_verified",
            evidence={"item_id": item_id, "source_bbox_pdf": getattr(text_item, "source_bbox_pdf", None)},
        )
    try:
        _set_object_metadata(
            obj,
            item_id=item_id,
            requested=requested,
            delivered="raster",
            text_item=text_item,
        )
        entity_id = str(obj.name or "")
    except Exception as exc:
        return AttemptOutcome.failed(
            "terminal_raster_identity_unverified",
            evidence={"item_id": item_id, "exception_type": type(exc).__name__, "detail": str(exc)},
            owned_artifacts=(_raster_artifact(obj),),
            owned_objects=(obj,),
            owned_datablocks=(obj.data,) if getattr(obj, "data", None) is not None else (),
        )
    if not entity_id:
        return AttemptOutcome.failed(
            "terminal_raster_identity_unverified",
            evidence={"item_id": item_id},
            owned_artifacts=(_raster_artifact(obj),),
            owned_objects=(obj,),
            owned_datablocks=(obj.data,) if getattr(obj, "data", None) is not None else (),
        )
    failures = []
    verification_evidence: Dict[str, Any] = {"item_id": item_id}
    actual_type = str(getattr(obj, "type", ""))
    verification_evidence["actual_object_type"] = actual_type
    if actual_type != "MESH":
        failures.append("actual_object_type_not_MESH")
    try:
        actual_item_id = str(obj.get("pdf_raster_source_item_id", "") or "")
        actual_source_bbox = [float(value) for value in obj.get("pdf_raster_source_bbox_pdf", [])]
        image_path = str(obj.get("pdf_image_path", "") or "")
        image_name = str(obj.get("pdf_image_datablock", "") or "")
        image_packed = bool(obj.get("pdf_image_packed", False))
        image_metadata_sha = str(obj.get("pdf_image_sha256", "") or "")
    except (AttributeError, TypeError, ValueError):
        actual_item_id = ""
        actual_source_bbox = []
        image_path = ""
        image_name = ""
        image_packed = False
        image_metadata_sha = ""
    expected_source_bbox = [float(value) for value in (getattr(text_item, "source_bbox_pdf", None) or [])]
    verification_evidence["source_item_id"] = actual_item_id
    verification_evidence["source_bbox_pdf"] = actual_source_bbox
    verification_evidence["image_path"] = image_path
    verification_evidence["image_datablock"] = image_name
    if actual_item_id != item_id:
        failures.append("source_item_identity_mismatch")
    if not all(
        math.isfinite(value)
        for value in (*expected_source_bbox, *actual_source_bbox)
    ):
        failures.append("raster_nonfinite_geometry")
    if len(expected_source_bbox) != 4 or len(actual_source_bbox) != 4 or any(
        abs(actual - expected) > 1e-7
        for actual, expected in zip(  # noqa: B905
            actual_source_bbox, expected_source_bbox
        )
    ):
        failures.append("source_clip_bbox_mismatch")
    image = None
    if not image_path or not os.path.isfile(image_path) or os.path.getsize(image_path) <= 0:
        failures.append("raster_clip_file_unverified")
    else:
        try:
            clip_sha = sha256(Path(image_path).read_bytes()).hexdigest()
        except OSError:
            clip_sha = ""
        verification_evidence["raster_clip_sha256"] = clip_sha
        verification_evidence["raster_image_metadata_sha256"] = image_metadata_sha
        if not image_packed:
            failures.append("raster_image_not_marked_packed")
        if not clip_sha or image_metadata_sha != clip_sha:
            failures.append("raster_image_digest_metadata_mismatch")
        images = getattr(getattr(bpy, "data", None), "images", None)
        get_image = getattr(images, "get", None)
        image = get_image(image_name) if callable(get_image) and image_name else None
        if image is None:
            failures.append("raster_packed_image_unavailable")
        else:
            try:
                packed_sha = verify_packed_sha256(image, clip_sha)
            except PackedAssetError:
                packed_sha = ""
                failures.append("raster_packed_image_hash_mismatch")
            verification_evidence["raster_packed_image_sha256"] = packed_sha
    try:
        material_name = str(obj.get("pdf_image_material", "") or "")
        material_owned = bool(obj.get("pdf_image_material_owned", False))
        image_owned = bool(obj.get("pdf_image_datablock_owned", False))
    except (AttributeError, TypeError, ValueError):
        material_name = ""
        material_owned = False
        image_owned = False
    materials_registry = getattr(getattr(bpy, "data", None), "materials", None)
    get_material = getattr(materials_registry, "get", None)
    material = (
        get_material(material_name)
        if callable(get_material) and material_name
        else None
    )
    mesh = getattr(obj, "data", None)
    try:
        assigned_materials = list(getattr(mesh, "materials", []) or [])
    except (AttributeError, ReferenceError, TypeError):
        assigned_materials = []
    material_assigned = material is not None and any(
        candidate is material
        or str(getattr(candidate, "name", "") or "") == material_name
        for candidate in assigned_materials
    )
    verification_evidence["raster_material"] = material_name
    verification_evidence["raster_material_owned"] = material_owned
    verification_evidence["raster_image_owned"] = image_owned
    verification_evidence["raster_material_assigned"] = material_assigned
    if not material_owned or not image_owned:
        failures.append("raster_attempt_resource_ownership_unverified")
    if material is None or not material_assigned or not bool(getattr(material, "use_nodes", False)):
        failures.append("raster_material_assignment_unverified")

    try:
        uv_layers = mesh.uv_layers
        get_uv = getattr(uv_layers, "get", None)
        uv_layer = get_uv("UVMap") if callable(get_uv) else None
        uv_count = len(list(getattr(uv_layer, "data", []) or [])) if uv_layer is not None else 0
        loop_count = len(list(getattr(mesh, "loops", []) or []))
    except (AttributeError, ReferenceError, TypeError):
        uv_count = 0
        loop_count = 0
    verification_evidence["raster_uv_count"] = uv_count
    verification_evidence["raster_loop_count"] = loop_count
    if uv_count <= 0 or loop_count <= 0 or uv_count != loop_count:
        failures.append("raster_uv_map_unverified")

    try:
        node_tree = material.node_tree
        nodes = list(node_tree.nodes)
        links = list(node_tree.links)
    except (AttributeError, ReferenceError, TypeError):
        nodes = []
        links = []
    texture_nodes = [node for node in nodes if str(getattr(node, "type", "")) == "TEX_IMAGE"]
    shader_nodes = [
        node for node in nodes if str(getattr(node, "type", "")) == "BSDF_PRINCIPLED"
    ]
    output_nodes = [
        node for node in nodes if str(getattr(node, "type", "")) == "OUTPUT_MATERIAL"
    ]
    texture_bound = any(getattr(node, "image", None) is image for node in texture_nodes)
    texture_to_shader = any(
        getattr(link, "from_node", None) in texture_nodes
        and getattr(link, "to_node", None) in shader_nodes
        for link in links
    )
    shader_to_output = any(
        getattr(link, "from_node", None) in shader_nodes
        and getattr(link, "to_node", None) in output_nodes
        for link in links
    )
    verification_evidence["raster_texture_image_bound"] = texture_bound
    verification_evidence["raster_texture_to_shader_linked"] = texture_to_shader
    verification_evidence["raster_shader_to_output_linked"] = shader_to_output
    if not texture_bound:
        failures.append("raster_material_image_binding_unverified")
    if not texture_to_shader or not shader_to_output:
        failures.append("raster_material_node_links_unverified")
    try:
        tx0, ty0, tx1, ty1 = (float(value) for value in text_item.bbox[:4])
        tx0, tx1 = sorted((tx0, tx1))
        ty0, ty1 = sorted((ty0, ty1))
        expected_location = (tx0 * MM_TO_M, ty0 * MM_TO_M)
        expected_dimensions = ((tx1 - tx0) * MM_TO_M, (ty1 - ty0) * MM_TO_M)
        actual_location = (float(obj.location[0]), float(obj.location[1]))
        actual_dimensions = (abs(float(obj.dimensions[0])), abs(float(obj.dimensions[1])))
        if not all(
            math.isfinite(value)
            for value in (
                *expected_location,
                *expected_dimensions,
                *actual_location,
                *actual_dimensions,
            )
        ):
            failures.append("raster_nonfinite_geometry")
        verification_evidence["expected_location_m"] = list(expected_location)
        verification_evidence["actual_location_m"] = list(actual_location)
        verification_evidence["expected_dimensions_m"] = list(expected_dimensions)
        verification_evidence["actual_dimensions_m"] = list(actual_dimensions)
        if any(
            abs(actual - expected) > 1e-7
            for actual, expected in zip(  # noqa: B905
                actual_location, expected_location
            )
        ):
            failures.append("raster_anchor_mismatch")
        if any(
            abs(actual - expected) > max(1e-7, expected * 1e-3)
            for actual, expected in zip(  # noqa: B905
                actual_dimensions, expected_dimensions
            )
        ):
            failures.append("raster_dimensions_mismatch")
    except (AttributeError, IndexError, TypeError, ValueError):
        failures.append("raster_placement_unverifiable")
    try:
        if not list(getattr(getattr(obj, "data", None), "vertices", []) or []):
            failures.append("raster_plane_has_no_verified_vertices")
    except (AttributeError, ReferenceError, TypeError):
        failures.append("raster_plane_geometry_unverifiable")
    if failures:
        return AttemptOutcome.failed(
            "terminal_raster_visual_verification_failed",
            evidence={**verification_evidence, "failures": failures},
            owned_artifacts=(_raster_artifact(obj),),
            owned_objects=(obj,),
            owned_datablocks=(obj.data,) if getattr(obj, "data", None) is not None else (),
        )
    return AttemptOutcome.delivered(
        obj,
        entity_ids=(entity_id,),
        evidence={
            **verification_evidence,
            "raster_verified": True,
            "placement_verified": True,
        },
        owned_artifacts=(_raster_artifact(obj),),
        owned_objects=(obj,),
        owned_datablocks=(obj.data,) if getattr(obj, "data", None) is not None else (),
    )


def _attempt_raster(
    text_item,
    collection,
    *,
    page_number,
    requested,
    item_id,
    terminal_raster_callback,
):
    """Retain ownership even if host verification raises after creation."""
    captured: Dict[str, Any] = {}

    def _capture_callback(*args, **kwargs):
        obj = terminal_raster_callback(*args, **kwargs)
        captured["object"] = obj
        return obj

    try:
        return _attempt_raster_impl(
            text_item,
            collection,
            page_number=page_number,
            requested=requested,
            item_id=item_id,
            terminal_raster_callback=(
                _capture_callback if callable(terminal_raster_callback) else terminal_raster_callback
            ),
        )
    except Exception as exc:
        obj = captured.get("object")
        if obj is None:
            return AttemptOutcome.failed(
                "terminal_raster_verification_raised",
                evidence={
                    "item_id": item_id,
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
        data = getattr(obj, "data", None)
        return AttemptOutcome.failed(
            "terminal_raster_verification_raised",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=(_raster_artifact(obj),),
            owned_objects=(obj,),
            owned_datablocks=(data,) if data is not None else (),
        )


def _cleanup_attempt(outcome: AttemptOutcome, collection) -> Dict[str, Any]:
    removed = []
    raster_resources = []
    for obj in tuple(outcome.owned_objects or ()):
        if obj is None:
            continue
        try:
            obj_name = str(getattr(obj, "name", "") or "")
            raster_resources.append({
                "path": str(obj.get("pdf_image_path", "") or ""),
                "material": str(obj.get("pdf_image_material", "") or ""),
                "material_owned": bool(obj.get("pdf_image_material_owned", False)),
                "image": str(obj.get("pdf_image_datablock", "") or ""),
                "image_owned": bool(obj.get("pdf_image_datablock_owned", False)),
            })
            text_material = str(obj.get("pdf_text_material", "") or "")
            if text_material:
                raster_resources.append({
                    "path": "",
                    "material": text_material,
                    "material_owned": bool(
                        obj.get("pdf_text_material_owned", False)
                    ),
                    "image": "",
                    "image_owned": False,
                })
        except ReferenceError:
            removed.append("<already_removed_object>")
            continue
        try:
            collection.objects.unlink(obj)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed.append(obj_name)
        except Exception as exc:
            return {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            }
    for data in tuple(outcome.owned_datablocks or ()):
        if data is None:
            continue
        try:
            data_type = _datablock_kind(data)
            data_name = str(getattr(data, "name", "") or "")
        except ReferenceError:
            removed.append("<already_removed_datablock>")
            continue
        registry = (
            getattr(bpy.data, "meshes", None)
            if data_type == "MESH"
            else getattr(bpy.data, "curves", None)
            if data_type == "CURVE"
            else getattr(bpy.data, "materials", None)
            if data_type == "MATERIAL"
            else getattr(bpy.data, "fonts", None)
            if data_type == "FONT"
            else None
        )
        remove = getattr(registry, "remove", None)
        if not callable(remove):
            return {
                "status": "failed",
                "removed": removed,
                "detail": f"no remover for owned datablock kind {data_type or '<unknown>'}",
            }
        try:
            remove(data)
            removed.append(data_name)
        except Exception as exc:
            return {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            }
    for resource in raster_resources:
        for registry_name, name_key, owned_key in (
            ("materials", "material", "material_owned"),
            ("images", "image", "image_owned"),
        ):
            name = resource.get(name_key, "")
            if not resource.get(owned_key) or not name:
                continue
            registry = getattr(bpy.data, registry_name, None)
            get = getattr(registry, "get", None)
            remove = getattr(registry, "remove", None)
            block = get(name) if callable(get) else None
            if block is None:
                continue
            try:
                if int(getattr(block, "users", 0) or 0) > 0:
                    raise RuntimeError(f"owned {registry_name} datablock still has users")
                if not callable(remove):
                    raise RuntimeError(f"no {registry_name} datablock remover")
                remove(block)
                removed.append(name)
            except Exception as exc:
                return {
                    "status": "failed",
                    "removed": removed,
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                }
        path = resource.get("path", "")
        if path and os.path.exists(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                return {
                    "status": "failed",
                    "removed": removed,
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                }
    return {"status": "complete", "removed": removed}


def cleanup_delivery_outcome(outcome: AttemptOutcome) -> Dict[str, Any]:
    """Remove every runtime-owned artifact from one delivered text outcome."""

    return _cleanup_attempt(outcome, None)


def _append_delivery_record(provenance_opts: Any, record: Dict[str, Any]) -> None:
    if provenance_opts is None:
        return
    records = getattr(provenance_opts, "_text_delivery_records", None)
    if not isinstance(records, list):
        records = []
        provenance_opts._text_delivery_records = records  # noqa: B010
    records.append(record)


def _positioned_zero_ink_source_manifest(
    text_item: NormalizedText,
    *,
    item_id: str,
    page_number: int,
    requested: str,
    z_offset_m: float,
    baseline_alignment: str,
) -> Dict[str, Any]:
    """Snapshot source character/layout truth before any host mutation."""
    if baseline_alignment not in {"BOTTOM_BASELINE", "BOTTOM"}:
        raise ValueError("a supported positioned baseline alignment is required")
    layouts = tuple(getattr(text_item, "source_char_layout", ()) or ())
    characters = []
    for index, layout in enumerate(layouts):
        glyph_id = getattr(layout, "glyph_id", None)
        zero_ink_character = text_is_zero_ink(layout.text)
        visible_zero_advance_character = bool(
            not zero_ink_character
            and float(layout.advance_width) == 0.0
            and glyph_id is not None
        )
        if zero_ink_character or visible_zero_advance_character:
            child = _character_text_item(text_item, layout)
            metrics = _positioned_font_axis_metrics_values(
                child,
                size=float(child.font_size) * MM_TO_M,
                baseline_alignment=baseline_alignment,
            )
            local_matrix_horizontal_extent = metrics.get(
                "local_matrix_horizontal_extent",
                metrics["local_advance"],
            )
            local_line_height = metrics["local_line_height"]
            local_baseline_y = metrics["local_baseline_y"]
        else:
            local_matrix_horizontal_extent = (
                float(layout.advance_width) * MM_TO_M
            )
            local_line_height = float(layout.glyph_height) * MM_TO_M
            local_baseline_y = 0.0
        intended_matrix = _metric_character_matrix_values(
            local_advance=local_matrix_horizontal_extent,
            local_line_height=local_line_height,
            local_baseline_y=local_baseline_y,
            target_origin=layout.target_origin,
            target_quad=layout.target_quad,
            z=float(z_offset_m),
            allow_zero_advance=zero_ink_character,
        )
        characters.append({
            "character_item_id": f"{item_id}:char:{index}",
            "character_index": index,
            "text": str(layout.text),
            "glyph_id": int(glyph_id) if glyph_id is not None else None,
            "advance_width_model": float(layout.advance_width),
            "glyph_height_model": float(layout.glyph_height),
            "source_origin_pdf": [
                float(layout.source_origin_pdf[0]),
                float(layout.source_origin_pdf[1]),
            ],
            "source_bbox_pdf": [
                float(value) for value in layout.source_bbox_pdf
            ],
            "source_quad_pdf": [
                [float(point[0]), float(point[1])]
                for point in layout.source_quad_pdf
            ],
            "target_origin_model": [
                float(layout.target_origin[0]),
                float(layout.target_origin[1]),
            ],
            "target_quad_model": [
                [float(point[0]), float(point[1])]
                for point in layout.target_quad
            ],
            "intended_affine_matrix": [
                float(value) for row in intended_matrix for value in row
            ],
        })
    return {
        "schema": ZERO_INK_SOURCE_MANIFEST_SCHEMA,
        "importer_id": IMPORTER_ID,
        "item_id": str(item_id),
        "page_number": int(page_number),
        "source_span_id": int(text_item.id),
        "requested_representation": str(requested),
        "source_text": str(text_item.text),
        "character_count": len(characters),
        "characters": characters,
    }


def _character_text_item(text_item: NormalizedText, layout) -> NormalizedText:
    target_quad = tuple(
        (float(point[0]), float(point[1])) for point in layout.target_quad
    )
    xs = [point[0] for point in target_quad]
    ys = [point[1] for point in target_quad]
    top_dx = target_quad[1][0] - target_quad[0][0]
    top_dy = target_quad[1][1] - target_quad[0][1]
    return replace(
        text_item,
        text=str(layout.text),
        normalized=str(layout.text).upper().strip(),
        insertion=(float(layout.target_origin[0]), float(layout.target_origin[1])),
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        source_bbox_pdf=tuple(float(value) for value in layout.source_bbox_pdf),
        source_quad_pdf=tuple(
            (float(point[0]), float(point[1])) for point in layout.source_quad_pdf
        ),
        target_quad_model=target_quad,
        advance_width=float(layout.advance_width),
        glyph_height=float(layout.glyph_height),
        rotation=math.degrees(math.atan2(top_dy, top_dx)),
        source_char_layout=(),
        requires_individual_positioning=False,
        positioned_character=True,
        source_glyph_id=(int(layout.glyph_id) if layout.glyph_id is not None else None),
    )


def _attempt_positioned_native_characters(
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    visual_style,
    z_offset_m,
    baseline_alignment,
):
    """Create one positioned source item, then evaluate all characters once."""
    layouts = tuple(getattr(text_item, "source_char_layout", ()) or ())
    candidates = []
    ownership_outcomes = []

    def character_record(index, layout, child, outcome):
        return {
            "character_index": index,
            "text": str(layout.text),
            "glyph_id": getattr(layout, "glyph_id", None),
            "source_origin_pdf": list(layout.source_origin_pdf),
            "source_quad_pdf": [list(point) for point in layout.source_quad_pdf],
            "target_origin_model": list(layout.target_origin),
            "target_quad_model": [list(point) for point in layout.target_quad],
            "positioned_character": bool(
                getattr(child, "positioned_character", False)
            ),
            "entity_ids": [str(value) for value in outcome.entity_ids],
            "verification": dict(outcome.evidence or {}),
        }

    def aggregate_failure(failed, index, outcomes, character_evidence):
        factory = (
            AttemptOutcome.impossible
            if failed.status == "impossible"
            else AttemptOutcome.failed
        )
        return factory(
            failed.reason,
            evidence={
                **dict(failed.evidence or {}),
                "failed_character_index": index,
                "character_entities": character_evidence,
            },
            owned_artifacts=tuple(
                artifact
                for candidate in outcomes
                for artifact in candidate.owned_artifacts
            ),
            owned_objects=tuple(
                obj for candidate in outcomes for obj in candidate.owned_objects
            ),
            owned_datablocks=tuple(
                data for candidate in outcomes for data in candidate.owned_datablocks
            ),
        )

    for index, layout in enumerate(layouts):
        child = _character_text_item(text_item, layout)
        glyph_id = getattr(layout, "glyph_id", None)
        suffix = f"_c{index:04d}_g{glyph_id if glyph_id is not None else 'na'}"
        obj, data, failure = _create_font_candidate(
            child,
            collection,
            page_number=page_number,
            requested=requested,
            delivered=delivered,
            item_id=item_id,
            visual_style=visual_style,
            z_offset_m=z_offset_m,
            entity_suffix=suffix,
            defer_host_update=True,
            baseline_alignment=baseline_alignment,
        )
        if failure is not None:
            outcomes = tuple(ownership_outcomes) + (failure,)
            evidence = [character_record(index, layout, child, failure)]
            return aggregate_failure(failure, index, outcomes, evidence)
        pending = AttemptOutcome.delivered(
            obj,
            entity_ids=(obj.name,),
            evidence={"item_id": item_id},
            owned_artifacts=_owned_artifacts_for_text_entity(obj, data),
            owned_objects=_owned_objects_for_text_entity(obj),
            owned_datablocks=(data,),
        )
        candidates.append((index, layout, child, obj))
        ownership_outcomes.append(pending)

    try:
        bpy.context.view_layer.update()
    except Exception as exc:
        return AttemptOutcome.failed(
            "positioned_native_batch_update_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=tuple(
                artifact
                for candidate in ownership_outcomes
                for artifact in candidate.owned_artifacts
            ),
            owned_objects=tuple(
                obj
                for candidate in ownership_outcomes
                for obj in candidate.owned_objects
            ),
            owned_datablocks=tuple(
                data
                for candidate in ownership_outcomes
                for data in candidate.owned_datablocks
            ),
        )

    outcomes = list(ownership_outcomes)
    character_evidence = []
    for position, (index, layout, child, obj) in enumerate(candidates):
        outcome = _verify_font_candidate(
            obj,
            child,
            delivered=delivered,
            item_id=item_id,
        )
        outcomes[position] = outcome
        character_evidence.append(character_record(index, layout, child, outcome))
        if outcome.status != "delivered":
            return aggregate_failure(
                outcome,
                index,
                tuple(outcomes),
                character_evidence,
            )
        try:
            outcome.entity["pdf_source_char_index"] = index
            outcome.entity["pdf_source_glyph_id"] = (
                int(layout.glyph_id) if layout.glyph_id is not None else -1
            )
        except (AttributeError, ReferenceError, TypeError, ValueError):
            pass

    first = outcomes[0]
    evidence = {
        **_font_asset_evidence(text_item),
        **dict(first.evidence or {}),
        "item_id": item_id,
        "character_positioning_preserved": True,
        "character_count": len(outcomes),
        "character_entities": character_evidence,
        "dependency_graph_updates": 1,
    }
    return AttemptOutcome.delivered(
        first.entity,
        entity_ids=tuple(
            entity_id for outcome in outcomes for entity_id in outcome.entity_ids
        ),
        evidence=evidence,
        owned_artifacts=tuple(
            artifact for outcome in outcomes for artifact in outcome.owned_artifacts
        ),
        owned_objects=tuple(
            obj for outcome in outcomes for obj in outcome.owned_objects
        ),
        owned_datablocks=tuple(
            data for outcome in outcomes for data in outcome.owned_datablocks
        ),
    )


def _prepare_positioned_converted_candidate(
    source,
    source_data,
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    depsgraph,
    source_verification,
    mesh_factory=None,
):
    """Convert one evaluated FONT without forcing a dependency-graph update."""
    final_data = None
    final = None
    source_evidence = dict(source_verification or {})
    try:
        source_marks_zero_ink = bool(
            source.get("pdf_metric_zero_ink_identity", False)
        )
    except (AttributeError, ReferenceError, TypeError):
        source_marks_zero_ink = False
    verified_zero_ink = (
        source_marks_zero_ink
        and source_evidence.get("zero_ink_identity") is True
        and source_evidence.get("evaluated_ink_bounds_verified") is True
        and text_is_zero_ink(getattr(text_item, "text", ""))
    )
    try:
        evaluated = source.evaluated_get(depsgraph)
        if delivered == "glyphs":
            to_curve = getattr(evaluated, "to_curve", None)
            if not callable(to_curve):
                return None, None, AttemptOutcome.impossible(
                    "evaluated_font_to_curve_capability_absent_for_item",
                    evidence=_host_capability_evidence(
                        item_id,
                        page_number,
                        int(text_item.id),
                        "Object.to_curve",
                    ),
                    owned_artifacts=_owned_artifacts_for_text_entity(
                        source, source_data
                    ),
                    owned_objects=_owned_objects_for_text_entity(source),
                    owned_datablocks=(source_data,),
                ), None
            converted = to_curve(depsgraph, apply_modifiers=False)
            try:
                final_data = converted.copy()
            finally:
                clear = getattr(evaluated, "to_curve_clear", None)
                if callable(clear):
                    clear()
            converted_splines = list(getattr(final_data, "splines", []) or [])
            if verified_zero_ink:
                return None, final_data, None, {
                    **source_evidence,
                    "item_id": item_id,
                    "zero_ink_identity": True,
                    "evaluated_ink_bounds_verified": True,
                    "conversion_outcome": "verified_zero_ink_no_physical_entity",
                    "converted_datablock_kind": "CURVE",
                    "converted_ink_element_count": len(converted_splines),
                    "discarded_host_placeholder_ink": bool(converted_splines),
                }
            if not converted_splines:
                return None, final_data, AttemptOutcome.failed(
                    "glyph_curve_has_no_verified_splines",
                    evidence={"item_id": item_id},
                    owned_artifacts=(
                        *_owned_artifacts_for_text_entity(source, source_data),
                        _artifact(None, final_data),
                    ),
                    owned_objects=_owned_objects_for_text_entity(source),
                    owned_datablocks=(source_data, final_data),
                ), None
            final_data.name = f"{source.name}_glyph_curve"
        elif delivered == "geometry":
            if not callable(mesh_factory):  # pragma: no cover - checked by caller
                raise RuntimeError("positioned mesh conversion capability disappeared")
            final_data = mesh_factory(evaluated, depsgraph=depsgraph)
            converted_vertices = list(getattr(final_data, "vertices", []) or [])
            if verified_zero_ink:
                return None, final_data, None, {
                    **source_evidence,
                    "item_id": item_id,
                    "zero_ink_identity": True,
                    "evaluated_ink_bounds_verified": True,
                    "conversion_outcome": "verified_zero_ink_no_physical_entity",
                    "converted_datablock_kind": "MESH",
                    "converted_ink_element_count": len(converted_vertices),
                    "discarded_host_placeholder_ink": bool(converted_vertices),
                }
            if not converted_vertices:
                return None, final_data, AttemptOutcome.failed(
                    "geometry_mesh_has_no_verified_vertices",
                    evidence={"item_id": item_id},
                    owned_artifacts=(
                        *_owned_artifacts_for_text_entity(source, source_data),
                        _artifact(None, final_data),
                    ),
                    owned_objects=_owned_objects_for_text_entity(source),
                    owned_datablocks=(source_data, final_data),
                ), None
            final_data.name = f"{source.name}_mesh"
        else:  # pragma: no cover - caller constrains the rung
            raise ValueError(f"unsupported positioned representation: {delivered}")

        final = bpy.data.objects.new(source.name, final_data)
        _copy_object_transform(source, final)
        _set_object_metadata(
            final,
            item_id=item_id,
            requested=requested,
            delivered=delivered,
            text_item=text_item,
        )
        _copy_text_material_metadata(source, final)
        final["pdf_text_source"] = str(text_item.text)
        collection.objects.link(final)
        return final, final_data, None, None
    except Exception as exc:
        reason = (
            "glyph_curve_conversion_failed_not_impossibility_proof"
            if delivered == "glyphs"
            else "geometry_mesh_conversion_failed_not_impossibility_proof"
        )
        return final, final_data, AttemptOutcome.failed(
            reason,
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
            owned_artifacts=(
                *_owned_artifacts_for_text_entity(source, source_data),
                *_owned_artifacts_for_text_entity(final, final_data),
            ),
            owned_objects=_unique_owned_objects(source, final),
            owned_datablocks=tuple(
                value
                for value in (source_data, final_data)
                if _valid_owned_ref(value)
            ),
        ), None


def _attempt_positioned_converted_characters(
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    visual_style,
    z_offset_m,
    source_manifest=None,
    source_manifest_sha256="",
    baseline_alignment=None,
):
    """Batch positioned FONT evaluation and CURVE/MESH verification by item."""
    layouts = tuple(getattr(text_item, "source_char_layout", ()) or ())
    mesh_factory = None
    if delivered == "geometry":
        mesh_factory = getattr(getattr(bpy.data, "meshes", None), "new_from_object", None)
        if not callable(mesh_factory):
            return AttemptOutcome.impossible(
                "evaluated_font_to_mesh_capability_absent_for_item",
                evidence=_host_capability_evidence(
                    item_id,
                    page_number,
                    int(text_item.id),
                    "meshes.new_from_object",
                ),
            )

    candidates = []
    character_evidence = []

    def character_record(candidate, outcome):
        layout = candidate["layout"]
        child = candidate["child"]
        verification = dict(outcome.evidence or {})
        character_id = f"{item_id}:char:{candidate['index']}"
        glyph_id = getattr(layout, "glyph_id", None)
        manifest_character = {}
        if isinstance(source_manifest, dict):
            manifest_characters = source_manifest.get("characters")
            if (
                isinstance(manifest_characters, list)
                and candidate["index"] < len(manifest_characters)
                and isinstance(manifest_characters[candidate["index"]], dict)
            ):
                manifest_character = manifest_characters[candidate["index"]]
        character_manifest = None
        character_manifest_sha256 = ""
        if verification.get("zero_ink_identity") is True:
            try:
                raw_character_manifest = make_zero_ink_character_manifest(
                    source_manifest,
                    source_manifest_sha256,
                    candidate["index"],
                )
                character_manifest, character_manifest_sha256 = (
                    freeze_zero_ink_source_manifest(raw_character_manifest)
                )
            except (AttributeError, TypeError, ValueError, OverflowError):
                character_manifest = None
                character_manifest_sha256 = ""
            verification.update({
                "source_manifest_schema": ZERO_INK_SOURCE_MANIFEST_SCHEMA,
                "source_manifest_sha256": source_manifest_sha256,
                "item_id": str(item_id),
                "page_number": int(page_number),
                "source_span_id": int(text_item.id),
                "character_item_id": character_id,
                "character_index": candidate["index"],
                "source_character_text": str(layout.text),
                "source_glyph_id": (
                    int(glyph_id) if glyph_id is not None else None
                ),
                "requested_representation": requested,
                "zero_ink_character_manifest_schema": (
                    ZERO_INK_CHARACTER_MANIFEST_SCHEMA
                ),
                "zero_ink_character_manifest_sha256": (
                    character_manifest_sha256
                ),
            })
        record = {
            "item_id": item_id,
            "character_item_id": character_id,
            "character_index": candidate["index"],
            "text": str(layout.text),
            "glyph_id": int(glyph_id) if glyph_id is not None else None,
            "advance_width_model": float(layout.advance_width),
            "glyph_height_model": float(layout.glyph_height),
            "source_origin_pdf": list(layout.source_origin_pdf),
            "source_bbox_pdf": list(layout.source_bbox_pdf),
            "source_quad_pdf": [list(point) for point in layout.source_quad_pdf],
            "target_origin_model": list(layout.target_origin),
            "target_quad_model": [list(point) for point in layout.target_quad],
            "intended_affine_matrix": list(
                manifest_character.get("intended_affine_matrix", ())
            ),
            "requested_representation": requested,
            "delivered_representation": delivered,
            "positioned_character": bool(
                getattr(child, "positioned_character", False)
            ),
            "entity_ids": [str(value) for value in outcome.entity_ids],
            "verification": verification,
        }
        if verification.get("zero_ink_identity") is True:
            record["source_manifest_sha256"] = source_manifest_sha256
            record["zero_ink_character_manifest"] = character_manifest
            record["zero_ink_character_manifest_sha256"] = (
                character_manifest_sha256
            )
        return record

    def ownership(extra_outcomes=()):
        artifacts = []
        objects = []
        datablocks = []

        def add_entity(obj, data):
            if obj is not None or data is not None:
                artifacts.extend(_owned_artifacts_for_text_entity(obj, data))
            for value in _owned_objects_for_text_entity(obj):
                if all(value is not existing for existing in objects):
                    objects.append(value)
            if _valid_owned_ref(data) and all(
                data is not existing for existing in datablocks
            ):
                datablocks.append(data)

        for candidate in candidates:
            add_entity(candidate.get("source"), candidate.get("source_data"))
            add_entity(candidate.get("final"), candidate.get("final_data"))
        for outcome in extra_outcomes:
            artifacts.extend(outcome.owned_artifacts)
            for value in outcome.owned_objects:
                if _valid_owned_ref(value) and all(
                    value is not existing for existing in objects
                ):
                    objects.append(value)
            for value in outcome.owned_datablocks:
                if _valid_owned_ref(value) and all(
                    value is not existing for existing in datablocks
                ):
                    datablocks.append(value)
        return tuple(artifacts), tuple(objects), tuple(datablocks)

    def aggregate_failure(failed, index=None, records=None, *, extra_evidence=None):
        artifacts, objects, datablocks = ownership((failed,))
        evidence = {**dict(failed.evidence or {})}
        if index is not None:
            evidence["failed_character_index"] = index
        if records is not None:
            evidence["character_entities"] = records
        if extra_evidence:
            evidence.update(extra_evidence)
        factory = (
            AttemptOutcome.impossible
            if failed.status == "impossible"
            else AttemptOutcome.failed
        )
        return factory(
            failed.reason,
            evidence=evidence,
            owned_artifacts=artifacts,
            owned_objects=objects,
            owned_datablocks=datablocks,
        )

    for index, layout in enumerate(layouts):
        child = _character_text_item(text_item, layout)
        glyph_id = getattr(layout, "glyph_id", None)
        suffix = f"_c{index:04d}_g{glyph_id if glyph_id is not None else 'na'}"
        if _visible_zero_advance_exact_contour_required(child):
            final, final_data, failure, exact_contour_evidence = (
                _create_exact_embedded_glyph_candidate(
                    child,
                    collection,
                    page_number=page_number,
                    requested=requested,
                    delivered=delivered,
                    item_id=item_id,
                    visual_style=visual_style,
                    z_offset_m=z_offset_m,
                    entity_suffix=suffix,
                    baseline_alignment=baseline_alignment,
                    mesh_factory=mesh_factory,
                )
            )
            if failure is not None:
                return aggregate_failure(failure, index)
            candidates.append({
                "index": index,
                "layout": layout,
                "child": child,
                "source": None,
                "source_data": None,
                "final": final,
                "final_data": final_data,
                "source_verification": None,
                "zero_ink_evidence": None,
                "exact_contour_evidence": exact_contour_evidence,
            })
            continue
        source, source_data, failure = _create_font_candidate(
            child,
            collection,
            page_number=page_number,
            requested=requested,
            delivered=delivered,
            item_id=item_id,
            visual_style=visual_style,
            z_offset_m=z_offset_m,
            entity_suffix=suffix,
            defer_host_update=True,
            baseline_alignment=baseline_alignment,
        )
        if failure is not None:
            return aggregate_failure(failure, index)
        candidates.append({
            "index": index,
            "layout": layout,
            "child": child,
            "source": source,
            "source_data": source_data,
            "final": None,
            "final_data": None,
            "source_verification": None,
            "zero_ink_evidence": None,
            "exact_contour_evidence": None,
        })

    try:
        bpy.context.view_layer.update()
    except Exception as exc:
        failure = AttemptOutcome.failed(
            "positioned_conversion_source_batch_update_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
        )
        return aggregate_failure(failure)

    source_records = []
    for candidate in candidates:
        if candidate["exact_contour_evidence"] is not None:
            exact_outcome = AttemptOutcome.delivered(
                candidate["final"],
                entity_ids=(candidate["final"].name,),
                evidence={
                    "item_id": item_id,
                    **candidate["exact_contour_evidence"],
                },
                owned_artifacts=_owned_artifacts_for_text_entity(
                    candidate["final"], candidate["final_data"]
                ),
                owned_objects=_owned_objects_for_text_entity(candidate["final"]),
                owned_datablocks=(candidate["final_data"],),
            )
            source_records.append(character_record(candidate, exact_outcome))
            continue
        source_outcome = _verify_font_candidate(
            candidate["source"],
            candidate["child"],
            delivered="text",
            item_id=item_id,
        )
        if source_outcome.status != "delivered":
            source_records.append(character_record(candidate, source_outcome))
            return aggregate_failure(
                source_outcome,
                candidate["index"],
                source_records,
            )
        candidate["source_verification"] = dict(source_outcome.evidence or {})
        source_records.append(character_record(candidate, source_outcome))

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception as exc:
        failure = AttemptOutcome.failed(
            "positioned_conversion_dependency_graph_unavailable",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
        )
        return aggregate_failure(failure, records=source_records)

    for candidate in candidates:
        if candidate["exact_contour_evidence"] is not None:
            continue
        final, final_data, failure, zero_ink_evidence = (
            _prepare_positioned_converted_candidate(
                candidate["source"],
                candidate["source_data"],
                candidate["child"],
                collection,
                page_number=page_number,
                requested=requested,
                delivered=delivered,
                item_id=item_id,
                depsgraph=depsgraph,
                source_verification=candidate["source_verification"],
                mesh_factory=mesh_factory,
            )
        )
        candidate["final"] = final
        candidate["final_data"] = final_data
        candidate["zero_ink_evidence"] = zero_ink_evidence
        if failure is not None:
            records = list(source_records)
            records[candidate["index"]] = character_record(candidate, failure)
            return aggregate_failure(failure, candidate["index"], records)

    for candidate in candidates:
        if candidate["exact_contour_evidence"] is not None:
            continue
        if candidate["zero_ink_evidence"] is not None:
            try:
                material_name = str(
                    candidate["source"].get("pdf_text_material", "") or ""
                )
                zero_ink_material = bpy.data.materials.get(material_name)
            except (AttributeError, ReferenceError, TypeError):
                zero_ink_material = None
            if not _valid_owned_ref(zero_ink_material):
                failure = AttemptOutcome.failed(
                    "verified_zero_ink_material_ownership_unavailable",
                    evidence={"item_id": item_id},
                )
                return aggregate_failure(
                    failure,
                    candidate["index"],
                    source_records,
                )
            cleanup_outcome = AttemptOutcome.delivered(
                candidate["source"],
                entity_ids=(candidate["source"].name,),
                evidence=dict(candidate["zero_ink_evidence"]),
                owned_artifacts=(
                    *_owned_artifacts_for_text_entity(
                        candidate["source"], candidate["source_data"]
                    ),
                    _artifact(None, candidate["final_data"]),
                    _artifact(None, zero_ink_material),
                ),
                owned_objects=_owned_objects_for_text_entity(candidate["source"]),
                owned_datablocks=(
                    candidate["source_data"],
                    candidate["final_data"],
                    zero_ink_material,
                ),
            )
            cleanup = _cleanup_attempt(cleanup_outcome, collection)
            if cleanup.get("status") != "complete":
                failure = AttemptOutcome.failed(
                    "verified_zero_ink_candidate_cleanup_failed",
                    evidence={"item_id": item_id, "cleanup": cleanup},
                    owned_artifacts=cleanup_outcome.owned_artifacts,
                    owned_objects=cleanup_outcome.owned_objects,
                    owned_datablocks=cleanup_outcome.owned_datablocks,
                )
                return aggregate_failure(
                    failure,
                    candidate["index"],
                    source_records,
                )
            candidate["source"] = None
            candidate["source_data"] = None
            candidate["final_data"] = None
            candidate["zero_ink_evidence"].update(
                cleanup=cleanup,
                zero_ink_source_font_cleaned=True,
                empty_conversion_datablock_cleaned=True,
            )
            zero_ink_outcome = AttemptOutcome.delivered(
                None,
                evidence=dict(candidate["zero_ink_evidence"]),
            )
            source_records[candidate["index"]] = character_record(
                candidate, zero_ink_outcome
            )
            continue
        cleanup, remaining_objects, remaining_data = _remove_object_and_data(
            candidate["source"], candidate["source_data"], collection
        )
        candidate["source"] = remaining_objects[0] if remaining_objects else None
        candidate["source_data"] = remaining_data[0] if remaining_data else None
        if cleanup.get("status") != "complete":
            failure = AttemptOutcome.failed(
                "superseded_font_candidate_cleanup_failed",
                evidence={"item_id": item_id, "cleanup": cleanup},
            )
            return aggregate_failure(
                failure,
                candidate["index"],
                source_records,
            )

    try:
        bpy.context.view_layer.update()
    except Exception as exc:
        failure = AttemptOutcome.failed(
            "positioned_conversion_final_batch_update_failed_not_impossibility_proof",
            evidence={
                "item_id": item_id,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            },
        )
        return aggregate_failure(failure, records=source_records)

    expected_type = "CURVE" if delivered == "glyphs" else "MESH"
    outcomes = []
    character_evidence = []
    for candidate in candidates:
        if candidate["zero_ink_evidence"] is not None:
            zero_ink_outcome = AttemptOutcome.delivered(
                None,
                evidence=dict(candidate["zero_ink_evidence"]),
            )
            character_evidence.append(character_record(candidate, zero_ink_outcome))
            continue
        verification_failure, verification_evidence = _verify_converted_candidate(
            candidate["final"],
            candidate["final_data"],
            candidate["child"],
            expected_type=expected_type,
            item_id=item_id,
        )
        if verification_failure is not None:
            character_evidence.append(
                character_record(candidate, verification_failure)
            )
            return aggregate_failure(
                verification_failure,
                candidate["index"],
                character_evidence,
            )
        detail_key = "spline_count" if delivered == "glyphs" else "vertex_count"
        detail_values = (
            candidate["final_data"].splines
            if delivered == "glyphs"
            else candidate["final_data"].vertices
        )
        outcome = AttemptOutcome.delivered(
            candidate["final"],
            entity_ids=(candidate["final"].name,),
            evidence={
                "item_id": item_id,
                **dict(candidate["exact_contour_evidence"] or {}),
                **verification_evidence,
                detail_key: len(detail_values),
            },
            owned_artifacts=_owned_artifacts_for_text_entity(
                candidate["final"], candidate["final_data"]
            ),
            owned_objects=_owned_objects_for_text_entity(candidate["final"]),
            owned_datablocks=(candidate["final_data"],),
        )
        outcomes.append(outcome)
        character_evidence.append(character_record(candidate, outcome))
        try:
            outcome.entity["pdf_source_char_index"] = candidate["index"]
            glyph_id = getattr(candidate["layout"], "glyph_id", None)
            outcome.entity["pdf_source_glyph_id"] = (
                int(glyph_id) if glyph_id is not None else -1
            )
        except (AttributeError, ReferenceError, TypeError, ValueError):
            pass

    if not outcomes:
        removed = []
        for character in character_evidence:
            cleanup = character.get("verification", {}).get("cleanup", {})
            for removed_id in cleanup.get("removed", ()):
                value = str(removed_id)
                if value and value not in removed:
                    removed.append(value)
        zero_ink_evidence = {
            **_font_asset_evidence(text_item),
            **_proof_identity(item_id, page_number, int(text_item.id)),
            "proof_kind": "positioned_zero_ink_delivery_v1",
            "logical_delivery_id": f"{item_id}:zero-ink:{delivered}",
            "requested_representation": requested,
            "delivered_representation": delivered,
            "source_manifest_schema": ZERO_INK_SOURCE_MANIFEST_SCHEMA,
            "source_manifest_sha256": source_manifest_sha256,
            "source_text": str(
                source_manifest.get("source_text", text_item.text)
                if isinstance(source_manifest, dict)
                else text_item.text
            ),
            "zero_ink_delivery": True,
            "zero_ink_identity_verified": True,
            "no_visible_ink_expected": True,
            "physical_entity_count": 0,
            "source_character_count": len(candidates),
            "character_count": len(candidates),
            "attempted_character_count": len(candidates),
            "visible_character_count": 0,
            "zero_ink_character_count": len(character_evidence),
            "character_positioning_preserved": True,
            "character_entities": character_evidence,
            "cleanup_verified": True,
            "cleanup": {"status": "complete", "removed": removed},
            "dependency_graph_updates": 2,
        }
        return AttemptOutcome.delivered(
            None,
            entity_ids=(),
            evidence=zero_ink_evidence,
        )

    first = outcomes[0]
    zero_ink_count = sum(
        1
        for candidate in candidates
        if candidate["zero_ink_evidence"] is not None
    )
    physical_entity_count = sum(len(outcome.entity_ids) for outcome in outcomes)
    zero_ink_removed = []
    for character in character_evidence:
        verification = character.get("verification", {})
        if verification.get("zero_ink_identity") is not True:
            continue
        for removed_id in verification.get("cleanup", {}).get("removed", ()):
            value = str(removed_id)
            if value and value not in zero_ink_removed:
                zero_ink_removed.append(value)
    evidence = {
        **_font_asset_evidence(text_item),
        **_proof_identity(item_id, page_number, int(text_item.id)),
        **dict(first.evidence or {}),
        "item_id": item_id,
        "requested_representation": requested,
        "delivered_representation": delivered,
        "source_manifest_schema": ZERO_INK_SOURCE_MANIFEST_SCHEMA,
        "source_manifest_sha256": source_manifest_sha256,
        "source_text": str(
            source_manifest.get("source_text", text_item.text)
            if isinstance(source_manifest, dict)
            else text_item.text
        ),
        "source_character_count": len(candidates),
        "character_positioning_preserved": True,
        "character_count": len(candidates),
        "attempted_character_count": len(candidates),
        "visible_character_count": len(outcomes),
        "zero_ink_character_count": zero_ink_count,
        "physical_entity_count": physical_entity_count,
        "character_entities": character_evidence,
        "cleanup_verified": True,
        "cleanup": {"status": "complete", "removed": zero_ink_removed},
        "dependency_graph_updates": 2,
    }
    return AttemptOutcome.delivered(
        first.entity,
        entity_ids=tuple(
            entity_id for outcome in outcomes for entity_id in outcome.entity_ids
        ),
        evidence=evidence,
        owned_artifacts=tuple(
            artifact for outcome in outcomes for artifact in outcome.owned_artifacts
        ),
        owned_objects=tuple(
            obj for outcome in outcomes for obj in outcome.owned_objects
        ),
        owned_datablocks=tuple(
            data for outcome in outcomes for data in outcome.owned_datablocks
        ),
    )


def _attempt_positioned_characters(
    text_item,
    collection,
    *,
    page_number,
    requested,
    delivered,
    item_id,
    visual_style,
    z_offset_m,
    source_manifest=None,
    source_manifest_sha256="",
    baseline_alignment=None,
):
    layouts = tuple(getattr(text_item, "source_char_layout", ()) or ())
    if not layouts:
        return AttemptOutcome.failed(
            "individual_positioning_requested_without_character_layout",
            evidence={"item_id": item_id},
        )
    reconstructed = "".join(str(layout.text) for layout in layouts)
    if reconstructed != str(text_item.text):
        return AttemptOutcome.failed(
            "character_layout_text_identity_mismatch",
            evidence={
                "item_id": item_id,
                "source_text": str(text_item.text),
                "layout_text": reconstructed,
            },
        )
    if baseline_alignment not in {"BOTTOM_BASELINE", "BOTTOM"}:
        return AttemptOutcome.failed(
            "positioned_baseline_alignment_unavailable_not_impossibility_proof",
            evidence={"item_id": item_id},
        )
    if delivered in {"text", "3d_text"}:
        return _attempt_positioned_native_characters(
            text_item,
            collection,
            page_number=page_number,
            requested=requested,
            delivered=delivered,
            item_id=item_id,
            visual_style=visual_style,
            z_offset_m=z_offset_m,
            baseline_alignment=baseline_alignment,
        )
    if delivered in {"glyphs", "geometry"}:
        return _attempt_positioned_converted_characters(
            text_item,
            collection,
            page_number=page_number,
            requested=requested,
            delivered=delivered,
            item_id=item_id,
            visual_style=visual_style,
            z_offset_m=z_offset_m,
            source_manifest=source_manifest,
            source_manifest_sha256=source_manifest_sha256,
            baseline_alignment=baseline_alignment,
        )

    raise ValueError(f"unsupported positioned representation: {delivered}")


def build_text(
    text_item: NormalizedText,
    collection: bpy.types.Collection,
    page_number: int = 0,
    visual_style: str = "source",
    z_offset_m: float = 0.0,
    strict_text_fidelity: bool = True,
    text_mode: str = "3d_text",
    provenance_opts: Any = None,
    terminal_raster_callback: Optional[Callable] = None,
) -> Optional[bpy.types.Object]:
    if strict_text_fidelity is not True:
        raise ValueError("strict_text_fidelity cannot be disabled")
    requested = normalize_representation(text_mode)
    if str(getattr(text_item, "text", "") or "") == "":
        item_id = f"page:{int(page_number or text_item.page_number or 0)}:text:{int(text_item.id)}"
        record = {
            "item_id": item_id,
            "page": int(page_number or text_item.page_number or 0),
            "source_span_id": int(text_item.id),
            "requested_representation": requested,
            "attempts": [],
            "final_representation": None,
            "status": "failed",
            "fallback_attempted": False,
            "fallback_used": False,
            "entity_ids": [],
            "reason": "empty_source_text",
        }
        _append_delivery_record(provenance_opts, record)
        return None

    effective_page = int(page_number or getattr(text_item, "page_number", 0) or 0)
    item_id = f"page:{effective_page}:text:{int(text_item.id)}"
    positioned_text = bool(
        getattr(text_item, "requires_individual_positioning", False)
    )
    baseline_alignment = None
    if positioned_text:
        if _positioned_item_uses_only_exact_contours(text_item, requested):
            baseline_alignment = "BOTTOM_BASELINE"
        else:
            try:
                baseline_alignment = _probe_positioned_baseline_alignment()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                baseline_alignment = None
    zero_ink_source_manifest = None
    zero_ink_source_manifest_sha256 = ""
    if (
        requested in {"glyphs", "geometry"}
        and positioned_text
    ):
        try:
            raw_manifest = _positioned_zero_ink_source_manifest(
                text_item,
                item_id=item_id,
                page_number=effective_page,
                requested=requested,
                z_offset_m=z_offset_m,
                baseline_alignment=baseline_alignment,
            )
            (
                zero_ink_source_manifest,
                zero_ink_source_manifest_sha256,
            ) = freeze_zero_ink_source_manifest(raw_manifest)
        except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
            zero_ink_source_manifest = None
            zero_ink_source_manifest_sha256 = ""

    def attempt(representation: str) -> AttemptOutcome:
        if representation == "labels":
            return _attempt_labels(item_id, effective_page, int(text_item.id))
        if (
            representation in {"text", "3d_text", "glyphs", "geometry"}
            and positioned_text
        ):
            return _attempt_positioned_characters(
                text_item,
                collection,
                page_number=effective_page,
                requested=requested,
                delivered=representation,
                item_id=item_id,
                visual_style=visual_style,
                z_offset_m=z_offset_m,
                source_manifest=zero_ink_source_manifest,
                source_manifest_sha256=zero_ink_source_manifest_sha256,
                baseline_alignment=baseline_alignment,
            )
        if representation in {"text", "3d_text"}:
            return _attempt_native_font(
                text_item,
                collection,
                page_number=effective_page,
                requested=requested,
                delivered=representation,
                item_id=item_id,
                visual_style=visual_style,
                z_offset_m=z_offset_m,
            )
        if representation == "glyphs":
            return _attempt_glyphs(
                text_item,
                collection,
                page_number=effective_page,
                requested=requested,
                item_id=item_id,
                visual_style=visual_style,
                z_offset_m=z_offset_m,
            )
        if representation == "geometry":
            return _attempt_geometry(
                text_item,
                collection,
                page_number=effective_page,
                requested=requested,
                item_id=item_id,
                visual_style=visual_style,
                z_offset_m=z_offset_m,
            )
        return _attempt_raster(
            text_item,
            collection,
            page_number=effective_page,
            requested=requested,
            item_id=item_id,
            terminal_raster_callback=terminal_raster_callback,
        )

    obj, record = deliver_item(
        item_id=item_id,
        page_number=effective_page,
        source_span_id=int(text_item.id),
        requested=requested,
        expected_zero_ink_manifest=zero_ink_source_manifest,
        attempt=attempt,
        cleanup=lambda outcome: _cleanup_attempt(outcome, collection),
    )
    delivered_outcome = record.pop("_delivered_outcome", None)
    zero_ink_delivery_manifest = record.pop(
        "_zero_ink_delivery_manifest",
        None,
    )
    zero_ink_reconciliation_authority = record.pop(
        "_zero_ink_reconciliation_authority",
        None,
    )
    _append_delivery_record(provenance_opts, record)
    zero_ink_delivery = (
        obj is None
        and record.get("status") == "delivered"
        and record.get("zero_ink_delivery") is True
        and record.get("physical_entity_count") == 0
    )
    logical_zero_ink_children = (
        record.get("status") == "delivered"
        and int(record.get("zero_ink_character_count", 0) or 0) > 0
    )
    if obj is None and not zero_ink_delivery:
        LOGGER.error(
            "Blender text delivery failed for %s requested=%s attempts=%s",
            item_id,
            requested,
            [attempt_record.get("attempted_representation") for attempt_record in record["attempts"]],
        )
        return None

    if provenance_opts is not None and isinstance(delivered_outcome, AttemptOutcome):
        outcomes = getattr(provenance_opts, "_text_delivery_outcomes", None)
        if not isinstance(outcomes, dict):
            outcomes = {}
            provenance_opts._text_delivery_outcomes = outcomes  # noqa: B010
        outcomes[item_id] = delivered_outcome
        if logical_zero_ink_children and isinstance(zero_ink_source_manifest, dict):
            manifests = getattr(
                provenance_opts,
                "_zero_ink_source_manifests",
                None,
            )
            if not isinstance(manifests, dict):
                manifests = {}
                provenance_opts._zero_ink_source_manifests = manifests  # noqa: B010
            manifests[item_id] = zero_ink_source_manifest
            delivery_manifests = getattr(
                provenance_opts,
                "_zero_ink_delivery_manifests",
                None,
            )
            if not isinstance(delivery_manifests, dict):
                delivery_manifests = {}
                provenance_opts._zero_ink_delivery_manifests = (  # noqa: B010
                    delivery_manifests
                )
            delivery_manifests[item_id] = zero_ink_delivery_manifest
            if isinstance(
                zero_ink_reconciliation_authority,
                ZeroInkReconciliationAuthority,
            ):
                authorities = getattr(
                    provenance_opts,
                    "_zero_ink_reconciliation_authorities",
                    (),
                )
                if not isinstance(authorities, tuple):
                    authorities = ()
                provenance_opts._zero_ink_reconciliation_authorities = (  # noqa: B010
                    tuple(
                        authority
                        for authority in authorities
                        if isinstance(authority, ZeroInkReconciliationAuthority)
                        and authority.item_id != item_id
                    )
                    + (zero_ink_reconciliation_authority,)
                )

    delivered = str(record["final_representation"])
    if delivered != requested:
        reason = str(record["attempts"][0].get("reason") or "item_specific_impossibility")
        _record_text_mode_fallback(
            provenance_opts,
            requested=requested,
            delivered=delivered,
            reason=reason,
        )
        try:
            obj["pdf_text_fallback_reason"] = reason
        except Exception:
            pass
    if int(record.get("delivered_count_contribution", 0) or 0) > 0:
        _record_delivered_text_entity(provenance_opts, delivered)
    _record_text_provenance(
        provenance_opts,
        page_number=effective_page,
        text_item=text_item,
        requested_text_mode=requested,
        delivered_text_mode=delivered,
        parent_handle=(
            str(record.get("logical_delivery_id") or "")
            if zero_ink_delivery
            else str(getattr(obj, "name", "") or "")
        ),
        zero_ink_delivery=zero_ink_delivery,
    )
    return obj


def build_all_text(
    text_items: list,
    collection: bpy.types.Collection,
    page_number: int = 0,
    visual_style: str = "source",
    z_offset_m: float = 0.0,
    strict_text_fidelity: bool = True,
    text_mode: str = "3d_text",
    progress_callback=None,
    provenance_opts: Any = None,
    terminal_raster_callback: Optional[Callable] = None,
) -> int:
    count = 0
    total = max(1, len(text_items or []))
    heartbeat_every = max(25, int(total / 25))
    for idx, item in enumerate(text_items or []):
        if progress_callback and idx % heartbeat_every == 0:
            try:
                progress_callback((idx + 1) / float(total))
            except Exception:
                pass
            try:
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
            except Exception:
                pass
        obj = build_text(
            item,
            collection,
            page_number,
            visual_style=visual_style,
            z_offset_m=z_offset_m,
            strict_text_fidelity=strict_text_fidelity,
            text_mode=text_mode,
            provenance_opts=provenance_opts,
            terminal_raster_callback=terminal_raster_callback,
        )
        if obj is not None:
            count += 1
    if progress_callback:
        try:
            progress_callback(1.0)
        except Exception:
            pass
    return count
