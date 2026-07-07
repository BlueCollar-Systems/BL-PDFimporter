# -*- coding: utf-8 -*-
# bl_text_builder.py — Text rendering for Blender
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Creates Blender Text (font) curve objects from pdfcadcore NormalizedText items.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, Optional, Tuple

import bpy

from .pdfcadcore.primitives import NormalizedText
from .pdfcadcore.text_scale import calibrate_text_size_to_bbox, normalize_text_angle_deg

# PDF font sizes are in mm from the extractor. Blender text size is in
# Blender units (meters by default), so convert mm -> m.
MM_TO_M = 0.001
_FONT_SIZE_SCALE = 1.0
_FONT_CACHE: Optional[bpy.types.VectorFont] = None
_TEXT_MODES = {"labels", "3d_text", "glyphs", "geometry"}


def _normalize_style(style: str) -> str:
    key = (style or "source").strip().lower()
    if key in {"source", "blueprint", "high_contrast"}:
        return key
    return "source"


def _normalize_text_mode(text_mode: str) -> str:
    mode = (text_mode or "3d_text").strip().lower()
    if mode in _TEXT_MODES:
        return mode
    return "3d_text"


def _text_extrusion_depth(font_size: float) -> float:
    return max(font_size * 0.12, 0.00025)


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
        # Brighter blueprint ink tone for dark viewport readability.
        return (0.36, 0.74, 0.98)
    if style_key == "high_contrast":
        return (0.95, 0.95, 0.95)
    # "source" — use the actual PDF text color when available.
    if source_color is not None:
        return source_color
    return (0.06, 0.06, 0.06)


def _should_center_anchor(
    text_item: NormalizedText,
    *,
    strict_text_fidelity: bool = True,
) -> bool:
    if text_item.bbox is None:
        return False
    if strict_text_fidelity:
        # Strict mode should preserve source insertion/baseline anchors.
        return False
    tags = set(text_item.generic_tags or [])
    if "dimension_like" in tags:
        return True
    if "detail_reference" in tags:
        return True
    return False


def _get_or_create_text_material(
    style: str,
    source_color: Optional[Tuple[float, float, float]] = None,
) -> bpy.types.Material:
    style_key = _normalize_style(style)
    r, g, b = _styled_text_color(style_key, source_color=source_color)

    # For source style with unique colors, create per-color materials.
    if style_key == "source" and source_color is not None:
        ri, gi, bi = round(r * 255), round(g * 255), round(b * 255)
        mat_name = f"PDF_Text_{ri:02X}{gi:02X}{bi:02X}"
    else:
        mat_name = f"PDF_Text_{style_key}"

    existing = bpy.data.materials.get(mat_name)
    if existing is not None:
        return existing

    mat = bpy.data.materials.new(name=mat_name)
    mat.diffuse_color = (r, g, b, 1.0)
    mat.use_nodes = False
    return mat


def _get_preferred_font() -> Optional[bpy.types.VectorFont]:
    """Load a readable default font with distinct numeric glyphs."""
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE

    candidates = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates.extend(
        [
            os.path.join(windir, "Fonts", "arial.ttf"),
            os.path.join(windir, "Fonts", "segoeui.ttf"),
            os.path.join(windir, "Fonts", "calibri.ttf"),
            os.path.join(windir, "Fonts", "consola.ttf"),
        ]
    )

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            _FONT_CACHE = bpy.data.fonts.load(path)
            return _FONT_CACHE
        except Exception:
            continue

    try:
        _FONT_CACHE = bpy.data.fonts.get("Bfont")
    except Exception:
        _FONT_CACHE = None
    return _FONT_CACHE


def _fit_text_to_bbox(obj: bpy.types.Object, text_item: NormalizedText) -> None:
    """Scale text object to extracted bbox to preserve alignment and readability."""
    if text_item.bbox is None:
        return

    bx0, by0, bx1, by1 = text_item.bbox
    axis_w = max((bx1 - bx0) * MM_TO_M, 0.0)
    axis_h = max((by1 - by0) * MM_TO_M, 0.0)
    if axis_w <= 1e-9 or axis_h <= 1e-9:
        return

    norm = abs(normalize_text_angle_deg(float(text_item.rotation or 0.0)))
    if norm >= 45.0:
        target_w = max(axis_w, axis_h)
        target_h = min(axis_w, axis_h)
    else:
        target_w = axis_w
        target_h = axis_h

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    current_w = max(float(obj.dimensions.x), 1e-9)
    current_h = max(float(obj.dimensions.y), 1e-9)
    scale_w = target_w / current_w
    scale_h = target_h / current_h
    if not math.isfinite(scale_w) or not math.isfinite(scale_h):
        return

    # Use uniform scale to avoid glyph deformation (e.g. "15/16" -> "15/6").
    s = min(scale_h * 1.05, scale_w * 1.08)
    s = max(0.10, min(10.0, s))
    obj.scale.x *= s
    obj.scale.y *= s


def _text_span_dict(text_item: NormalizedText) -> Dict[str, Any]:
    bbox = getattr(text_item, "bbox", None)
    insertion = getattr(text_item, "insertion", (0.0, 0.0)) or (0.0, 0.0)
    return {
        "text": str(getattr(text_item, "text", "") or ""),
        "bbox": list(bbox) if bbox else None,
        "origin": [float(insertion[0]), float(insertion[1])],
        "size": float(getattr(text_item, "font_size", 0.0) or 0.0),
    }


def _provenance_entity_type(text_mode: str) -> str:
    mode = _normalize_text_mode(text_mode)
    if mode in {"glyphs", "geometry"}:
        return "outline_curve_or_mesh"
    return "native_3d_text"


