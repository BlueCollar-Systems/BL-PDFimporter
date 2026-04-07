# -*- coding: utf-8 -*-
# preferences.py — Addon preferences panel
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Addon preferences panel for PDF Vector Importer.
Shows PyMuPDF install status and provides an Install button.
"""
from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .dependency_manager import check_pymupdf, get_pymupdf_version, install_pymupdf


class PDFVEC_OT_install_pymupdf(bpy.types.Operator):
    """Install PyMuPDF dependency"""

    bl_idname = "pdfvec.install_pymupdf"
    bl_label = "Install PyMuPDF"
    bl_description = "Download and install the PyMuPDF library required for PDF parsing"

    def execute(self, context):
        self.report({"INFO"}, "Installing PyMuPDF... this may take a moment.")
        success = install_pymupdf()
        if success:
            version = get_pymupdf_version()
            self.report({"INFO"}, f"PyMuPDF {version} installed successfully.")
        else:
            self.report(
                {"ERROR"},
                "Failed to install PyMuPDF. Check Blender console for details.",
            )
        return {"FINISHED"}


class PDFVectorImporterPreferences(bpy.types.AddonPreferences):
    """Addon preferences for PDF Vector Importer."""

    bl_idname = "pdf_vector_importer"

    remember_last_directory: BoolProperty(  # type: ignore[assignment]
        name="Remember Last Import Folder",
        description="Preselect the previously used folder when opening the PDF importer",
        default=True,
    )

    last_import_dir: StringProperty(  # type: ignore[assignment]
        name="Last Import Folder",
        description="Most recently used PDF folder",
        subtype="DIR_PATH",
        default="",
    )

    default_visual_style: EnumProperty(  # type: ignore[assignment]
        name="Default Visual Style",
        description="Default look for imported vectors and text",
        items=[
            ("source", "Source Accurate", "Preserve source PDF colors"),
            ("blueprint", "Blueprint Preview", "Crisp cyan linework for better readability"),
            ("high_contrast", "High Contrast", "Dark monochrome linework for clarity"),
        ],
        default="high_contrast",
    )

    @property
    def pymupdf_installed(self) -> bool:
        """True if PyMuPDF is available for import."""
        return check_pymupdf()

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Dependencies", icon="PACKAGE")

        if self.pymupdf_installed:
            version = get_pymupdf_version()
            row = box.row()
            row.label(text=f"PyMuPDF: installed (v{version})", icon="CHECKMARK")
        else:
            row = box.row()
            row.label(text="PyMuPDF: NOT installed", icon="ERROR")
            row = box.row()
            row.operator(PDFVEC_OT_install_pymupdf.bl_idname, icon="IMPORT")
            row = box.row()
            row.label(text="PyMuPDF is required for PDF parsing.", icon="INFO")

        layout.separator()
        box = layout.box()
        box.label(text="Workflow", icon="FILE_FOLDER")
        box.prop(self, "remember_last_directory")
        if self.remember_last_directory:
            box.prop(self, "last_import_dir")

        layout.separator()
        box = layout.box()
        box.label(text="Default Look", icon="SHADING_RENDERED")
        box.prop(self, "default_visual_style")


# Additional class for register/unregister — the install operator needs
# separate registration since it is used from the preferences panel.
_PREF_CLASSES = (PDFVEC_OT_install_pymupdf,)


def register():
    for cls in _PREF_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_PREF_CLASSES):
        bpy.utils.unregister_class(cls)
