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
from typing import Optional, Tuple

import bpy

from .pdfcadcore.primitives import NormalizedText

# PDF font sizes are in mm from the extractor. Blender text size is in
# Blender units (meters by default), so convert mm -> m.
MM_TO_M = 0.001
_FONT_SIZE_SCALE = 1.0
_FONT_CACHE: Optional[bpy.types.VectorFont] = None


def _normalize_style(style: str) -> str:
    key = (style or "source").strip().lower()
    if key in {"source", "blueprint", "high_contrast"}:
        return key
    return "source"


def _styled_text_color(style: str) -> Tuple[float, float, float]:
    style_key = _normalize_style(style)
    if style_key == "blueprint":
        # Brighter blueprint ink tone for dark viewport readability.
        return (0.36, 0.74, 0.98)
    if style_key == "high_contrast":
        return (0.95, 0.95, 0.95)
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


def _get_or_create_text_material(style: str) -> bpy.types.Material:
    style_key = _normalize_style(style)
    mat_name = f"PDF_Text_{style_key}"
    existing = bpy.data.materials.get(mat_name)
    if existing is not None:
        return existing

    mat = bpy.data.materials.new(name=mat_name)
    r, g, b = _styled_text_color(style_key)
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

    # Axis-aligned bbox fitting distorts vertical/diagonal labels.
    rot = abs(float(text_item.rotation or 0.0))
    if rot > 1.0 and abs(rot - 180.0) > 1.0:
        return

    bx0, by0, bx1, by1 = text_item.bbox
    target_w = max((bx1 - bx0) * MM_TO_M, 0.0)
    target_h = max((by1 - by0) * MM_TO_M, 0.0)
    if target_w <= 1e-9 or target_h <= 1e-9:
        return

    current_w = max(float(obj.dimensions.x), 1e-9)
    current_h = max(float(obj.dimensions.y), 1e-9)
    scale_w = target_w / current_w
    scale_h = target_h / current_h
    if not math.isfinite(scale_w) or not math.isfinite(scale_h):
        return

    # Use uniform scale to avoid glyph deformation (e.g. "15/16" -> "15/6").
    s = min(scale_h, scale_w * 1.15)
    s = max(0.10, min(10.0, s))
    obj.scale.x *= s
    obj.scale.y *= s


def build_text(
    text_item: NormalizedText,
    collection: bpy.types.Collection,
    page_number: int = 0,
    visual_style: str = "source",
    z_offset_m: float = 0.0,
    strict_text_fidelity: bool = True,
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

    obj_name = f"P{page_number}_text_{text_item.id}"

    # Create font curve data
    font_data = bpy.data.curves.new(name=obj_name, type="FONT")
    font_data.body = text_item.text
    font_data.size = max(text_item.font_size * _FONT_SIZE_SCALE * MM_TO_M, 0.0005)
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

    # Extrude to zero — flat text
    font_data.extrude = 0.0

    # Create object and set position
    obj = bpy.data.objects.new(obj_name, font_data)

    if center_anchor and text_item.bbox is not None:
        bx0, by0, bx1, by1 = text_item.bbox
        x = (bx0 + bx1) * 0.5
        y = (by0 + by1) * 0.5
    else:
        x, y = text_item.insertion
    obj.location = (x * MM_TO_M, y * MM_TO_M, z_offset_m)
    if not strict_text_fidelity:
        _fit_text_to_bbox(obj, text_item)

    # Apply rotation (text_item.rotation is in degrees)
    if text_item.rotation != 0.0:
        obj.rotation_euler = (0.0, 0.0, math.radians(text_item.rotation))

    try:
        mat = _get_or_create_text_material(visual_style)
        if len(font_data.materials) == 0:
            font_data.materials.append(mat)
        else:
            font_data.materials[0] = mat
        obj.color = mat.diffuse_color
    except Exception:
        pass

    collection.objects.link(obj)
    return obj


def build_all_text(
    text_items: list,
    collection: bpy.types.Collection,
    page_number: int = 0,
    visual_style: str = "source",
    z_offset_m: float = 0.0,
    strict_text_fidelity: bool = True,
    progress_callback=None,
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
        )
        if obj is not None:
            count += 1
    if progress_callback:
        try:
            progress_callback(1.0)
        except Exception:
            pass
    return count
