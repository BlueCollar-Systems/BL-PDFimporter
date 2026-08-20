# -*- coding: utf-8 -*-
# bl_geometry_builder.py — Convert pdfcadcore Primitives to Blender objects
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Builds Blender geometry (Curves, Meshes, Collections, Materials)
from the host-neutral pdfcadcore Primitive/PageData structures.

Coordinate mapping:
  PDF X  -> Blender X
  PDF Y  -> Blender Y  (already flipped by extractor)
  Z      -> 0
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import bpy
import bmesh

from .pdfcadcore.primitives import PageData, Primitive

# mm -> m conversion (Blender world units are meters by default)
MM_TO_M = 0.001
# PDF user-space points -> mm at page scale 1.0. pdfcadcore already converts
# coordinates and line_width with ``MM_PER_PT * scale`` but hands
# ``Primitive.dash_pattern`` / ``dash_phase`` through in raw PDF points (the
# shared core is byte-identical across hosts); this adapter converts them once.
MM_PER_PT = 25.4 / 72.0

# PDF/normalized line width (mm) -> Blender bevel depth (m).
# bevel_depth is a *radius*, so 1 mm line width -> 0.5 mm radius -> correct
# 1 mm displayed width.  Using MM_TO_M directly doubled every lineweight.
_LINEWIDTH_SCALE = MM_TO_M * 0.5
_MIN_BEVEL_DEPTH = 0.0000125  # 0.0125 mm radius -> ~0.025 mm visible hairline
_DEFAULT_HAIRLINE_BEVEL_DEPTH = 0.000025  # PDF zero-width strokes render as ~0.05 mm

# Number of sample points for arc approximation
_ARC_SAMPLE_COUNT = 32
_MIN_DASH_MM = 0.05
_MIN_PATTERN_CYCLE_MM = 0.25
_MAX_DASH_STEPS = 20000
_BACKGROUND_FILL_AREA_RATIO = 0.92


def _line_bevel_depth(line_width: Optional[float]) -> float:
    """Map PDF line width in mm to Blender curve bevel radius in meters."""
    try:
        if line_width is not None and float(line_width) > 0.0:
            return max(float(line_width) * _LINEWIDTH_SCALE, _MIN_BEVEL_DEPTH)
    except (TypeError, ValueError):
        pass
    return _DEFAULT_HAIRLINE_BEVEL_DEPTH


# ── Material cache ───────────────────────────────────────────────────

def _color_key(color: Optional[Tuple[float, float, float]]) -> str:
    """Create a stable string key from an RGB tuple."""
    if color is None:
        return "0.000_0.000_0.000"
    return f"{color[0]:.3f}_{color[1]:.3f}_{color[2]:.3f}"


def _normalize_style(style: str) -> str:
    key = (style or "source").strip().lower()
    if key in {"source", "blueprint", "high_contrast"}:
        return key
    return "source"


def _styled_color(
    color: Optional[Tuple[float, float, float]],
    style: str,
) -> Tuple[float, float, float]:
    """Map source colors into a preview style while preserving readability."""
    style_key = _normalize_style(style)
    base = color if color else (0.0, 0.0, 0.0)
    if style_key == "source":
        return base

    lum = (base[0] * 0.2126) + (base[1] * 0.7152) + (base[2] * 0.0722)
    if style_key == "blueprint":
        # Brighter blueprint palette for dark viewport readability.
        cyan = (0.35, 0.72, 0.96)
        strength = 0.72 + (0.20 * lum)
        return (
            min(1.0, (cyan[0] * strength) + 0.06),
            min(1.0, (cyan[1] * strength) + 0.08),
            min(1.0, (cyan[2] * strength) + 0.08),
        )

    # High contrast mode for dark viewport themes: near-white linework.
    v = max(0.84, min(0.98, 0.98 - (lum * 0.10)))
    return (v, v, v)


def _material_key(color: Optional[Tuple[float, float, float]], style: str) -> str:
    return f"{_normalize_style(style)}:{_color_key(color)}"


def _get_or_create_material(
    color: Optional[Tuple[float, float, float]],
    cache: Dict[str, bpy.types.Material],
    style: str = "source",
) -> bpy.types.Material:
    """Return a shared material for the given RGB color, creating if needed."""
    key = _material_key(color, style)
    if key in cache:
        return cache[key]

    r, g, b = _styled_color(color, style)
    style_key = _normalize_style(style)
    name = f"PDF_{style_key}_{r:.2f}_{g:.2f}_{b:.2f}"
    mat = bpy.data.materials.new(name=name)
    mat.diffuse_color = (r, g, b, 1.0)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    emission = nodes.new(type="ShaderNodeEmission")
    emission.inputs["Color"].default_value = (r, g, b, 1.0)
    emission.inputs["Strength"].default_value = 1.0
    out = nodes.new(type="ShaderNodeOutputMaterial")
    links.new(emission.outputs["Emission"], out.inputs["Surface"])
    cache[key] = mat
    return mat