def _record_text_provenance(
    provenance_opts: Any,
    *,
    page_number: int,
    text_item: NormalizedText,
    text_mode: str,
    parent_handle: str = "",
) -> None:
    if provenance_opts is None:
        return
    try:
        from .pdfcadcore.source_provenance import record_text_span_provenance

        record_text_span_provenance(
            provenance_opts,
            page=int(page_number),
            span=_text_span_dict(text_item),
            text=str(text_item.text or ""),
            created_entity_type=_provenance_entity_type(text_mode),
            parent_handle=str(parent_handle or ""),
            import_mode=str(getattr(provenance_opts, "import_mode", "") or ""),
            text_mode=str(text_mode or ""),
        )
    except (ImportError, TypeError, ValueError):
        pass


def build_text(
    text_item: NormalizedText,
    collection: bpy.types.Collection,
    page_number: int = 0,
    visual_style: str = "source",
    z_offset_m: float = 0.0,
    strict_text_fidelity: bool = True,
    text_mode: str = "3d_text",
    provenance_opts: Any = None,
) -> Optional[bpy.types.Object]:
    """
    Create a Blender Text curve object from a NormalizedText item.

    Args:
        text_item: Normalized text data from pdfcadcore extraction.
        collection: Target Blender collection.
        page_number: Page number for naming.

    Returns:
        The created Blender object, or None if text is empty.
    """
    if not text_item.text or not text_item.text.strip():
        return None

    mode = _normalize_text_mode(text_mode)
    obj_name = f"P{page_number}_text_{mode}_{text_item.id}"

    # Create font curve data
    font_data = bpy.data.curves.new(name=obj_name, type="FONT")
    font_data.body = text_item.text
    calibrated_size_mm = _calibrated_text_size_mm(text_item)
    font_data.size = max(calibrated_size_mm * _FONT_SIZE_SCALE * MM_TO_M, 0.0005)
    preferred_font = _get_preferred_font()
    if preferred_font is not None:
        try:
            font_data.font = preferred_font
        except Exception:
            pass
    center_anchor = (
        _should_center_anchor(
            text_item,
            strict_text_fidelity=strict_text_fidelity,
        )
        and text_item.bbox is not None
    )
    if center_anchor:
        font_data.align_x = "CENTER"
        font_data.align_y = "CENTER"
    else:
        font_data.align_x = "LEFT"
        # PyMuPDF insertion is baseline-oriented; use baseline alignment when available.
        try:
            font_data.align_y = "BOTTOM_BASELINE"
        except Exception:
            font_data.align_y = "BOTTOM"

    if mode == "3d_text":
        font_data.extrude = _text_extrusion_depth(font_data.size)
    else:
        font_data.extrude = 0.0
    if mode in {"glyphs", "geometry"}:
        try:
            font_data.resolution_u = max(int(getattr(font_data, "resolution_u", 12) or 12), 24)
        except Exception:
            pass

    # Create object and set position
    obj = bpy.data.objects.new(obj_name, font_data)
    try:
        obj["pdf_text_mode"] = mode
        obj["pdf_text_size_mm"] = float(calibrated_size_mm)
    except Exception:
        pass

    if center_anchor and text_item.bbox is not None:
        bx0, by0, bx1, by1 = text_item.bbox
        x = (bx0 + bx1) * 0.5
        y = (by0 + by1) * 0.5
    else:
        x, y = text_item.insertion
    obj.location = (x * MM_TO_M, y * MM_TO_M, z_offset_m)
    try:
        source_color = text_item.color
        mat = _get_or_create_text_material(visual_style, source_color=source_color)
        if len(font_data.materials) == 0:
            font_data.materials.append(mat)
        else:
            font_data.materials[0] = mat
        obj.color = mat.diffuse_color
    except Exception:
        pass

    collection.objects.link(obj)
    if mode == "3d_text" or not strict_text_fidelity:
        _fit_text_to_bbox(obj, text_item)

    # Apply rotation (text_item.rotation is in degrees)
    if text_item.rotation != 0.0:
        obj.rotation_euler = (0.0, 0.0, math.radians(text_item.rotation))

    if mode in {"glyphs", "geometry"}:
        obj = _meshify_text_object(obj, collection, mode)
    _record_text_provenance(
        provenance_opts,
        page_number=page_number,
        text_item=text_item,
        text_mode=mode,
        parent_handle=str(getattr(obj, "name", "") or ""),
    )
    return obj


def _meshify_text_object(
    obj: bpy.types.Object,
    collection: bpy.types.Collection,
    mode: str,
) -> bpy.types.Object:
    """Convert text curves to mesh geometry when Blender can evaluate them."""
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
        mesh.name = f"{obj.name}_mesh"
        mesh_obj = bpy.data.objects.new(obj.name, mesh)
        mesh_obj.matrix_world = obj.matrix_world.copy()
        mesh_obj.color = obj.color
        for mat in getattr(obj.data, "materials", []):
            mesh.materials.append(mat)
        mesh_obj["pdf_text_mode"] = mode
        mesh_obj["pdf_text_source"] = getattr(obj.data, "body", "")
        collection.objects.link(mesh_obj)
        try:
            collection.objects.unlink(obj)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
        return mesh_obj
    except Exception:
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
) -> int:
    """
    Build Blender text objects for all NormalizedText items.

    Args:
        text_items: List of NormalizedText items from page_data.text_items.
        collection: Target Blender collection.
        page_number: Page number for naming.

    Returns:
        Count of text objects created.
    """
    count = 0
    total = max(1, len(text_items or []))
    heartbeat_every = max(25, int(total / 25))
    for idx, item in enumerate(text_items):
        if progress_callback and (idx % heartbeat_every == 0):
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
        )
        if obj is not None:
            count += 1
    if progress_callback:
        try:
            progress_callback(1.0)
        except Exception:
            pass
    return count
