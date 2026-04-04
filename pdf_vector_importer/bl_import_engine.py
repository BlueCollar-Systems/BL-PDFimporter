# -*- coding: utf-8 -*-
# bl_import_engine.py — Main import orchestrator for Blender
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Top-level import pipeline that ties together pdfcadcore extraction,
optional recognition, and Blender geometry/text building.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Dict, List, Optional

import bpy

from .dependency_manager import check_pymupdf, ensure_lib_path
from .pdfcadcore import ImportConfig, extract_page, recognition, reset_ids
from .bl_geometry_builder import build_page
from .bl_text_builder import build_all_text


# ── Preset mapping ───────────────────────────────────────────────────

def _config_from_preset(preset_name: str) -> ImportConfig:
    """Map a UI preset name to an ImportConfig instance."""
    key = (preset_name or "shop").strip().lower()
    if key in ("fast", "fast_preview"):
        return ImportConfig.fast()
    if key in ("general", "general_vector"):
        return ImportConfig.general_vector()
    if key in ("technical", "technical_drawing"):
        return ImportConfig.technical_drawing()
    if key in ("shop", "shop_drawing"):
        return ImportConfig.shop_drawing()
    if key in ("raster_vector", "raster+vectors"):
        cfg = ImportConfig.general_vector()
        cfg.import_mode = "hybrid"
        cfg.ignore_images = False
        cfg.raster_fallback = True
        return cfg
    if key in ("raster_only", "raster"):
        cfg = ImportConfig.fast()
        cfg.import_mode = "raster"
        cfg.ignore_images = False
        cfg.raster_fallback = True
        cfg.import_text = False
        return cfg
    if key in ("max", "max_fidelity"):
        return ImportConfig.max_fidelity()
    # Default fallback
    return ImportConfig.shop_drawing()


def _apply_overrides(config: ImportConfig, ui_config: dict) -> ImportConfig:
    """Apply operator UI overrides onto an ImportConfig."""
    if "text_mode" in ui_config:
        config.text_mode = ui_config["text_mode"]
        config.import_text = ui_config["text_mode"] != "none"
    if "detect_arcs" in ui_config:
        config.detect_arcs = ui_config["detect_arcs"]
    if "make_faces" in ui_config:
        config.make_faces = ui_config["make_faces"]
    if "group_by_color" in ui_config:
        config.group_by_color = ui_config["group_by_color"]
    if "map_dashes" in ui_config:
        config.map_dashes = ui_config["map_dashes"]
    return config


# ── Page range parsing ───────────────────────────────────────────────

def _parse_pages(page_str: str, total_pages: int) -> List[int]:
    """
    Parse a page range string into a list of 0-based page indices.

    Supports: 'all', '1', '1,3-5', '2-4'
    Input is 1-based (user-facing). Output is 0-based (internal).
    """
    page_str = (page_str or "all").strip().lower()

    if page_str in ("all", "", "*"):
        return list(range(total_pages))

    pages = set()
    for part in page_str.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            lo = max(1, int(m.group(1)))
            hi = min(total_pages, int(m.group(2)))
            for p in range(lo, hi + 1):
                pages.add(p - 1)
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < total_pages:
                pages.add(idx)

    return sorted(pages) if pages else list(range(total_pages))


# ── Main import entry point ──────────────────────────────────────────

def import_pdf(
    filepath: str,
    config: Optional[dict] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, int]:
    """
    Import a PDF file into Blender. Main entry point.

    Args:
        filepath: Absolute path to the PDF file.
        config: Dict with keys like 'preset', 'pages', 'text_mode',
                'detect_arcs', 'make_faces', 'group_by_color', 'map_dashes'.
        progress_callback: Optional callable(progress_float, message_str).

    Returns:
        Stats dict: {
            'pages_imported': int,
            'primitives': int,
            'text_items': int,
            'curves': int,
            'meshes': int,
            'circles': int,
            'arcs': int,
        }

    Raises:
        RuntimeError: If PyMuPDF is not available.
        FileNotFoundError: If the PDF file does not exist.
    """
    if config is None:
        config = {}

    def _progress(pct: float, msg: str):
        if progress_callback:
            progress_callback(pct, msg)

    # 1. Verify PyMuPDF is available
    _progress(0.0, "Checking dependencies...")
    if not check_pymupdf():
        raise RuntimeError(
            "PyMuPDF is not installed. Open addon preferences "
            "(Edit > Preferences > Add-ons > PDF Vector Importer) "
            "and click 'Install PyMuPDF'."
        )

    ensure_lib_path()
    import fitz  # noqa: E402

    # 2. Verify file exists
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"PDF file not found: {filepath}")

    # 3. Build ImportConfig from preset + overrides
    import_cfg = _config_from_preset(config.get("preset", "shop"))
    import_cfg = _apply_overrides(import_cfg, config)

    # 4. Reset pdfcadcore ID counter
    reset_ids()

    # 5. Open PDF
    _progress(0.05, "Opening PDF...")
    doc = fitz.open(filepath)
    total_pages = doc.page_count

    # 6. Determine pages to import
    page_indices = _parse_pages(config.get("pages", "all"), total_pages)
    if not page_indices:
        doc.close()
        return {
            "pages_imported": 0, "primitives": 0, "text_items": 0,
            "curves": 0, "meshes": 0, "circles": 0, "arcs": 0,
        }

    # 7. Create root collection
    basename = os.path.splitext(os.path.basename(filepath))[0]
    root_col = bpy.data.collections.new(f"PDF Import - {basename}")
    bpy.context.scene.collection.children.link(root_col)

    # 8. Build config dict for geometry builder
    builder_config = {
        "make_faces": import_cfg.make_faces,
        "group_by_color": import_cfg.group_by_color,
    }

    # 9. Process each page
    total_stats = {
        "pages_imported": 0, "primitives": 0, "text_items": 0,
        "curves": 0, "meshes": 0, "circles": 0, "arcs": 0,
    }

    for i, page_idx in enumerate(page_indices):
        pct = 0.1 + 0.85 * (i / len(page_indices))
        page_num = page_idx + 1
        _progress(pct, f"Extracting page {page_num}...")

        page = doc.load_page(page_idx)

        # 9a. Extract primitives via pdfcadcore
        page_data = extract_page(
            page, page_num,
            scale=import_cfg.user_scale,
            flip_y=import_cfg.flip_y,
        )

        # 9b. Optional recognition pass
        try:
            recognition.run(page_data, mode="auto")
        except Exception:
            # Recognition failure is non-fatal
            pass

        # 9c. Create page collection
        page_col = bpy.data.collections.new(f"PDF_Page_{page_num}")
        root_col.children.link(page_col)

        # 9d. Build geometry
        _progress(pct + 0.02, f"Building geometry for page {page_num}...")
        page_stats = build_page(page_data, page_col, builder_config)

        # 9e. Build text objects
        text_count = 0
        if import_cfg.import_text and import_cfg.text_mode != "none":
            text_count = build_all_text(
                page_data.text_items, page_col, page_num,
            )

        # 9f. Accumulate stats
        total_stats["pages_imported"] += 1
        total_stats["primitives"] += len(page_data.primitives)
        total_stats["text_items"] += text_count
        total_stats["curves"] += page_stats.get("curves", 0)
        total_stats["meshes"] += page_stats.get("meshes", 0)
        total_stats["circles"] += page_stats.get("circles", 0)
        total_stats["arcs"] += page_stats.get("arcs", 0)

    doc.close()
    _progress(1.0, "Import complete.")
    return total_stats