# ── Collection helpers ───────────────────────────────────────────────

def _get_or_create_child_collection(
    parent: bpy.types.Collection,
    name: str,
    cache: Optional[Dict[Tuple[int, str], bpy.types.Collection]] = None,
) -> bpy.types.Collection:
    """Return a child collection by name, creating if it does not exist."""
    cache_key = (id(parent), name)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    for child in parent.children:
        if child.name == name:
            if cache is not None:
                cache[cache_key] = child
            return child
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    if cache is not None:
        cache[cache_key] = col
    return col


def _resolve_collection(
    page_col: bpy.types.Collection,
    prim: Primitive,
    group_by_color: bool,
    collection_cache: Optional[Dict[Tuple[int, str], bpy.types.Collection]] = None,
) -> bpy.types.Collection:
    """
    Determine the target collection for a primitive.

    Priority: OCG layer name > color group > page collection.
    """
    target = page_col

    # OCG / layer sub-collection
    if prim.layer_name:
        target = _get_or_create_child_collection(target, prim.layer_name, cache=collection_cache)

    # Color sub-collection
    if group_by_color and prim.stroke_color:
        r, g, b = prim.stroke_color
        color_name = f"Color_{r:.2f}_{g:.2f}_{b:.2f}"
        target = _get_or_create_child_collection(target, color_name, cache=collection_cache)

    return target


# ── Curve builders ───────────────────────────────────────────────────

def _write_spline_points(spline, points_m, z_offset_m: float = 0.0) -> None:
    """Store POLY/NURBS coordinates so Blender 3.2 actually keeps them.

    ``spline.points[i].co = (x, y, z, w)`` is a no-op on the default first
    point in Blender 3.2. Every stroke then starts at world origin and Top
    Orthographic at the grid becomes a starburst instead of the sheet.
    ``foreach_set("co", ...)`` writes the runtime array on 3.2 through 5.2.
    """
    count = len(points_m)
    if count < 1:
        return
    existing = len(spline.points)
    if existing < count:
        spline.points.add(count - existing)
    flat = []
    z_value = float(z_offset_m)
    for x_m, y_m in points_m:
        flat.extend((float(x_m), float(y_m), z_value, 1.0))
    writer = getattr(spline.points, "foreach_set", None)
    if callable(writer):
        writer("co", flat)
        return
    for index, (x_m, y_m) in enumerate(points_m):
        co = spline.points[index].co
        try:
            co[0] = float(x_m)
            co[1] = float(y_m)
            co[2] = z_value
            co[3] = 1.0
        except (TypeError, AttributeError, IndexError):
            spline.points[index].co = (float(x_m), float(y_m), z_value, 1.0)


def _create_poly_curve(
    name: str,
    points: list,
    closed: bool,
    collection: bpy.types.Collection,
    line_width: Optional[float],
    material: bpy.types.Material,
    z_offset_m: float = 0.0,
) -> bpy.types.Object:
    """Create a Curve object with a POLY spline from a list of 2D points."""
    points_m = [(x * MM_TO_M, y * MM_TO_M) for x, y in points]

    curve_data = bpy.data.curves.new(name=name, type="CURVE")
    curve_data.dimensions = "3D"
    curve_data.resolution_u = 12

    curve_data.bevel_depth = _line_bevel_depth(line_width)

    spline = curve_data.splines.new("POLY")
    _write_spline_points(spline, points_m, z_offset_m=z_offset_m)
    spline.use_cyclic_u = closed

    # Material
    curve_data.materials.append(material)

    obj = bpy.data.objects.new(name, curve_data)
    collection.objects.link(obj)
    return obj


def _create_multi_poly_curve(
    name: str,
    runs: list,
    collection: bpy.types.Collection,
    line_width: Optional[float],
    material: bpy.types.Material,
    z_offset_m: float = 0.0,
) -> Optional[bpy.types.Object]:
    """
    Create one Curve object containing multiple POLY splines.
    Used for dashed polylines to avoid creating one object per dash segment.
    """
    valid_runs = [run for run in (runs or []) if len(run) >= 2]
    if not valid_runs:
        return None

    curve_data = bpy.data.curves.new(name=name, type="CURVE")
    curve_data.dimensions = "3D"
    curve_data.resolution_u = 12

    curve_data.bevel_depth = _line_bevel_depth(line_width)

    for run in valid_runs:
        pts_m = [(x * MM_TO_M, y * MM_TO_M) for x, y in run]
        spline = curve_data.splines.new("POLY")
        _write_spline_points(spline, pts_m, z_offset_m=z_offset_m)
        spline.use_cyclic_u = False

    curve_data.materials.append(material)
    obj = bpy.data.objects.new(name, curve_data)
    collection.objects.link(obj)
    return obj


