# -*- coding: utf-8 -*-
# operators.py — Main import operator
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Blender import operator for PDF vector drawings.
Uses ImportHelper mixin for the file browser integration.
"""
from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper


# ── Preset enum items ────────────────────────────────────────────────
_PRESET_ITEMS = [
    ("fast", "Fast Preview", "Speed over fidelity — no text, no arcs, no faces"),
    ("general", "General Vector", "Good for most PDFs — balanced quality"),
    ("technical", "Technical Drawing", "Engineering drawings — arcs, dashes, faces"),
    ("shop", "Shop Drawing", "Fabrication drawings — full fidelity (default)"),
    (
        "raster_vector",
        "Raster + Vectors",
        "Import both raster images and vector geometry",
    ),
    ("raster_only", "Raster Only", "Import only raster/image content from the PDF"),
    ("max", "Max Fidelity", "Highest accuracy — slowest import"),
]

_TEXT_MODE_ITEMS = [
    ("none", "None", "Do not import text"),
    ("labels", "Labels", "Import text as Blender text objects (default)"),
    ("geometry", "Geometry", "Convert text to mesh geometry"),
]


class IMPORT_OT_pdf_vector(bpy.types.Operator, ImportHelper):
    """Import PDF vector drawings as native Blender geometry."""

    bl_idname = "import_scene.pdf_vector"
    bl_label = "Import PDF Vector"
    bl_description = "Import PDF vector drawings as Blender curves and meshes"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".pdf"
    filter_glob: StringProperty(default="*.pdf", options={"HIDDEN"})  # type: ignore[assignment]

    # ── Properties ───────────────────────────────────────────────────
    preset: EnumProperty(  # type: ignore[assignment]
        name="Preset",
        description="Import quality preset",
        items=_PRESET_ITEMS,
        default="shop",
    )

    pages: StringProperty(  # type: ignore[assignment]
        name="Pages",
        description="Pages to import: 'all', '1', '1,3-5', or '2-4'",
        default="all",
    )

    text_mode: EnumProperty(  # type: ignore[assignment]
        name="Text Mode",
        description="How to handle text in the PDF",
        items=_TEXT_MODE_ITEMS,
        default="labels",
    )

    detect_arcs: BoolProperty(  # type: ignore[assignment]
        name="Detect Arcs",
        description="Reconstruct arcs and circles from polyline segments",
        default=True,
    )

    make_faces: BoolProperty(  # type: ignore[assignment]
        name="Make Faces",
        description="Create mesh faces from closed loops and rectangles",
        default=True,
    )

    group_by_color: BoolProperty(  # type: ignore[assignment]
        name="Group by Color",
        description="Organize geometry into sub-collections by stroke color",
        default=True,
    )

    map_dashes: BoolProperty(  # type: ignore[assignment]
        name="Map Dash Patterns",
        description="Preserve PDF dash patterns as Blender line styles",
        default=True,
    )

    def execute(self, context):
        from . import bl_import_engine

        # Build config dict from operator properties
        config = {
            "preset": self.preset,
            "pages": self.pages,
            "text_mode": self.text_mode,
            "detect_arcs": self.detect_arcs,
            "make_faces": self.make_faces,
            "group_by_color": self.group_by_color,
            "map_dashes": self.map_dashes,
        }

        try:
            stats = bl_import_engine.import_pdf(self.filepath, config=config)
        except Exception as exc:
            self.report({"ERROR"}, f"PDF import failed: {exc}")
            return {"CANCELLED"}

        prims = stats.get("primitives", 0)
        texts = stats.get("text_items", 0)
        pages = stats.get("pages_imported", 0)
        self.report(
            {"INFO"},
            f"Imported {prims} primitives, {texts} text items from {pages} page(s)",
        )
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout

        # Preset selector
        layout.prop(self, "preset")
        layout.separator()

        # Page selection
        layout.prop(self, "pages")
        layout.separator()

        # Individual options
        box = layout.box()
        box.label(text="Options", icon="PREFERENCES")
        box.prop(self, "text_mode")
        box.prop(self, "detect_arcs")
        box.prop(self, "make_faces")
        box.prop(self, "group_by_color")
        box.prop(self, "map_dashes")


def menu_func_import(self, context):
    """Append to File > Import menu."""
    self.layout.operator(
        IMPORT_OT_pdf_vector.bl_idname, text="PDF Vector (.pdf)"
    )
