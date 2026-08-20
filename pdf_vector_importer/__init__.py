# -*- coding: utf-8 -*-
# PDF Vector Importer for Blender — Addon registration
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Blender addon that imports PDF vector drawings as native geometry.
Uses the pdfcadcore shared library for extraction and recognition.
"""
from __future__ import annotations

bl_info = {
    "name": "PDF Vector Importer",
    "author": "BlueCollar Systems",
    "version": (1, 0, 94),
    "blender": (3, 1, 0),
    "location": "File > Import > PDF Vector (.pdf)",
    "description": "Import PDF vector drawings as native Blender geometry",
    "category": "Import-Export",
}


def register():
    """Register all addon classes and add menu entry."""
    from . import operators, preferences  # noqa: F811
    from .dependency_manager import (
        ensure_pymupdf_runtime,
        print_diagnostics,
        report_host_python_floor,
    )

    import bpy

    # Register the install-dependency operator first (used by preferences)
    preferences.register()

    bpy.utils.register_class(preferences.PDFVectorImporterPreferences)
    bpy.utils.register_class(operators.IMPORT_OT_pdf_vector_cancel)
    bpy.utils.register_class(operators.IMPORT_OT_pdf_vector)
    bpy.types.TOPBAR_MT_file_import.append(operators.menu_func_import)

    print_diagnostics()
    report_host_python_floor()
    if not ensure_pymupdf_runtime(auto_install=False):
        print(
            "[PDF Vector Importer] PyMuPDF unavailable at register time. "
            "Reinstall the official release ZIP; runtime checks do not run pip "
            "or access the network."
        )


def unregister():
    """Unregister all addon classes and remove menu entry."""
    from . import operators, preferences  # noqa: F811

    import bpy

    bpy.types.TOPBAR_MT_file_import.remove(operators.menu_func_import)
    bpy.utils.unregister_class(operators.IMPORT_OT_pdf_vector)
    bpy.utils.unregister_class(operators.IMPORT_OT_pdf_vector_cancel)
    bpy.utils.unregister_class(preferences.PDFVectorImporterPreferences)

    preferences.unregister()


if __name__ == "__main__":
    register()