def _dash_pattern_to_model_mm(
    dash_pattern,
    dash_phase,
    pt_to_model_mm: float,
) -> Tuple[Optional[list], float]:
    """Convert a pdfcadcore dash array + phase (PDF points) to model mm.

    ``pt_to_model_mm`` is ``MM_PER_PT * page_scale``: the same factor the
    extractor applied to coordinates and ``line_width``. Consuming the raw
    point values as mm drew every pattern 72/25.4 = 2.83x too long (1011:
    31.2 mm centerline pitch for a 31.2 pt pattern; 15 mm bolt centerlines
    solid because the first 'dash' alone was 19.2 mm).
    """
    if not dash_pattern:
        return None, 0.0
    try:
        factor = float(pt_to_model_mm)
    except (TypeError, ValueError):
        factor = MM_PER_PT
    if not math.isfinite(factor) or factor <= 0.0:
        factor = MM_PER_PT
    try:
        pattern = [float(value) * factor for value in dash_pattern]
    except (TypeError, ValueError):
        return None, 0.0
    if not pattern:
        return None, 0.0
    try:
        phase = float(dash_phase or 0.0) * factor
    except (TypeError, ValueError):
        phase = 0.0
    if not math.isfinite(phase) or phase < 0.0:
        phase = 0.0
    return pattern, phase


def _sanitize_dash_pattern(dash_pattern) -> Optional[list]:
    """Normalize a dash pattern list to positive lengths in mm."""
    if not dash_pattern:
        return None
    try:
        vals = [float(v) for v in dash_pattern if float(v) > 0.0]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    # Clamp pathological tiny dash entries that can create huge split loops.
    vals = [max(v, _MIN_DASH_MM) for v in vals]
    # PDF semantics: odd-length dash arrays are repeated.
    if len(vals) % 2 == 1:
        vals = vals * 2
    # If the whole pattern cycle is effectively sub-pixel at CAD scales,
    # treat as solid to avoid runaway splitting with no visual benefit.
    if sum(vals) < _MIN_PATTERN_CYCLE_MM:
        return None
    return vals


def _dash_polyline(points: list, dash_pattern: list, dash_phase: float = 0.0) -> list:
    """
    Split a polyline into visible dash runs according to dash_pattern (mm).
    Returns a list of point runs suitable for individual curve objects.

    *dash_phase* offsets the pattern start (in mm) for visual accuracy with
    PDF dash-phase values.
    """
    if len(points) < 2:
        return []

    pattern = _sanitize_dash_pattern(dash_pattern)
    if not pattern:
        return [points]

    # Preflight safety: estimate step count and bail out to solid when the
    # dash pattern is too dense for practical runtime.
    eps = 1e-9
    min_dash = max(min(pattern), eps)
    est_steps = 0.0
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        est_steps += math.hypot(x1 - x0, y1 - y0) / min_dash
        if est_steps > _MAX_DASH_STEPS:
            return [points]

    runs = []
    current_run = []
    pattern_index = 0
    pattern_pos = 0.0
    draw_on = True

    # Apply dash phase offset: advance through the pattern by dash_phase mm
    # so the first visible dash starts at the correct offset.
    if dash_phase and dash_phase > 0.0:
        phase_remaining = float(dash_phase)
        cycle_len = sum(pattern)
        if cycle_len > 0:
            phase_remaining = phase_remaining % cycle_len
        while phase_remaining > eps:
            seg_remain = pattern[pattern_index] - pattern_pos
            if phase_remaining >= seg_remain:
                phase_remaining -= seg_remain
                pattern_pos = 0.0
                draw_on = not draw_on
                pattern_index = (pattern_index + 1) % len(pattern)
            else:
                pattern_pos += phase_remaining
                phase_remaining = 0.0
    step_counter = 0

    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        dx = x1 - x0
        dy = y1 - y0
        seg_len = math.hypot(dx, dy)
        if seg_len <= eps:
            continue

        ux = dx / seg_len
        uy = dy / seg_len
        remaining = seg_len
        cx = x0
        cy = y0

        while remaining > eps:
            step_counter += 1
            if step_counter > _MAX_DASH_STEPS:
                # Safety fallback: preserve geometry as solid instead of hanging.
                return [points]
            pat_len = max(pattern[pattern_index] - pattern_pos, eps)
            step = min(remaining, pat_len)
            nx = cx + ux * step
            ny = cy + uy * step

            if draw_on:
                if not current_run:
                    current_run.append((cx, cy))
                if (not current_run) or math.hypot(nx - current_run[-1][0], ny - current_run[-1][1]) > eps:
                    current_run.append((nx, ny))
            else:
                if len(current_run) >= 2:
                    runs.append(current_run)
                current_run = []

            remaining -= step
            cx, cy = nx, ny
            pattern_pos += step

            if pattern_pos >= pattern[pattern_index] - eps:
                pattern_index = (pattern_index + 1) % len(pattern)
                pattern_pos = 0.0
                draw_on = not draw_on

    if len(current_run) >= 2:
        runs.append(current_run)

    return runs


