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
    "version": (1, 0, 2),
    "blender": (3, 0, 0),
    "location": "File > Import > PDF Vector (.pdf)",
    "description": "Import PDF vector drawings as native Blender geometry",
    "category": "Import-Export",
}


def register():
    """Register all addon classes and add menu entry."""
    from . import operators, preferences  # noqa: F811

    import bpy

    # Register the install-dependency operator first (used by preferences)
    preferences.register()

    bpy.utils.register_class(preferences.PDFVectorImporterPreferences)
    bpy.utils.register_class(operators.IMPORT_OT_pdf_vector)
    bpy.types.TOPBAR_MT_file_import.append(operators.menu_func_import)


def unregister():
    """Unregister all addon classes and remove menu entry."""
    from . import operators, preferences  # noqa: F811

    import bpy

    bpy.types.TOPBAR_MT_file_import.remove(operators.menu_func_import)
    bpy.utils.unregister_class(operators.IMPORT_OT_pdf_vector)
    bpy.utils.unregister_class(preferences.PDFVectorImporterPreferences)

    preferences.unregister()


if __name__ == "__main__":
    register()
