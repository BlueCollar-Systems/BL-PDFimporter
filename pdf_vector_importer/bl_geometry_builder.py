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

# Scale factor for PDF line widths -> Blender bevel depth
_LINEWIDTH_SCALE = 0.01

# Number of sample points for arc approximation
_ARC_SAMPLE_COUNT = 32


# ── Material cache ───────────────────────────────────────────────────

def _color_key(color: Optional[Tuple[float, float, float]]) -> str:
    """Create a stable string key from an RGB tuple."""
    if color is None:
        return "0.000_0.000_0.000"
    return f"{color[0]:.3f}_{color[1]:.3f}_{color[2]:.3f}"


def _get_or_create_material(
    color: Optional[Tuple[float, float, float]],
    cache: Dict[str, bpy.types.Material],
) -> bpy.types.Material:
    """Return a shared material for the given RGB color, creating if needed."""
    key = _color_key(color)
    if key in cache:
        return cache[key]

    r, g, b = color if color else (0.0, 0.0, 0.0)
    name = f"PDF_{r:.2f}_{g:.2f}_{b:.2f}"
    mat = bpy.data.materials.new(name=name)
    mat.diffuse_color = (r, g, b, 1.0)
    mat.use_nodes = False
    cache[key] = mat
    return mat


# ── Collection helpers ───────────────────────────────────────────────

def _get_or_create_child_collection(
    parent: bpy.types.Collection,
    name: str,
) -> bpy.types.Collection:
    """Return a child collection by name, creating if it does not exist."""
    for child in parent.children:
        if child.name == name:
            return child
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    return col


def _resolve_collection(
    page_col: bpy.types.Collection,
    prim: Primitive,
    group_by_color: bool,
) -> bpy.types.Collection:
    """
    Determine the target collection for a primitive.

    Priority: OCG layer name > color group > page collection.
    """
    target = page_col

    # OCG / layer sub-collection
    if prim.layer_name:
        target = _get_or_create_child_collection(target, prim.layer_name)

    # Color sub-collection
    if group_by_color and prim.stroke_color:
        r, g, b = prim.stroke_color
        color_name = f"Color_{r:.2f}_{g:.2f}_{b:.2f}"
        target = _get_or_create_child_collection(target, color_name)

    return target


# ── Curve builders ───────────────────────────────────────────────────

def _create_poly_curve(
    name: str,
    points: list,
    closed: bool,
    collection: bpy.types.Collection,
    line_width: Optional[float],
    material: bpy.types.Material,
) -> bpy.types.Object:
    """Create a Curve object with a POLY spline from a list of 2D points."""
    curve_data = bpy.data.curves.new(name=name, type="CURVE")
    curve_data.dimensions = "3D"
    curve_data.resolution_u = 12

    # Line width as bevel depth
    if line_width is not None and line_width > 0:
        curve_data.bevel_depth = line_width * _LINEWIDTH_SCALE
    else:
        curve_data.bevel_depth = 0.002  # minimal visible width

    spline = curve_data.splines.new("POLY")
    spline.points.add(len(points) - 1)  # one point already exists
    for i, (x, y) in enumerate(points):
        spline.points[i].co = (x, y, 0.0, 1.0)
    spline.use_cyclic_u = closed

    # Material
    curve_data.materials.append(material)

    obj = bpy.data.objects.new(name, curve_data)
    collection.objects.link(obj)
    return obj


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
) -> bpy.types.Object:
    """Create a NURBS circle curve object."""
    curve_data = bpy.data.curves.new(name=name, type="CURVE")
    curve_data.dimensions = "3D"

    if line_width is not None and line_width > 0:
        curve_data.bevel_depth = line_width * _LINEWIDTH_SCALE
    else:
        curve_data.bevel_depth = 0.002

    # Blender NURBS circle: 8-point circle approximation
    spline = curve_data.splines.new("NURBS")
    num_pts = 8
    spline.points.add(num_pts - 1)
    cx, cy = center
    for i in range(num_pts):
        angle = 2.0 * math.pi * i / num_pts
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        spline.points[i].co = (x, y, 0.0, 1.0)
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
) -> bpy.types.Object:
    """Create a flat mesh face from a closed polygon of 2D points."""
    mesh = bpy.data.meshes.new(name=name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    verts = [bm.verts.new((x, y, 0.0)) for x, y in points]
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


# ── Main entry point ─────────────────────────────────────────────────

def build_page(
    page_data: PageData,
    collection: bpy.types.Collection,
    config: Optional[dict] = None,
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
    group_by_color = config.get("group_by_color", True)

    material_cache: Dict[str, bpy.types.Material] = {}
    stats = {"curves": 0, "meshes": 0, "circles": 0, "arcs": 0}

    for prim in page_data.primitives:
        target_col = _resolve_collection(collection, prim, group_by_color)
        mat = _get_or_create_material(prim.stroke_color, material_cache)

        obj_name = f"P{page_data.page_number}_{prim.type}_{prim.id}"

        if prim.type == "line":
            _create_poly_curve(
                obj_name, prim.points, False, target_col,
                prim.line_width, mat,
            )
            stats["curves"] += 1

        elif prim.type == "polyline":
            _create_poly_curve(
                obj_name, prim.points, False, target_col,
                prim.line_width, mat,
            )
            stats["curves"] += 1

        elif prim.type == "arc":
            if prim.center and prim.radius and prim.start_angle is not None and prim.end_angle is not None:
                arc_pts = _sample_arc_points(
                    prim.center, prim.radius,
                    prim.start_angle, prim.end_angle,
                    _ARC_SAMPLE_COUNT,
                )
                _create_poly_curve(
                    obj_name, arc_pts, False, target_col,
                    prim.line_width, mat,
                )
                stats["arcs"] += 1
            elif prim.points and len(prim.points) >= 2:
                # Fallback: use polyline points
                _create_poly_curve(
                    obj_name, prim.points, False, target_col,
                    prim.line_width, mat,
                )
                stats["curves"] += 1

        elif prim.type == "circle":
            if prim.center and prim.radius:
                _create_nurbs_circle(
                    obj_name, prim.center, prim.radius,
                    target_col, prim.line_width, mat,
                )
                stats["circles"] += 1
            elif prim.points and len(prim.points) >= 3:
                # Closed polyline fallback
                _create_poly_curve(
                    obj_name, prim.points, True, target_col,
                    prim.line_width, mat,
                )
                stats["curves"] += 1

        elif prim.type in ("closed_loop", "rect"):
            if make_faces and len(prim.points) >= 3:
                # Create both curve outline and mesh face
                _create_poly_curve(
                    obj_name + "_outline", prim.points, True, target_col,
                    prim.line_width, mat,
                )
                _create_face_mesh(
                    obj_name + "_face", prim.points, target_col, mat,
                )
                stats["curves"] += 1
                stats["meshes"] += 1
            else:
                _create_poly_curve(
                    obj_name, prim.points, True, target_col,
                    prim.line_width, mat,
                )
                stats["curves"] += 1

        else:
            # Unknown type — best effort as polyline
            if prim.points and len(prim.points) >= 2:
                _create_poly_curve(
                    obj_name, prim.points, prim.closed, target_col,
                    prim.line_width, mat,
                )
                stats["curves"] += 1

    return stats
