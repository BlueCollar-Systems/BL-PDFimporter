# -*- coding: utf-8 -*-
# operators.py — Main import operator
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Blender import operator for PDF vector drawings.
Uses ImportHelper mixin for the file browser integration.

BCS-ARCH-001 Rule 5 sweep: every quality-tier dial (detect_arcs,
make_faces, map_dashes, ignore_fill_only_shapes) has been removed from
the operator's user-facing properties. Internal defaults from
pdfcadcore.ImportConfig apply universally — modes differ by strategy
on input type, never by quality tier.
"""
from __future__ import annotations

import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

# Shelved 2026-07-06: closed-region shape extrusion deferred; code retained.
SHAPE_EXTRUSION_UI_ENABLED = False


# ── Mode enum items (BCS-ARCH-001) ───────────────────────────────────
_MODE_ITEMS = [
    ("auto",   "Auto",   "Analyze and pick Vector/Raster/Hybrid automatically"),
    ("vector", "Vector", "Extract all vector geometry faithfully"),
    ("raster", "Raster", "Place as high-DPI image (scanned PDFs)"),
    ("hybrid", "Hybrid", "Vectors where clean, raster where lossy"),
]

# Text rendering is orthogonal to mode. A separate ``import_text``
# boolean toggles whether text is imported at all.
_TEXT_MODE_ITEMS = [
    (
        "labels",
        "Labels",
        "Request persistent model labels; any host limitation and closest fallback are reported per item",
    ),
    ("text",     "Text",     "Flat editable Blender FONT text using the exact PDF font program"),
    ("3d_text",  "3D Text",  "Extruded editable Blender FONT text using the exact PDF font program"),
    ("glyphs",   "Glyphs",   "Convert exact-font text to non-editable CURVE glyph outlines"),
    ("geometry", "Geometry", "Convert exact-font text to non-editable MESH geometry"),
    ("raster",   "Raster",   "Render each source text item as an aligned raster patch"),
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

_MODEL3D_ITEMS = [
    ("off", "Off", "Keep the imported PDF flat"),
    (
        "auto",
        "Auto (if drawing has 3D evidence)",
        "Extrude filled closed regions only when text evidence makes a 3D result honest",
    ),
    (
        "extrude",
        "Extrude closed shapes",
        "Explicitly extrude eligible closed PDF regions while skipping page-background fills",
    ),
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
    show_advanced: BoolProperty(  # type: ignore[assignment]
        name="Advanced Options",
        description="Show import strategy override (Vector / Raster / Hybrid)",
        default=False,
        options={"SKIP_SAVE"},
    )

    mode: EnumProperty(  # type: ignore[assignment]
        name="Import Strategy",
        description=(
            "Override Auto with a fixed strategy for all pages (Advanced only). "
            "Every strategy targets maximum fidelity (BCS-ARCH-001)."
        ),
        items=_MODE_ITEMS,
        default="auto",
    )

    pages: StringProperty(  # type: ignore[assignment]
        name="Pages",
        description="Pages to import: 'all', '1', '1,3-5', or '2-4'",
        default="all",
    )

    import_text: BoolProperty(  # type: ignore[assignment]
        name="Import Text",
        description="Import text from the PDF (orthogonal to Mode)",
        default=True,
    )

    text_mode: EnumProperty(  # type: ignore[assignment]
        name="Text Mode",
        description="How imported text is represented in the scene",
        items=_TEXT_MODE_ITEMS,
        default="3d_text",
    )

    group_by_color: BoolProperty(  # type: ignore[assignment]
        name="Group by Color",
        description="Organize geometry into sub-collections by stroke color",
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

    model3d_mode: EnumProperty(  # type: ignore[assignment]
        name="3D Model",
        description="Optional 3D model generation from closed PDF geometry",
        items=_MODEL3D_ITEMS,
        default="off",
    )

    model3d_depth_mm: FloatProperty(  # type: ignore[assignment]
        name="3D Depth (mm)",
        description="Extrusion depth for generated 3D solids",
        default=3.175,
        min=0.01,
        max=100000.0,
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

        # Build config dict from operator properties.
        # BCS-ARCH-001 Rule 5: detect_arcs / make_faces / map_dashes /
        # ignore_fill_only_shapes are no longer user-adjustable. Their
        # consolidated defaults come from pdfcadcore.ImportConfig.
        effective_mode = self.mode if self.show_advanced else "auto"
        config = {
            "mode": effective_mode,
            "pages": self.pages,
            "import_text": self.import_text,
            "text_mode": self.text_mode,
            "group_by_color": self.group_by_color,
            "visual_style": self.visual_style,
            "line_z_offset_mm": self.line_z_offset_mm,
            "text_z_offset_mm": self.text_z_offset_mm,
            "image_z_offset_mm": self.image_z_offset_mm,
            "auto_focus_view": self.auto_focus_view,
            "keep_selection_after_focus": self.keep_selection_after_focus,
            "auto_hide_default_cube": self.auto_hide_default_cube,
            "page_arrangement": self.page_arrangement,
            "page_gap_ratio": self.page_gap_ratio,
            "model3d_mode": self.model3d_mode if SHAPE_EXTRUSION_UI_ENABLED else "off",
            "model3d_depth_mm": self.model3d_depth_mm if SHAPE_EXTRUSION_UI_ENABLED else 3.175,
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
            from .pdfcadcore.fitz_loader import PdfOpenError

            _set_status(None)
            if isinstance(exc, PdfOpenError):
                self.report({"ERROR"}, str(exc))
            else:
                self.report({"ERROR"}, f"PDF import failed: {exc}")
            return {"CANCELLED"}

        _set_status(None)

        report_error = str(stats.get("import_report_error") or "").strip()
        if report_error:
            self.report(
                {"ERROR"},
                "PDF objects were created, but the mandatory import report failed: "
                f"{report_error}. Do not trust this import until reporting succeeds.",
            )
        temp_cleanup_error = str(stats.get("temp_cleanup_error") or "").strip()
        if temp_cleanup_error:
            self.report(
                {"ERROR"},
                "PDF delivery completed, but owned temporary files could not be "
                f"cleaned: {temp_cleanup_error}. Review the import report.",
            )

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
        failed_text = int(stats.get("text_delivery_failed_items", 0) or 0)
        fallback_text = int(stats.get("text_delivery_fallback_items", 0) or 0)
        report_path = str(stats.get("import_report_path") or "")
        if failed_text > 0:
            self.report(
                {"ERROR"},
                f"{failed_text} text item(s) were not delivered. "
                f"Do not trust this import until extra.text_delivery is reviewed in {report_path or 'the import report'}.",
            )
        elif fallback_text > 0:
            self.report(
                {"WARNING"},
                f"{fallback_text} text item(s) used a proven item-specific fallback; "
                f"requested/final types and evidence are in {report_path or 'the import report'}.",
            )
        raster_failures = list(stats.get("raster_delivery_failures") or [])
        if raster_failures:
            self.report(
                {"ERROR"},
                f"{len(raster_failures)} raster delivery attempt(s) failed. "
                f"The import is incomplete; review extra.raster_delivery_failures in "
                f"{report_path or 'the import report'}.",
            )
        geometry_issues = list(stats.get("geometry_delivery_issues") or [])
        if geometry_issues:
            geometry_failures = sum(
                1
                for issue in geometry_issues
                if str(issue.get("status") or "").strip().lower() != "verified"
            )
            severity = {"ERROR"} if geometry_failures else {"WARNING"}
            self.report(
                severity,
                f"{len(geometry_issues)} geometry primitive(s) required a reported "
                f"source-point approximation; {geometry_failures} were not delivered. "
                f"Review extra.geometry_delivery_issues in {report_path or 'the import report'}.",
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

        layout.label(
            text="Professional import — maximum fidelity; Auto picks "
            "vector, raster, or hybrid per page.",
            icon="INFO",
        )
        layout.separator()

        # Page selection
        layout.prop(self, "pages")
        layout.separator()

        adv = layout.box()
        adv.prop(self, "show_advanced", icon="MODIFIER")
        if self.show_advanced:
            adv.prop(self, "mode")

        # Individual options (BCS-ARCH-001 Rule 5 — Text + Import Text;
        # strategy override is Advanced-only. Other quality dials are baked in.)
        box = layout.box()
        box.label(text="Options", icon="PREFERENCES")
        box.prop(self, "import_text")
        sub = box.row()
        sub.enabled = self.import_text
        sub.prop(self, "text_mode")
        box.prop(self, "group_by_color")

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

        box = layout.box()
        if SHAPE_EXTRUSION_UI_ENABLED:
            box.label(text="3D Model", icon="MESH_CUBE")
            box.prop(self, "model3d_mode")
            row = box.row()
            row.enabled = self.model3d_mode != "off"
            row.prop(self, "model3d_depth_mm")


def menu_func_import(self, context):
    """Append to File > Import menu."""
    self.layout.operator(
        IMPORT_OT_pdf_vector.bl_idname, text="PDF Vector (.pdf)"
    )
