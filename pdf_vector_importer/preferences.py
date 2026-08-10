# -*- coding: utf-8 -*-
# preferences.py — Addon preferences panel
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Addon preferences panel for PDF Vector Importer.
Shows PyMuPDF install status and provides an Install button.
"""
from __future__ import annotations

from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .build_identity import IDENTITY_FILENAME
from .dependency_manager import (
    check_pymupdf,
    get_pymupdf_version,
    install_pymupdf,
    runtime_diagnostics,
)


def _is_packaged_release() -> bool:
    """True when the add-on carries the manifest written by build_release.py."""
    return Path(__file__).resolve().with_name(IDENTITY_FILENAME).is_file()


class PDFVEC_OT_install_pymupdf(bpy.types.Operator):
    """Install PyMuPDF for an unmanifested source/development tree."""

    bl_idname = "pdfvec.install_pymupdf"
    bl_label = "Source/Development: Install PyMuPDF"
    bl_description = (
        "Source/development only: use pip and the network to replace the add-on's "
        "private PyMuPDF package"
    )

    confirm_network_install: BoolProperty(  # type: ignore[assignment]
        name="Allow network download and package replacement",
        description=(
            "Confirm that pip may contact a package index and replace files in this "
            "source/development add-on tree"
        ),
        default=False,
    )

    @classmethod
    def poll(cls, _context):
        return not _is_packaged_release()

    def invoke(self, context, _event):
        if _is_packaged_release():
            self.report(
                {"ERROR"},
                "Packaged releases must be repaired by reinstalling the official release ZIP.",
            )
            return {"CANCELLED"}
        self.confirm_network_install = False
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Source/development tool only.", icon="ERROR")
        layout.label(text="This runs pip, uses the network, and replaces private package files.")
        layout.prop(self, "confirm_network_install")

    def execute(self, _context):
        if _is_packaged_release():
            self.report(
                {"ERROR"},
                "Reinstall the official release ZIP; packaged releases cannot run pip.",
            )
            return {"CANCELLED"}
        if not bool(getattr(self, "confirm_network_install", False)):
            self.report(
                {"WARNING"},
                "Confirm the network download and package replacement before continuing.",
            )
            return {"CANCELLED"}
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
            row.label(text=runtime_diagnostics(), icon="BLANK1")
            row = box.row()
            if _is_packaged_release():
                row.label(
                    text="Reinstall the official release ZIP; runtime checks stay offline.",
                    icon="INFO",
                )
            else:
                row.label(
                    text="Source/development dependency tool (network + package mutation):",
                    icon="INFO",
                )
                row = box.row()
                row.operator(PDFVEC_OT_install_pymupdf.bl_idname, icon="IMPORT")

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
_REGISTERED_PREF_CLASSES = []


def register():
    classes = () if _is_packaged_release() else _PREF_CLASSES
    for cls in classes:
        bpy.utils.register_class(cls)
        _REGISTERED_PREF_CLASSES.append(cls)


def unregister():
    for cls in reversed(_REGISTERED_PREF_CLASSES):
        bpy.utils.unregister_class(cls)
    _REGISTERED_PREF_CLASSES[:] = []