def _draw_stroked_polyline(
    name: str,
    points: list,
    closed: bool,
    collection: bpy.types.Collection,
    line_width: Optional[float],
    material: bpy.types.Material,
    dash_pattern=None,
    dash_phase: float = 0.0,
    z_offset_m: float = 0.0,
) -> int:
    """
    Draw a solid or dashed polyline and return number of curve objects created.
    """
    if not points or len(points) < 2:
        return 0

    if closed:
        _create_poly_curve(
            name,
            points,
            True,
            collection,
            line_width,
            material,
            z_offset_m=z_offset_m,
        )
        return 1

    runs = _dash_polyline(points, dash_pattern, dash_phase=dash_phase) if dash_pattern else [points]
    if not runs:
        return 0
    valid_runs = [r for r in runs if len(r) >= 2]
    if not valid_runs:
        return 0
    if len(valid_runs) == 1:
        _create_poly_curve(
            name,
            valid_runs[0],
            False,
            collection,
            line_width,
            material,
            z_offset_m=z_offset_m,
        )
        return 1

    created = _create_multi_poly_curve(
        name,
        valid_runs,
        collection,
        line_width,
        material,
        z_offset_m=z_offset_m,
    )
    return 1 if created is not None else 0


def _batch_width_key(line_width: Optional[float]):
    """Stable grouping key for paper-space line widths."""
    if line_width is None:
        return None
    try:
        return round(float(line_width), 6)
    except (TypeError, ValueError):
        return None


def _sample_arc_points(
    center: Tuple[float, float],
    radius: float,
    start_angle: float,
    end_angle: float,
    num_points: int = _ARC_SAMPLE_COUNT,
) -> list:
    """Sample points along an arc defined by center, radius, and angle range."""
    cx, cy = center
    # Normalize angle sweep
    sweep = end_angle - start_angle
    if sweep <= 0:
        sweep += 2.0 * math.pi

    pts = []
    for i in range(num_points + 1):
        t = i / float(num_points)
        angle = start_angle + t * sweep
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        pts.append((x, y))
    return pts


def _create_nurbs_circle(
    name: str,
    center: Tuple[float, float],
    radius: float,
    collection: bpy.types.Collection,
    line_width: Optional[float],
    material: bpy.types.Material,
    z_offset_m: float = 0.0,
) -> bpy.types.Object:
    """Create a NURBS circle curve object."""
    curve_data = bpy.data.curves.new(name=name, type="CURVE")
    curve_data.dimensions = "3D"

    curve_data.bevel_depth = _line_bevel_depth(line_width)

    # Blender NURBS circle: 8-point circle approximation
    spline = curve_data.splines.new("NURBS")
    num_pts = 8
    cx, cy = center[0] * MM_TO_M, center[1] * MM_TO_M
    r_m = radius * MM_TO_M
    circle_pts = []
    for i in range(num_pts):
        angle = 2.0 * math.pi * i / num_pts
        circle_pts.append(
            (cx + r_m * math.cos(angle), cy + r_m * math.sin(angle))
        )
    _write_spline_points(spline, circle_pts, z_offset_m=z_offset_m)
    spline.use_cyclic_u = True
    spline.order_u = 3

    curve_data.materials.append(material)

    obj = bpy.data.objects.new(name, curve_data)
    collection.objects.link(obj)
    return obj


# ── Mesh face builder ────────────────────────────────────────────────

