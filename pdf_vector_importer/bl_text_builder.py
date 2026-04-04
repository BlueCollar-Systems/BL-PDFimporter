# -*- coding: utf-8 -*-
# bl_text_builder.py — Text rendering for Blender
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Creates Blender Text (font) curve objects from pdfcadcore NormalizedText items.
"""
from __future__ import annotations

import math
from typing import Optional

import bpy

from .pdfcadcore.primitives import NormalizedText

# PDF font sizes are in mm from the extractor. Blender text size is in
# Blender units (meters by default). We scale down so text appears at
# a reasonable size relative to the imported geometry (also in mm).
_FONT_SIZE_SCALE = 1.0


def build_text(
    text_item: NormalizedText,
    collection: bpy.types.Collection,
    page_number: int = 0,
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
    font_data.size = max(text_item.font_size * _FONT_SIZE_SCALE, 0.5)
    font_data.align_x = "LEFT"
    font_data.align_y = "BOTTOM"

    # Extrude to zero — flat text
    font_data.extrude = 0.0

    # Create object and set position
    obj = bpy.data.objects.new(obj_name, font_data)

    x, y = text_item.insertion
    obj.location = (x, y, 0.0)

    # Apply rotation (text_item.rotation is in degrees)
    if text_item.rotation != 0.0:
        obj.rotation_euler = (0.0, 0.0, math.radians(text_item.rotation))

    collection.objects.link(obj)
    return obj


def build_all_text(
    text_items: list,
    collection: bpy.types.Collection,
    page_number: int = 0,
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
    for item in text_items:
        obj = build_text(item, collection, page_number)
        if obj is not None:
            count += 1
    return count
