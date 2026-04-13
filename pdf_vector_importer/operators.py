# -*- coding: utf-8 -*-
# operators.py — Main import operator
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Blender import operator for PDF vector drawings.
Uses ImportHelper mixin for the file browser integration.
"""
from __future__ import annotations

import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
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

_VISUAL_STYLE_ITEMS = [
    ("source", "Source Accurate", "Preserve source PDF colors"),
    ("blueprint", "Blueprint Preview", "Crisp cyan linework for readability"),
    ("high_contrast", "High Contrast", "Bright monochrome linework for dark viewports"),
]

_PAGE_ARRANGEMENT_ITEMS = [
    ("spread", "Spread (20% gap)", "Stack pages with a 20% gap"),
    ("compact", "Compact gap", "Stack pages with configurable compact gap"),
    ("touch", "Touching pages", "Stack pages edge-to-edge without a gap"),
    ("overlay", "Overlay pages", "Place all pages at the same origin"),
]


def _addon_prefs(context):
    addon = context.preferences.addons.get("pdf_vector_importer")
    if addon is None:
        return None
    return addon.preferences


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
        default=False,
    )

    ignore_fill_only_shapes: BoolProperty(  # type: ignore[assignment]
        name="Ignore Fill-Only Shapes",
        description="Skip fill-only PDF shapes that can hide linework",
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

    visual_style: EnumProperty(  # type: ignore[assignment]
        name="Visual Style",
        description="Display style for imported vectors/text",
        items=_VISUAL_STYLE_ITEMS,
        default="blueprint",
    )

    line_z_offset_mm: FloatProperty(  # type: ignore[assignment]
        name="Line Z Offset (mm)",
        description="Small Z offset applied to vector curves to reduce z-fighting",
        default=0.10,
        min=-5.0,
        max=5.0,
    )

    text_z_offset_mm: FloatProperty(  # type: ignore[assignment]
        name="Text Z Offset (mm)",
        description="Raise text slightly above linework for readability",
        default=0.35,
        min=-5.0,
        max=5.0,
    )

    image_z_offset_mm: FloatProperty(  # type: ignore[assignment]
        name="Image Z Offset (mm)",
        description="Lower raster images slightly below vectors and text",
        default=-0.2,
        min=-5.0,
        max=5.0,
    )

    auto_focus_view: BoolProperty(  # type: ignore[assignment]
        name="Auto Focus Imported Drawing",
        description="Frame and focus the viewport on imported geometry",
        default=True,
        options={"SKIP_SAVE"},
    )

    keep_selection_after_focus: BoolProperty(  # type: ignore[assignment]
        name="Keep Selection After Focus",
        description="Keep imported objects selected after viewport framing",
        default=False,
        options={"SKIP_SAVE"},
    )

    auto_hide_default_cube: BoolProperty(  # type: ignore[assignment]
        name="Auto Hide Default Cube",
        description="Hide Blender's default startup cube if present so imported drawings are not occluded",
        default=True,
    )

    page_arrangement: EnumProperty(  # type: ignore[assignment]
        name="Page Layout",
        description="How multi-page imports are arranged in the scene",
        items=_PAGE_ARRANGEMENT_ITEMS,
        default="spread",
    )

    page_gap_ratio: FloatProperty(  # type: ignore[assignment]
        name="Compact Gap Ratio",
        description="Gap ratio for compact page layout (0.20 = 20% page break)",
        default=0.20,
        min=0.0,
        max=1.0,
    )

    def invoke(self, context, event):
        prefs = _addon_prefs(context)
        if prefs is not None:
            try:
                self.visual_style = prefs.default_visual_style
            except Exception:
                pass
            # Keep focus on by default each import run unless user turns it off now.
            self.auto_focus_view = True
            self.keep_selection_after_focus = False

            remember = bool(getattr(prefs, "remember_last_directory", True))
            last_dir = str(getattr(prefs, "last_import_dir", "") or "")
            if remember and last_dir and os.path.isdir(last_dir):
                # ImportHelper uses filepath as initial browser path.
                self.filepath = os.path.join(last_dir, "")

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        from . import bl_import_engine

        # Build config dict from operator properties
        config = {
            "preset": self.preset,
            "pages": self.pages,
            "text_mode": self.text_mode,
            "detect_arcs": self.detect_arcs,
            "make_faces": self.make_faces,
            "ignore_fill_only_shapes": self.ignore_fill_only_shapes,
            "group_by_color": self.group_by_color,
            "map_dashes": self.map_dashes,
            "visual_style": self.visual_style,
            "line_z_offset_mm": self.line_z_offset_mm,
            "text_z_offset_mm": self.text_z_offset_mm,
            "image_z_offset_mm": self.image_z_offset_mm,
            "auto_focus_view": self.auto_focus_view,
            "keep_selection_after_focus": self.keep_selection_after_focus,
            "auto_hide_default_cube": self.auto_hide_default_cube,
            "page_arrangement": self.page_arrangement,
            "page_gap_ratio": self.page_gap_ratio,
        }

        def _set_status(text: str | None):
            try:
                ws = getattr(context, "workspace", None)
                if ws is not None and hasattr(ws, "status_text_set"):
                    ws.status_text_set(text)
            except Exception:
                pass

        def _on_progress(pct: float, message: str):
            try:
                pct_i = int(max(0, min(100, round(float(pct) * 100.0))))
            except Exception:
                pct_i = 0
            _set_status(f"PDF Import {pct_i}% - {message}")

        try:
            _set_status("PDF Import 0% - Starting import...")
            stats = bl_import_engine.import_pdf(
                self.filepath,
                config=config,
                progress_callback=_on_progress,
                context=context,
            )
        except Exception as exc:
            _set_status(None)
            self.report({"ERROR"}, f"PDF import failed: {exc}")
            return {"CANCELLED"}

        _set_status(None)

        prims = stats.get("primitives", 0)
        texts = stats.get("text_items", 0)
        images = stats.get("images", 0)
        pages = stats.get("pages_imported", 0)
        skipped_fill = stats.get("skipped_fill_only", 0)
        hidden_cube = stats.get("hidden_startup_cube", 0)
        self.report(
            {"INFO"},
            f"Imported {prims} primitives, {texts} text items, {images} images from {pages} page(s); "
            f"skipped {skipped_fill} fill-only shapes; hid {hidden_cube} default cube(s)",
        )

        prefs = _addon_prefs(context)
        if prefs is not None and bool(getattr(prefs, "remember_last_directory", True)):
            try:
                last_dir = os.path.dirname(self.filepath)
                if last_dir and os.path.isdir(last_dir):
                    prefs.last_import_dir = last_dir
            except Exception:
                pass

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
        box.prop(self, "ignore_fill_only_shapes")
        box.prop(self, "group_by_color")
        box.prop(self, "map_dashes")

        box = layout.box()
        box.label(text="View & Readability", icon="SHADING_RENDERED")
        box.prop(self, "visual_style")
        box.prop(self, "auto_focus_view")
        box.prop(self, "keep_selection_after_focus")
        box.prop(self, "auto_hide_default_cube")
        box.prop(self, "page_arrangement")
        box.prop(self, "page_gap_ratio")
        col = box.column(align=True)
        col.prop(self, "line_z_offset_mm")
        col.prop(self, "text_z_offset_mm")
        col.prop(self, "image_z_offset_mm")


def menu_func_import(self, context):
    """Append to File > Import menu."""
    self.layout.operator(
        IMPORT_OT_pdf_vector.bl_idname, text="PDF Vector (.pdf)"
    )