def _create_face_mesh(
    name: str,
    points: list,
    collection: bpy.types.Collection,
    material: bpy.types.Material,
    z_offset_m: float = 0.0,
) -> bpy.types.Object:
    """Create a flat mesh face from a closed polygon of 2D points."""
    mesh = bpy.data.meshes.new(name=name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    verts = [bm.verts.new((x * MM_TO_M, y * MM_TO_M, z_offset_m)) for x, y in points]
    bm.verts.ensure_lookup_table()

    if len(verts) >= 3:
        try:
            bm.faces.new(verts)
        except ValueError:
            # Degenerate face — skip silently
            pass

    bm.to_mesh(mesh)
    bm.free()

    mesh.materials.append(material)
    return obj


def _polygon_area(points: list) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        area += (x0 * y1) - (x1 * y0)
    return abs(area) * 0.5


def _dedupe_closed_points(points: list) -> list:
    pts = list(points or [])
    if len(pts) >= 2:
        first = pts[0]
        last = pts[-1]
        try:
            if abs(first[0] - last[0]) <= 1e-9 and abs(first[1] - last[1]) <= 1e-9:
                pts = pts[:-1]
        except Exception:
            pass
    return pts


def _create_extruded_mesh(
    name: str,
    points: list,
    collection: bpy.types.Collection,
    material: bpy.types.Material,
    depth_m: float,
    z_offset_m: float = 0.0,
) -> Optional[bpy.types.Object]:
    """Create a simple prism mesh from a closed 2D polygon."""
    pts = _dedupe_closed_points(points)
    if len(pts) < 3 or depth_m <= 0.0:
        return None

    mesh = bpy.data.meshes.new(name=name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    bottom = [bm.verts.new((x * MM_TO_M, y * MM_TO_M, z_offset_m)) for x, y in pts]
    top = [bm.verts.new((x * MM_TO_M, y * MM_TO_M, z_offset_m + depth_m)) for x, y in pts]
    bm.verts.ensure_lookup_table()

    try:
        bm.faces.new(bottom)
    except ValueError:
        pass
    try:
        bm.faces.new(list(reversed(top)))
    except ValueError:
        pass
    for i in range(len(pts)):
        j = (i + 1) % len(pts)
        try:
            bm.faces.new((bottom[i], bottom[j], top[j], top[i]))
        except ValueError:
            pass

    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(material)
    return obj


def _primitive_area_ratio(prim: Primitive, page_area: float) -> float:
    """Best-effort area ratio of a primitive relative to the page area."""
    if page_area <= 1e-9:
        return 0.0
    try:
        if prim.area is not None:
            a = abs(float(prim.area))
            if a > 0.0 and math.isfinite(a):
                return a / page_area
    except Exception:
        pass

    pts = prim.points or []
    if len(pts) >= 3:
        try:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            a = abs((max(xs) - min(xs)) * (max(ys) - min(ys)))
            if a > 0.0 and math.isfinite(a):
                return a / page_area
        except Exception:
            pass
    return 0.0


def _model3d_mode(config: dict) -> str:
    mode = str(config.get("model3d_mode", "off") or "off").strip().lower()
    if mode in {"yes", "true", "on", "auto_if_evidence"}:
        return "auto"
    if mode in {"closed", "closed_shapes", "extrude_closed_shapes", "force"}:
        return "extrude"
    return mode if mode in {"off", "auto", "extrude"} else "off"


def _model3d_primitive_area(prim: Primitive, points: Optional[list] = None) -> float:
    try:
        if prim.area is not None and abs(float(prim.area)) > 0.0:
            return abs(float(prim.area))
    except Exception:
        pass
    if prim.type == "circle" and prim.radius:
        try:
            return math.pi * float(prim.radius) * float(prim.radius)
        except Exception:
            return 0.0
    return _polygon_area(points or prim.points or [])


def _model3d_should_extrude(
    prim: Primitive,
    page_area: float,
    has_fill: bool,
    config: dict,
    points: Optional[list] = None,
) -> bool:
    mode = _model3d_mode(config)
    if mode == "off":
        return False
    try:
        depth_m = float(config.get("model3d_depth_m", 0.0) or 0.0)
    except (TypeError, ValueError):
        depth_m = 0.0
    if depth_m <= 0.0:
        return False
    area = _model3d_primitive_area(prim, points)
    if area <= 1e-6:
        return False
    if page_area > 1e-9 and area / page_area >= 0.80:
        return False
    if mode == "auto":
        return bool(config.get("model3d_intent_feasible")) and has_fill
    return True


def _is_background_fill_primitive(prim: Primitive, page_area: float) -> bool:
    """
    Identify giant page-cover fills that should be skipped in fill-only mode.
    We intentionally keep smaller fill-only details (e.g., hole markers/icons).
    """
    if prim.type not in ("rect", "closed_loop"):
        return False
    return _primitive_area_ratio(prim, page_area) >= _BACKGROUND_FILL_AREA_RATIO


def _circle_polygon_points(
    center: Tuple[float, float],
    radius: float,
    segments: int = 48,
) -> list:
    """Approximate a circle with polygon points for mesh-face fill rendering."""
    segs = max(12, int(segments))
    cx, cy = center
    pts = []
    for i in range(segs):
        angle = 2.0 * math.pi * i / segs
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return pts


# ── Main entry point ─────────────────────────────────────────────────

def build_page(
    page_data: PageData,
    collection: bpy.types.Collection,
    config: Optional[dict] = None,
    progress_callback=None,
) -> dict:
    """
    Build Blender geometry from a PageData into the given collection.

    Args:
        page_data: Normalized page data from pdfcadcore extraction.
        collection: Target Blender collection for this page's objects.
        config: Import configuration dict with keys like
                'make_faces', 'group_by_color', 'detect_arcs'.

    Returns:
        Stats dict with counts of created objects.
    """
    if config is None:
        config = {}

    make_faces = config.get("make_faces", True)
    ignore_fill_only_shapes = bool(config.get("ignore_fill_only_shapes", True))
    group_by_color = config.get("group_by_color", True)
    map_dashes = config.get("map_dashes", True)
    visual_style = _normalize_style(config.get("visual_style", "source"))
    line_z_offset_m = float(config.get("line_z_offset_m", 0.0) or 0.0)
    # Points -> model mm for dash arrays; the engine passes MM_PER_PT *
    # user_scale (what the extractor used for coordinates and line_width).
    pt_to_model_mm = config.get("pt_to_model_mm", MM_PER_PT)

    def _prim_dashes(prim: Primitive) -> Tuple[Optional[list], float]:
        """Dash array/phase for a primitive in model mm (None when unmapped)."""
        if not map_dashes:
            return None, 0.0
        return _dash_pattern_to_model_mm(
            prim.dash_pattern, prim.dash_phase, pt_to_model_mm
        )

    material_cache: Dict[str, bpy.types.Material] = {}
    collection_cache: Dict[Tuple[int, str], bpy.types.Collection] = {}
    stats = {
        "curves": 0,
        "meshes": 0,
        "circles": 0,
        "arcs": 0,
        "skipped_fill_only": 0,
        "batched_curve_primitives": 0,
        "batched_curve_runs": 0,
        "batched_curve_objects": 0,
        "model3d_solids": 0,
        "geometry_delivery_issues": [],
    }
    page_area = max(float(page_data.width or 0.0) * float(page_data.height or 0.0), 1e-9)
    prims = page_data.primitives or []
    total_prims = max(1, len(prims))
    from .import_session import cancel_heartbeat_interval

    configured_gap = max(1, int(config.get("geometry_heartbeat_every", 25) or 25))
    heartbeat_every = cancel_heartbeat_interval(
        total_prims,
        maximum_gap=min(25, configured_gap),
    )
    batch_open_curves = bool(config.get("batch_open_curves", True))
    open_curve_batches: Dict[Tuple[int, int, object, float], dict] = {}

    def _queue_open_curve(
        name: str,
        points: list,
        target_col: bpy.types.Collection,
        line_width: Optional[float],
        material: bpy.types.Material,
        dash_pattern=None,
        dash_phase: float = 0.0,
    ) -> int:
        """Queue compatible open strokes into fewer Blender curve objects."""
        if not points or len(points) < 2:
            return 0
        if not batch_open_curves:
            return _draw_stroked_polyline(
                name,
                points,
                False,
                target_col,
                line_width,
                material,
                dash_pattern=dash_pattern,
                dash_phase=dash_phase,
                z_offset_m=line_z_offset_m,
            )

        runs = _dash_polyline(points, dash_pattern, dash_phase=dash_phase) if dash_pattern else [points]
        valid_runs = [run for run in runs if len(run) >= 2]
        if not valid_runs:
            return 0

        key = (
            id(target_col),
            id(material),
            _batch_width_key(line_width),
            round(line_z_offset_m, 9),
        )
        batch = open_curve_batches.get(key)
        if batch is None:
            batch = {
                "name": name,
                "runs": [],
                "collection": target_col,
                "line_width": line_width,
                "material": material,
            }
            open_curve_batches[key] = batch
        batch["runs"].extend(valid_runs)
        stats["batched_curve_primitives"] += 1
        stats["batched_curve_runs"] += len(valid_runs)
        return 0

    def _flush_open_curve_batches() -> int:
        created = 0
        for index, batch in enumerate(open_curve_batches.values(), start=1):
            obj_name = f"P{page_data.page_number}_batch_{index:03d}"
            obj = _create_multi_poly_curve(
                obj_name,
                batch["runs"],
                batch["collection"],
                batch["line_width"],
                batch["material"],
                z_offset_m=line_z_offset_m,
            )
            if obj is not None:
                created += 1
        stats["batched_curve_objects"] = created
        stats["curves"] += created
        open_curve_batches.clear()
        return created

    for idx, prim in enumerate(prims):
        if progress_callback and (idx % heartbeat_every == 0):
            progress_result = None
            try:
                progress_result = progress_callback((idx + 1) / float(total_prims))
            except Exception:
                pass
            if progress_result is False:
                from .import_session import ImportCancelledError

                raise ImportCancelledError("PDF import cancelled during geometry building")
        has_fill_any = prim.fill_color is not None
        has_stroke_any = prim.stroke_color is not None
        if (
            ignore_fill_only_shapes
            and has_fill_any
            and not has_stroke_any
            and _is_background_fill_primitive(prim, page_area)
        ):
            stats["skipped_fill_only"] += 1
            continue

        target_col = _resolve_collection(
            collection,
            prim,
            group_by_color,
            collection_cache=collection_cache,
        )
        mat = _get_or_create_material(
            prim.stroke_color or prim.fill_color,
            material_cache,
            style=visual_style,
        )

        obj_name = f"P{page_data.page_number}_{prim.type}_{prim.id}"

        if prim.type == "line":
            dash_pattern_mm, dash_phase_mm = _prim_dashes(prim)
            created = _queue_open_curve(
                obj_name, prim.points, target_col,
                prim.line_width, mat,
                dash_pattern=dash_pattern_mm,
                dash_phase=dash_phase_mm,
            )
            stats["curves"] += created

        elif prim.type == "polyline":
            dash_pattern_mm, dash_phase_mm = _prim_dashes(prim)
            created = _queue_open_curve(
                obj_name, prim.points, target_col,
                prim.line_width, mat,
                dash_pattern=dash_pattern_mm,
                dash_phase=dash_phase_mm,
            )
            stats["curves"] += created

        elif prim.type == "arc":
            dash_pattern_mm, _dash_phase_mm = _prim_dashes(prim)
            if prim.center and prim.radius and prim.start_angle is not None and prim.end_angle is not None:
                arc_pts = _sample_arc_points(
                    prim.center, prim.radius,
                    prim.start_angle, prim.end_angle,
                    _ARC_SAMPLE_COUNT,
                )
                created = _queue_open_curve(
                    obj_name, arc_pts, target_col,
                    prim.line_width, mat,
                    dash_pattern=dash_pattern_mm,
                )
                stats["curves"] += created
                stats["arcs"] += 1
            elif prim.points and len(prim.points) >= 2:
                # Fallback: use polyline points
                created = _queue_open_curve(
                    obj_name, prim.points, target_col,
                    prim.line_width, mat,
                    dash_pattern=dash_pattern_mm,
                )
                stats["curves"] += created

        elif prim.type == "circle":
            has_fill = prim.fill_color is not None
            has_stroke = prim.stroke_color is not None
            fill_face_z = line_z_offset_m - max(0.0005, (_MIN_BEVEL_DEPTH * 1.5))

            if prim.center and prim.radius:
                circle_points = _circle_polygon_points(prim.center, prim.radius)
                if make_faces and has_fill:
                    fill_mat = _get_or_create_material(
                        prim.fill_color or prim.stroke_color,
                        material_cache,
                        style=visual_style,
                    )
                    _create_face_mesh(
                        obj_name + "_face",
                        circle_points,
                        target_col,
                        fill_mat,
                        z_offset_m=fill_face_z,
                    )
                    stats["meshes"] += 1
                if _model3d_should_extrude(prim, page_area, has_fill, config, circle_points):
                    solid_mat = _get_or_create_material(
                        prim.fill_color or prim.stroke_color,
                        material_cache,
                        style=visual_style,
                    )
                    if _create_extruded_mesh(
                        obj_name + "_solid",
                        circle_points,
                        target_col,
                        solid_mat,
                        float(config.get("model3d_depth_m", 0.0) or 0.0),
                        z_offset_m=fill_face_z,
                    ):
                        stats["meshes"] += 1
                        stats["model3d_solids"] += 1

                if has_stroke or not has_fill:
                    _create_nurbs_circle(
                        obj_name,
                        prim.center,
                        prim.radius,
                        target_col,
                        prim.line_width,
                        mat,
                        z_offset_m=line_z_offset_m,
                    )
                    stats["circles"] += 1
            elif prim.points and len(prim.points) >= 3:
                if make_faces and has_fill:
                    fill_mat = _get_or_create_material(
                        prim.fill_color or prim.stroke_color,
                        material_cache,
                        style=visual_style,
                    )
                    _create_face_mesh(
                        obj_name + "_face",
                        prim.points,
                        target_col,
                        fill_mat,
                        z_offset_m=fill_face_z,
                    )
                    stats["meshes"] += 1
                if _model3d_should_extrude(prim, page_area, has_fill, config, prim.points):
                    solid_mat = _get_or_create_material(
                        prim.fill_color or prim.stroke_color,
                        material_cache,
                        style=visual_style,
                    )
                    if _create_extruded_mesh(
                        obj_name + "_solid",
                        prim.points,
                        target_col,
                        solid_mat,
                        float(config.get("model3d_depth_m", 0.0) or 0.0),
                        z_offset_m=fill_face_z,
                    ):
                        stats["meshes"] += 1
                        stats["model3d_solids"] += 1
                if has_stroke or not has_fill:
                    # Closed polyline fallback
                    _create_poly_curve(
                        obj_name,
                        prim.points,
                        True,
                        target_col,
                        prim.line_width,
                        mat,
                        z_offset_m=line_z_offset_m,
                    )
                    stats["curves"] += 1

        elif prim.type in ("closed_loop", "rect"):
            has_fill = prim.fill_color is not None
            has_stroke = prim.stroke_color is not None
            prim_area = abs(float(prim.area or 0.0))
            area_ratio = (prim_area / page_area) if page_area > 0.0 else 0.0
            # Giant page-sized fills (often paper background rectangles) can
            # visually blank the viewport and hide imported vectors.
            is_giant_page_fill = area_ratio >= 0.92
            create_face = bool(make_faces and has_fill and len(prim.points) >= 3 and not is_giant_page_fill)
            create_outline = bool(len(prim.points) >= 2 and has_stroke)
            # Keep fills slightly below linework to avoid z-fighting.
            face_z = line_z_offset_m - max(0.0005, (_MIN_BEVEL_DEPTH * 1.5))

            if create_face:
                face_mat = _get_or_create_material(
                    prim.fill_color or prim.stroke_color,
                    material_cache,
                    style=visual_style,
                )
                if create_outline:
                    _create_poly_curve(
                        obj_name + "_outline", prim.points, True, target_col,
                        prim.line_width, mat,
                        z_offset_m=line_z_offset_m,
                    )
                _create_face_mesh(
                    obj_name + "_face", prim.points, target_col, face_mat, z_offset_m=face_z,
                )
                if create_outline:
                    stats["curves"] += 1
                stats["meshes"] += 1
                if _model3d_should_extrude(prim, page_area, has_fill, config, prim.points):
                    if _create_extruded_mesh(
                        obj_name + "_solid",
                        prim.points,
                        target_col,
                        face_mat,
                        float(config.get("model3d_depth_m", 0.0) or 0.0),
                        z_offset_m=face_z,
                    ):
                        stats["meshes"] += 1
                        stats["model3d_solids"] += 1
            elif create_outline:
                _create_poly_curve(
                    obj_name, prim.points, True, target_col,
                    prim.line_width, mat,
                    z_offset_m=line_z_offset_m,
                )
                stats["curves"] += 1
                if _model3d_should_extrude(prim, page_area, has_fill, config, prim.points):
                    if _create_extruded_mesh(
                        obj_name + "_solid",
                        prim.points,
                        target_col,
                        mat,
                        float(config.get("model3d_depth_m", 0.0) or 0.0),
                        z_offset_m=face_z,
                    ):
                        stats["meshes"] += 1
                        stats["model3d_solids"] += 1

        else:
            # Preserve the source point path as geometry, but never hide that
            # the normalized primitive kind was not understood.  Unknown open
            # paths are built immediately (instead of batched) so delivery can
            # be verified for this exact primitive before it is reported.
            issue = {
                "page": int(page_data.page_number),
                "primitive_id": int(prim.id),
                "requested_type": "geometry",
                "source_primitive_type": str(prim.type or ""),
                "delivered_type": None,
                "status": "failed",
                "reason": "unknown_normalized_primitive_type",
                "verification": "not_created",
            }
            if prim.points and len(prim.points) >= 2:
                dash_pattern_mm, dash_phase_mm = _prim_dashes(prim)
                created = _draw_stroked_polyline(
                    obj_name, prim.points, bool(prim.closed), target_col,
                    prim.line_width, mat,
                    dash_pattern=dash_pattern_mm,
                    dash_phase=dash_phase_mm,
                    z_offset_m=line_z_offset_m,
                )
                stats["curves"] += created
                if created > 0:
                    issue.update({
                        "delivered_type": "polyline_geometry",
                        "status": "verified",
                        "verification": "source_points_preserved",
                    })
            else:
                issue["reason"] = "unknown_primitive_without_polyline_points"
            stats["geometry_delivery_issues"].append(issue)

    _flush_open_curve_batches()

    if progress_callback:
        progress_result = None
        try:
            progress_result = progress_callback(1.0)
        except Exception:
            pass
        if progress_result is False:
            from .import_session import ImportCancelledError

            raise ImportCancelledError("PDF import cancelled after geometry building")
    return stats
