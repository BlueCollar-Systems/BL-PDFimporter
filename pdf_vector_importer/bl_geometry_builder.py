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

# PDF/normalized line width (mm) -> Blender bevel depth (m)
_LINEWIDTH_SCALE = MM_TO_M
_MIN_BEVEL_DEPTH = 0.00035  # 0.35 mm baseline visibility in viewport

# Number of sample points for arc approximation
_ARC_SAMPLE_COUNT = 32
_MIN_DASH_MM = 0.05
_MIN_PATTERN_CYCLE_MM = 0.25
_MAX_DASH_STEPS = 20000
_BACKGROUND_FILL_AREA_RATIO = 0.92


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
    mat.use_nodes = False
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

    # Line width as bevel depth
    if line_width is not None and line_width > 0:
        curve_data.bevel_depth = max(line_width * _LINEWIDTH_SCALE, _MIN_BEVEL_DEPTH)
    else:
        curve_data.bevel_depth = _MIN_BEVEL_DEPTH

    spline = curve_data.splines.new("POLY")
    spline.points.add(len(points_m) - 1)  # one point already exists
    for i, (x, y) in enumerate(points_m):
        spline.points[i].co = (x, y, z_offset_m, 1.0)
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

    if line_width is not None and line_width > 0:
        curve_data.bevel_depth = max(line_width * _LINEWIDTH_SCALE, _MIN_BEVEL_DEPTH)
    else:
        curve_data.bevel_depth = _MIN_BEVEL_DEPTH

    for run in valid_runs:
        pts_m = [(x * MM_TO_M, y * MM_TO_M) for x, y in run]
        spline = curve_data.splines.new("POLY")
        spline.points.add(len(pts_m) - 1)
        for i, (x, y) in enumerate(pts_m):
            spline.points[i].co = (x, y, z_offset_m, 1.0)
        spline.use_cyclic_u = False

    curve_data.materials.append(material)
    obj = bpy.data.objects.new(name, curve_data)
    collection.objects.link(obj)
    return obj


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


def _dash_polyline(points: list, dash_pattern: list) -> list:
    """
    Split a polyline into visible dash runs according to dash_pattern (mm).
    Returns a list of point runs suitable for individual curve objects.
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

    runs = _dash_polyline(points, dash_pattern) if dash_pattern else [points]
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

    if line_width is not None and line_width > 0:
        curve_data.bevel_depth = max(line_width * _LINEWIDTH_SCALE, _MIN_BEVEL_DEPTH)
    else:
        curve_data.bevel_depth = _MIN_BEVEL_DEPTH

    # Blender NURBS circle: 8-point circle approximation
    spline = curve_data.splines.new("NURBS")
    num_pts = 8
    spline.points.add(num_pts - 1)
    cx, cy = center[0] * MM_TO_M, center[1] * MM_TO_M
    r_m = radius * MM_TO_M
    for i in range(num_pts):
        angle = 2.0 * math.pi * i / num_pts
        x = cx + r_m * math.cos(angle)
        y = cy + r_m * math.sin(angle)
        spline.points[i].co = (x, y, z_offset_m, 1.0)
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

    material_cache: Dict[str, bpy.types.Material] = {}
    collection_cache: Dict[Tuple[int, str], bpy.types.Collection] = {}
    stats = {"curves": 0, "meshes": 0, "circles": 0, "arcs": 0, "skipped_fill_only": 0}
    page_area = max(float(page_data.width or 0.0) * float(page_data.height or 0.0), 1e-9)
    prims = page_data.primitives or []
    total_prims = max(1, len(prims))
    heartbeat_every = max(25, int(config.get("geometry_heartbeat_every", 80) or 80))

    for idx, prim in enumerate(prims):
        if progress_callback and (idx % heartbeat_every == 0):
            try:
                progress_callback((idx + 1) / float(total_prims))
            except Exception:
                pass
            # Keep Blender UI responsive during long geometry loops.
            try:
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
            except Exception:
                pass
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
            created = _draw_stroked_polyline(
                obj_name, prim.points, False, target_col,
                prim.line_width, mat,
                dash_pattern=prim.dash_pattern if map_dashes else None,
                z_offset_m=line_z_offset_m,
            )
            stats["curves"] += created

        elif prim.type == "polyline":
            created = _draw_stroked_polyline(
                obj_name, prim.points, False, target_col,
                prim.line_width, mat,
                dash_pattern=prim.dash_pattern if map_dashes else None,
                z_offset_m=line_z_offset_m,
            )
            stats["curves"] += created

        elif prim.type == "arc":
            if prim.center and prim.radius and prim.start_angle is not None and prim.end_angle is not None:
                arc_pts = _sample_arc_points(
                    prim.center, prim.radius,
                    prim.start_angle, prim.end_angle,
                    _ARC_SAMPLE_COUNT,
                )
                _draw_stroked_polyline(
                    obj_name, arc_pts, False, target_col,
                    prim.line_width, mat,
                    dash_pattern=prim.dash_pattern if map_dashes else None,
                    z_offset_m=line_z_offset_m,
                )
                stats["arcs"] += 1
            elif prim.points and len(prim.points) >= 2:
                # Fallback: use polyline points
                created = _draw_stroked_polyline(
                    obj_name, prim.points, False, target_col,
                    prim.line_width, mat,
                    dash_pattern=prim.dash_pattern if map_dashes else None,
                    z_offset_m=line_z_offset_m,
                )
                stats["curves"] += created

        elif prim.type == "circle":
            has_fill = prim.fill_color is not None
            has_stroke = prim.stroke_color is not None
            fill_face_z = line_z_offset_m - max(0.0005, (_MIN_BEVEL_DEPTH * 1.5))

            if prim.center and prim.radius:
                if make_faces and has_fill:
                    fill_mat = _get_or_create_material(
                        prim.fill_color or prim.stroke_color,
                        material_cache,
                        style=visual_style,
                    )
                    _create_face_mesh(
                        obj_name + "_face",
                        _circle_polygon_points(prim.center, prim.radius),
                        target_col,
                        fill_mat,
                        z_offset_m=fill_face_z,
                    )
                    stats["meshes"] += 1

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
            elif create_outline:
                _create_poly_curve(
                    obj_name, prim.points, True, target_col,
                    prim.line_width, mat,
                    z_offset_m=line_z_offset_m,
                )
                stats["curves"] += 1

        else:
            # Unknown type — best effort as polyline
            if prim.points and len(prim.points) >= 2:
                created = _draw_stroked_polyline(
                    obj_name, prim.points, prim.closed, target_col,
                    prim.line_width, mat,
                    dash_pattern=prim.dash_pattern if map_dashes else None,
                    z_offset_m=line_z_offset_m,
                )
                stats["curves"] += created

    if progress_callback:
        try:
            progress_callback(1.0)
        except Exception:
            pass
    return stats
