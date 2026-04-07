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
import tempfile
import time
from typing import Callable, Dict, List, Optional

import bpy

from .dependency_manager import check_pymupdf, ensure_lib_path
from .pdfcadcore import (
    ImportConfig, extract_page, recognition, reset_ids,
    classify_page_content, tag_hatch_primitives, cleanup_primitives,
)
from .bl_geometry_builder import build_page
from .bl_text_builder import build_all_text

_MM_PER_PT = 25.4 / 72.0
_MM_TO_M = 0.001


def _iter_collection_tree(root_collection: bpy.types.Collection):
    stack = [root_collection]
    seen = set()
    while stack:
        col = stack.pop()
        if col is None:
            continue
        key = id(col)
        if key in seen:
            continue
        seen.add(key)
        yield col
        try:
            stack.extend(list(col.children))
        except Exception:
            pass


def _find_layer_collection(layer_col, target_collection):
    if layer_col is None:
        return None
    try:
        if layer_col.collection == target_collection:
            return layer_col
    except Exception:
        pass
    for child in getattr(layer_col, "children", []):
        found = _find_layer_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _unhide_collection_tree(root_collection: bpy.types.Collection) -> None:
    scene = bpy.context.scene
    for col in _iter_collection_tree(root_collection):
        try:
            col.hide_viewport = False
        except Exception:
            pass
        try:
            col.hide_render = False
        except Exception:
            pass

        for view_layer in scene.view_layers:
            try:
                layer_col = _find_layer_collection(view_layer.layer_collection, col)
                if layer_col is None:
                    continue
                layer_col.exclude = False
                layer_col.hide_viewport = False
                layer_col.holdout = False
                layer_col.indirect_only = False
            except Exception:
                continue


def _world_bounds_for_objects(objects):
    try:
        from mathutils import Vector
    except Exception:
        return None, None

    min_v = None
    max_v = None
    for obj in objects:
        if obj is None:
            continue
        if getattr(obj, "type", "") in {"CAMERA", "LIGHT"}:
            continue
        try:
            corners = obj.bound_box
            mw = obj.matrix_world
        except Exception:
            continue
        if not corners:
            continue
        try:
            world_pts = [mw @ Vector((c[0], c[1], c[2])) for c in corners]
        except Exception:
            continue
        for p in world_pts:
            if min_v is None:
                min_v = Vector((p.x, p.y, p.z))
                max_v = Vector((p.x, p.y, p.z))
            else:
                min_v.x = min(min_v.x, p.x)
                min_v.y = min(min_v.y, p.y)
                min_v.z = min(min_v.z, p.z)
                max_v.x = max(max_v.x, p.x)
                max_v.y = max(max_v.y, p.y)
                max_v.z = max(max_v.z, p.z)
    return min_v, max_v


def _close(a: float, b: float, tol: float = 1.0e-4) -> bool:
    return abs(float(a) - float(b)) <= tol


def _is_default_startup_cube(obj) -> bool:
    try:
        if obj is None or getattr(obj, "type", "") != "MESH":
            return False
        if getattr(obj, "name", "") != "Cube":
            return False
        if getattr(obj, "parent", None) is not None:
            return False
        loc = obj.location
        rot = obj.rotation_euler
        scl = obj.scale
        dims = obj.dimensions
        if not (_close(loc.x, 0.0) and _close(loc.y, 0.0) and _close(loc.z, 0.0)):
            return False
        if not (_close(rot.x, 0.0) and _close(rot.y, 0.0) and _close(rot.z, 0.0)):
            return False
        if not (_close(scl.x, 1.0) and _close(scl.y, 1.0) and _close(scl.z, 1.0)):
            return False
        if not (_close(dims.x, 2.0, 1.0e-3) and _close(dims.y, 2.0, 1.0e-3) and _close(dims.z, 2.0, 1.0e-3)):
            return False
        return True
    except Exception:
        return False


def _auto_hide_default_cube(scene) -> int:
    if scene is None:
        return 0
    hidden = 0
    try:
        for obj in scene.objects:
            if not _is_default_startup_cube(obj):
                continue
            try:
                obj.hide_set(True)
            except Exception:
                pass
            try:
                obj.hide_viewport = True
            except Exception:
                pass
            hidden += 1
    except Exception:
        return hidden
    return hidden


def _text_item_profile(text_items) -> Dict[str, int]:
    total = 0
    longish = 0
    alpha = 0
    for item in text_items or []:
        raw = getattr(item, "text", "")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            continue
        total += 1
        if any(ch.isalpha() for ch in text):
            alpha += 1
        # Narrative/doc-style text runs are usually long and/or multi-word.
        if len(text) >= 18 or text.count(" ") >= 2:
            longish += 1
    return {"total": total, "longish": longish, "alpha": alpha}


def _looks_like_text_cloud_page(primitives_count: int, text_items) -> bool:
    profile = _text_item_profile(text_items)
    total = profile["total"]
    longish = profile["longish"]
    alpha = profile["alpha"]
    if total < 180 or longish < 40:
        return False

    long_ratio = longish / float(max(total, 1))
    text_to_vector_ratio = total / float(max(primitives_count, 1))
    alpha_ratio = alpha / float(max(total, 1))

    # Typical CAD drawings have lots of short tokens (fractions, IDs).
    # Narrative map/plan pages tend to have many longer, multi-word runs.
    if long_ratio >= 0.28 and alpha_ratio >= 0.55 and text_to_vector_ratio >= 2.5:
        return True

    # Heavy pages with extreme primitive counts can hang; prefer raster when
    # they are also text-heavy.
    if primitives_count >= 12000 and total >= 300 and long_ratio >= 0.20:
        return True

    return False


def _primitive_bbox_area_ratio(prim, page_area_mm2: float) -> float:
    if page_area_mm2 <= 1e-9:
        return 0.0
    try:
        if getattr(prim, "bbox", None):
            x0, y0, x1, y1 = prim.bbox
            return max(0.0, (abs(float(x1) - float(x0)) * abs(float(y1) - float(y0))) / page_area_mm2)
    except Exception:
        pass
    try:
        pts = list(getattr(prim, "points", []) or [])
        if len(pts) >= 3:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            return max(0.0, ((max(xs) - min(xs)) * (max(ys) - min(ys))) / page_area_mm2)
    except Exception:
        pass
    return 0.0


def _looks_like_page_frame_only(page_data) -> bool:
    prims = list(getattr(page_data, "primitives", []) or [])
    if not prims or len(prims) > 12:
        return False
    text_count = len(list(getattr(page_data, "text_items", []) or []))
    if text_count > 12:
        return False
    page_area = max(float(getattr(page_data, "width", 0.0) or 0.0) * float(getattr(page_data, "height", 0.0) or 0.0), 1.0)
    big_frames = 0
    for prim in prims:
        ratio = _primitive_bbox_area_ratio(prim, page_area)
        if ratio >= 0.88:
            big_frames += 1
    return big_frames >= 1


def _focus_view_on_import(
    root_collection: bpy.types.Collection,
    keep_selected: bool = False,
    prefer_material_preview: bool = False,
) -> bool:
    """
    Select imported objects and frame them in all visible VIEW_3D areas.
    Returns True when at least one 3D view was focused.
    """
    if root_collection is None:
        return False

    _unhide_collection_tree(root_collection)

    objects = []
    try:
        objects = [obj for obj in root_collection.all_objects if obj is not None]
    except Exception:
        # Fallback traversal for older Blender collection APIs.
        stack = [root_collection]
        seen_cols = set()
        while stack:
            col = stack.pop()
            if col is None or id(col) in seen_cols:
                continue
            seen_cols.add(id(col))
            try:
                objects.extend([obj for obj in col.objects if obj is not None])
            except Exception:
                pass
            try:
                stack.extend(list(col.children))
            except Exception:
                pass

    if not objects:
        return False

    # Ensure imported objects are visible before focusing.
    visible_objects = []
    for obj in objects:
        try:
            obj.hide_set(False)
        except Exception:
            pass
        try:
            obj.hide_viewport = False
        except Exception:
            pass
        try:
            obj.hide_render = False
        except Exception:
            pass
        visible_objects.append(obj)

    min_v, max_v = _world_bounds_for_objects(visible_objects)

    view_layer = bpy.context.view_layer

    # Select all imported objects for framing.
    try:
        for obj in view_layer.objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
    except Exception:
        pass

    selected = []
    for obj in visible_objects:
        try:
            obj.select_set(True)
            selected.append(obj)
        except Exception:
            continue

    if not selected:
        return False

    try:
        view_layer.objects.active = selected[0]
    except Exception:
        pass

    focused = False
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(
                    window=window,
                    screen=screen,
                    area=area,
                    region=region,
                    scene=bpy.context.scene,
                    view_layer=view_layer,
                ):
                    # If viewport is in Local View isolation, imported objects can
                    # exist in outliner but remain invisible. Exit isolation first.
                    try:
                        space = area.spaces.active
                        # Viewport-local collection isolation can hide imported
                        # collections even though they appear in the outliner.
                        if hasattr(space, "use_local_collections"):
                            space.use_local_collections = False
                        if getattr(space, "local_view", None) is not None:
                            bpy.ops.view3d.localview(frame_selected=False)
                        # Expand clip range so large drawings cannot disappear.
                        if min_v is not None and max_v is not None:
                            span_x = abs(max_v.x - min_v.x)
                            span_y = abs(max_v.y - min_v.y)
                            span_z = abs(max_v.z - min_v.z)
                            radius = max(span_x, span_y, span_z, 0.25)
                            space.clip_start = max(1.0e-5, min(float(space.clip_start), radius / 10000.0))
                            space.clip_end = max(float(space.clip_end), radius * 200.0, 1000.0)
                    except Exception:
                        pass

                    try:
                        bpy.ops.view3d.view_axis(type="TOP", align_active=False)
                    except Exception:
                        pass
                    # Prefer orthographic for plan-like PDF drawings.
                    try:
                        rv3d = area.spaces.active.region_3d
                        if rv3d is not None:
                            rv3d.view_perspective = "ORTHO"
                            if min_v is not None and max_v is not None:
                                center = (min_v + max_v) * 0.5
                                span_x = abs(max_v.x - min_v.x)
                                span_y = abs(max_v.y - min_v.y)
                                span_z = abs(max_v.z - min_v.z)
                                radius = max(span_x, span_y, span_z, 0.25)
                                rv3d.view_location = center
                                rv3d.view_distance = max(radius * 1.35, 0.4)
                        if prefer_material_preview:
                            try:
                                space.shading.type = "MATERIAL"
                                if hasattr(space.shading, "color_type"):
                                    space.shading.color_type = "TEXTURE"
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # Re-frame after switching orientation.
                    try:
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                    except Exception:
                        pass
                focused = True
            except Exception:
                continue

    # Fallback framing pass: try view_all in each 3D area when selected framing
    # didn't succeed (file-browser context can be finicky on some setups).
    if not focused:
        for window in wm.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type != "VIEW_3D":
                    continue
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(
                        window=window,
                        screen=screen,
                        area=area,
                        region=region,
                        scene=bpy.context.scene,
                        view_layer=view_layer,
                    ):
                        try:
                            space = area.spaces.active
                            if hasattr(space, "use_local_collections"):
                                space.use_local_collections = False
                            if getattr(space, "local_view", None) is not None:
                                bpy.ops.view3d.localview(frame_selected=False)
                        except Exception:
                            pass
                        bpy.ops.view3d.view_axis(type="TOP", align_active=False)
                        try:
                            rv3d = area.spaces.active.region_3d
                            if rv3d is not None:
                                rv3d.view_perspective = "ORTHO"
                                if min_v is not None and max_v is not None:
                                    center = (min_v + max_v) * 0.5
                                    span_x = abs(max_v.x - min_v.x)
                                    span_y = abs(max_v.y - min_v.y)
                                    span_z = abs(max_v.z - min_v.z)
                                    radius = max(span_x, span_y, span_z, 0.25)
                                    rv3d.view_location = center
                                    rv3d.view_distance = max(radius * 1.35, 0.4)
                            if prefer_material_preview:
                                try:
                                    space.shading.type = "MATERIAL"
                                    if hasattr(space.shading, "color_type"):
                                        space.shading.color_type = "TEXTURE"
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            bpy.ops.view3d.view_all(center=False)
                        except Exception:
                            pass
                    focused = True
                except Exception:
                    continue

    if not keep_selected:
        # Imported objects are selected only for framing; clear selection after.
        for obj in objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
        try:
            view_layer.objects.active = None
        except Exception:
            pass

    return focused


def _extract_image_placements(doc, page, page_num: int, import_cfg, image_dir: str) -> list[dict]:
    """Extract embedded image XObjects and map them into page coordinates (mm)."""
    placements: list[dict] = []
    if not image_dir:
        return placements

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    page_height = float(page.rect.height)
    seen_xrefs: set[int] = set()

    for img_info in page.get_images(full=True):
        xref = int(img_info[0])
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)

        try:
            pix = fitz.Pixmap(doc, xref)
            color_space_n = None
            try:
                color_space_n = int(getattr(getattr(pix, "colorspace", None), "n", 0))
            except (TypeError, ValueError):
                color_space_n = None

            needs_rgb = pix.alpha or pix.n != 3 or (color_space_n is not None and color_space_n != 3)
            if needs_rgb:
                pix = fitz.Pixmap(fitz.csRGB, pix)

            image_path = os.path.join(image_dir, f"page_{page_num:03d}_xref_{xref}.png")
            pix.save(image_path)
        except (RuntimeError, OSError, ValueError, TypeError):
            continue

        rects = page.get_image_rects(xref)
        for rect in rects:
            x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
            left = min(x0, x1)
            right = max(x0, x1)

            if import_cfg.flip_y:
                bottom_pt = page_height - max(y0, y1)
                top_pt = page_height - min(y0, y1)
            else:
                bottom_pt = min(y0, y1)
                top_pt = max(y0, y1)

            placements.append(
                {
                    "path": image_path,
                    "x_mm": left * _MM_PER_PT * import_cfg.user_scale,
                    "y_mm": bottom_pt * _MM_PER_PT * import_cfg.user_scale,
                    "width_mm": (right - left) * _MM_PER_PT * import_cfg.user_scale,
                    "height_mm": (top_pt - bottom_pt) * _MM_PER_PT * import_cfg.user_scale,
                    "xref": xref,
                    "page_number": page_num,
                }
            )

    return placements


def _render_page_raster(page, page_num: int, import_cfg, image_dir: str) -> Optional[dict]:
    """Render entire page to raster and place as one aligned image plane."""
    if not image_dir:
        return None

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    dpi = int(max(36, getattr(import_cfg, "raster_dpi", 200) or 200))
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    try:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image_path = os.path.join(image_dir, f"page_{page_num:03d}_raster_{dpi}dpi.png")
        pix.save(image_path)
    except (RuntimeError, OSError, ValueError, TypeError):
        return None

    width_mm = float(page.rect.width) * _MM_PER_PT * import_cfg.user_scale
    height_mm = float(page.rect.height) * _MM_PER_PT * import_cfg.user_scale
    return {
        "path": image_path,
        "x_mm": 0.0,
        "y_mm": 0.0,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "xref": -1,
        "page_number": page_num,
    }


def _create_image_plane(
    placement: dict,
    collection: bpy.types.Collection,
    z_offset_m: float = 0.0,
) -> Optional[bpy.types.Object]:
    """Create a textured mesh plane for an extracted/rasterized PDF image."""
    path = placement.get("path")
    if not path or not os.path.isfile(path):
        return None

    x = float(placement.get("x_mm", 0.0)) * _MM_TO_M
    y = float(placement.get("y_mm", 0.0)) * _MM_TO_M
    width = max(float(placement.get("width_mm", 0.0)) * _MM_TO_M, 1e-9)
    height = max(float(placement.get("height_mm", 0.0)) * _MM_TO_M, 1e-9)
    page_num = int(placement.get("page_number", 0))
    xref = int(placement.get("xref", -1))

    mesh = bpy.data.meshes.new(f"PDF_ImgMesh_{page_num}_{xref}")
    verts = [
        (x, y, z_offset_m),
        (x + width, y, z_offset_m),
        (x + width, y + height, z_offset_m),
        (x, y + height, z_offset_m),
    ]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    try:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        if uv_layer is not None:
            uv_by_vert = {
                0: (0.0, 0.0),
                1: (1.0, 0.0),
                2: (1.0, 1.0),
                3: (0.0, 1.0),
            }
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    v_idx = mesh.loops[loop_idx].vertex_index
                    uv_layer.data[loop_idx].uv = uv_by_vert.get(v_idx, (0.0, 0.0))
    except Exception:
        pass

    obj = bpy.data.objects.new(f"PDF_Image_{page_num}_{xref}", mesh)
    collection.objects.link(obj)

    mat_name = f"PDF_Image_Mat_{page_num}_{xref}"
    material = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    tex = nodes.new(type="ShaderNodeTexImage")
    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
    out = nodes.new(type="ShaderNodeOutputMaterial")

    image = bpy.data.images.load(path, check_existing=True)
    tex.image = image
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    material.blend_method = "HASHED"
    if mesh.materials:
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)

    obj["pdf_image_path"] = path
    obj["pdf_xref"] = xref
    return obj


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
        text_mode = str(ui_config["text_mode"] or "none").strip().lower()
        config.text_mode = text_mode
        config.import_text = text_mode != "none"
        if "strict_text_fidelity" not in ui_config:
            # Keep strict text fidelity tied to geometry mode unless explicitly
            # overridden. Labels mode is more robust with relaxed fitting.
            config.strict_text_fidelity = text_mode == "geometry"
    if "strict_text_fidelity" in ui_config:
        config.strict_text_fidelity = bool(ui_config["strict_text_fidelity"])
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
    context=None,
) -> Dict[str, int]:
    """
    Import a PDF file into Blender. Main entry point.

    Args:
        filepath: Absolute path to the PDF file.
        config: Dict with keys like 'preset', 'pages', 'text_mode',
                'detect_arcs', 'make_faces', 'group_by_color', 'map_dashes'.
        progress_callback: Optional callable(progress_float, message_str).
        context: Optional bpy.context for Blender window-manager progress bar.
                 Pass None for CLI/headless mode.

    Returns:
        Stats dict with keys: pages, primitives, text_items, collections,
        elapsed, curves, meshes, circles, arcs, pages_imported.

    Raises:
        RuntimeError: If PyMuPDF is not available.
        FileNotFoundError: If the PDF file does not exist.
    """
    if config is None:
        config = {}

    visual_style = str(config.get("visual_style", "source") or "source").strip().lower()
    if visual_style not in {"source", "blueprint", "high_contrast"}:
        visual_style = "source"
    line_z_offset_m = float(config.get("line_z_offset_mm", 0.10) or 0.10) * _MM_TO_M
    text_z_offset_m = float(config.get("text_z_offset_mm", 0.35) or 0.35) * _MM_TO_M
    image_z_offset_m = float(config.get("image_z_offset_mm", 0.0) or 0.0) * _MM_TO_M
    auto_focus_view = bool(config.get("auto_focus_view", True))
    keep_selection_after_focus = bool(config.get("keep_selection_after_focus", False))
    auto_hide_default_cube = bool(config.get("auto_hide_default_cube", True))

    # Blender window-manager progress bar (safe when context is None)
    wm = None
    if context is not None:
        try:
            wm = context.window_manager
            wm.progress_begin(0, 100)
        except Exception:
            wm = None

    def _wm_progress(pct: float):
        """Update Blender's progress bar (0.0-1.0 -> 0-100)."""
        if wm is not None:
            try:
                wm.progress_update(int(pct * 100))
            except Exception:
                pass

    _last_redraw_t = [0.0]

    def _progress(pct: float, msg: str):
        if progress_callback:
            progress_callback(pct, msg)
        _wm_progress(pct)
        # Keep the UI responsive during long geometry/text loops.
        try:
            now = time.perf_counter()
            if (now - _last_redraw_t[0]) >= 0.12:
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
                _last_redraw_t[0] = now
        except Exception:
            pass

    t_start = time.perf_counter()

    try:
        # 1. Verify PyMuPDF is available
        _progress(0.0, "Checking dependencies...")
        if not check_pymupdf():
            raise RuntimeError(
                "PyMuPDF is not installed. Open addon preferences "
                "(Edit > Preferences > Add-ons > PDF Vector Importer) "
                "and click 'Install PyMuPDF'."
            )

        ensure_lib_path()
        try:
            import pymupdf as fitz  # PyMuPDF >= 1.24 preferred name
        except ImportError:
            import fitz  # Legacy fallback

        # 2. Verify file exists
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"PDF file not found: {filepath}")

        hidden_startup_cube = 0
        if auto_hide_default_cube:
            hidden_startup_cube = _auto_hide_default_cube(bpy.context.scene)

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
            elapsed = time.perf_counter() - t_start
            return {
                "pages_imported": 0, "pages": 0, "primitives": 0,
                "text_items": 0, "collections": 0, "elapsed": elapsed,
                "curves": 0, "meshes": 0, "circles": 0, "arcs": 0, "images": 0,
            }

        # 7. Create root collection
        basename = os.path.splitext(os.path.basename(filepath))[0]
        root_col = bpy.data.collections.new(f"PDF Import - {basename}")
        bpy.context.scene.collection.children.link(root_col)
        collections_created = 1  # root collection
        image_dir = tempfile.mkdtemp(prefix="bc_bl_pdf_images_") if not import_cfg.ignore_images else ""

        # 8. Build config dict for geometry builder
        builder_config = {
            "make_faces": import_cfg.make_faces,
            "ignore_fill_only_shapes": bool(config.get("ignore_fill_only_shapes", True)),
            "group_by_color": import_cfg.group_by_color,
            "map_dashes": import_cfg.map_dashes,
            "visual_style": visual_style,
            "line_z_offset_m": line_z_offset_m,
        }

        # 9. Process each page
        total_stats = {
            "pages_imported": 0, "primitives": 0, "text_items": 0,
            "curves": 0, "meshes": 0, "circles": 0, "arcs": 0, "images": 0,
            "skipped_fill_only": 0,
            "hidden_startup_cube": hidden_startup_cube,
        }
        raster_pages_imported = 0
        total_page_count = max(1, len(page_indices))

        def _page_progress(page_offset: int, stage: float) -> float:
            stage_clamped = max(0.0, min(1.0, float(stage)))
            base = 0.10 + 0.75 * (page_offset / total_page_count)
            span = 0.75 / total_page_count
            return min(0.95, base + span * stage_clamped)

        for i, page_idx in enumerate(page_indices):
            page_num = page_idx + 1
            _progress(_page_progress(i, 0.05), f"Extracting page {page_num}/{len(page_indices)}...")

            page = doc.load_page(page_idx)
            import_mode = (import_cfg.import_mode or "auto").strip().lower()

            # 9a. Auto-mode classification (before extraction)
            if import_mode == "auto":
                raw_drawings = page.get_drawings()
                text_blocks = page.get_text("blocks") or []
                text_words = page.get_text("words") or []
                classification = classify_page_content(
                    raw_drawings,
                    text_blocks_count=len(text_blocks),
                    text_words_count=len(text_words),
                )
                if classification["type"] in ("glyph_flood", "fill_art", "raster_candidate"):
                    _progress(
                        _page_progress(i, 0.15),
                        f"Auto-mode: {classification['reason']} — favoring raster import for page {page_num}",
                    )
                    import_mode = "raster"

            # 9b. Extract primitives via pdfcadcore
            page_data = extract_page(
                page, page_num,
                scale=import_cfg.user_scale,
                flip_y=import_cfg.flip_y,
                strict_text_fidelity=import_cfg.strict_text_fidelity,
            )
            _progress(_page_progress(i, 0.35), f"Parsed page {page_num}: {len(page_data.primitives)} primitives")

            # 9c. Geometry cleanup (remove micro-segments)
            if import_cfg.cleanup_level != "conservative" or import_cfg.min_seg_len > 0:
                cleanup_stats = cleanup_primitives(
                    page_data.primitives,
                    cleanup_level=import_cfg.cleanup_level,
                )
                if import_cfg.verbose and cleanup_stats.get("removed_micro", 0) > 0:
                    _progress(_page_progress(i, 0.45), f"Cleanup: removed "
                              f"{cleanup_stats['removed_micro']} micro-segments "
                              f"on page {page_num}")

            if (
                import_mode == "auto"
                and import_cfg.raster_fallback
                and _looks_like_text_cloud_page(len(page_data.primitives), page_data.text_items)
            ):
                profile = _text_item_profile(page_data.text_items)
                _progress(
                    _page_progress(i, 0.50),
                    f"Auto-mode: text-heavy page ({profile['total']} text / {len(page_data.primitives)} vectors) — raster for page {page_num}",
                )
                import_mode = "raster"

            # 9d. Hatch detection (post-extraction, on primitives)
            if import_cfg.hatch_mode != "import":
                hatch_ids = tag_hatch_primitives(page_data.primitives)
                if hatch_ids:
                    if import_cfg.hatch_mode == "skip":
                        page_data.primitives = [
                            p for p in page_data.primitives
                            if p.id not in hatch_ids
                        ]
                    elif import_cfg.hatch_mode == "group":
                        for p in page_data.primitives:
                            if p.id in hatch_ids:
                                p.generic_tags.append("hatch_line")

            # 9e. Optional recognition pass
            _progress(_page_progress(i, 0.55), f"Recognition pass on page {page_num}...")
            try:
                recognition.run(page_data, mode="auto")
            except Exception:
                # Recognition failure is non-fatal
                pass

            # 9f. Create page collection
            page_col = bpy.data.collections.new(f"PDF_Page_{page_num}")
            root_col.children.link(page_col)
            collections_created += 1

            # 9g. Build geometry
            page_stats = {"curves": 0, "meshes": 0, "circles": 0, "arcs": 0}
            if import_mode != "raster":
                _progress(_page_progress(i, 0.72), f"Building geometry for page {page_num}...")
                def _geom_progress(frac):
                    frac = max(0.0, min(1.0, float(frac)))
                    _progress(
                        _page_progress(i, 0.72 + (0.10 * frac)),
                        f"Building geometry for page {page_num}... ({int(frac * 100)}%)",
                    )
                page_stats = build_page(
                    page_data,
                    page_col,
                    builder_config,
                    progress_callback=_geom_progress,
                )

            # 9h. Build text objects
            text_count = 0
            if import_mode != "raster" and import_cfg.import_text and import_cfg.text_mode != "none":
                _progress(_page_progress(i, 0.82), f"Building text for page {page_num}...")
                def _text_progress(frac):
                    frac = max(0.0, min(1.0, float(frac)))
                    _progress(
                        _page_progress(i, 0.82 + (0.09 * frac)),
                        f"Building text for page {page_num}... ({int(frac * 100)}%)",
                    )
                text_count = build_all_text(
                    page_data.text_items,
                    page_col,
                    page_num,
                    visual_style=visual_style,
                    z_offset_m=text_z_offset_m,
                    strict_text_fidelity=import_cfg.strict_text_fidelity,
                    progress_callback=_text_progress,
                )

            # 9i. Build image/raster planes
            image_count = 0
            if not import_cfg.ignore_images:
                _progress(_page_progress(i, 0.92), f"Building images for page {page_num}...")
                placements = []
                if import_mode == "raster":
                    rendered = _render_page_raster(page, page_num, import_cfg, image_dir)
                    if rendered:
                        placements.append(rendered)
                else:
                    placements = _extract_image_placements(doc, page, page_num, import_cfg, image_dir)
                    if (
                        import_cfg.raster_fallback
                        and not placements
                        and (not page_data.primitives or _looks_like_page_frame_only(page_data))
                    ):
                        _progress(
                            _page_progress(i, 0.93),
                            f"Auto-mode: sparse vector shell on page {page_num} — raster fallback",
                        )
                        rendered = _render_page_raster(page, page_num, import_cfg, image_dir)
                        if rendered:
                            placements.append(rendered)

                for placement in placements:
                    if _create_image_plane(placement, page_col, z_offset_m=image_z_offset_m):
                        image_count += 1
            if import_mode == "raster" and image_count > 0:
                raster_pages_imported += 1

            # 9j. Accumulate stats
            total_stats["pages_imported"] += 1
            total_stats["primitives"] += len(page_data.primitives)
            total_stats["text_items"] += text_count
            total_stats["curves"] += page_stats.get("curves", 0)
            total_stats["meshes"] += page_stats.get("meshes", 0)
            total_stats["circles"] += page_stats.get("circles", 0)
            total_stats["arcs"] += page_stats.get("arcs", 0)
            total_stats["images"] += image_count
            total_stats["skipped_fill_only"] += page_stats.get("skipped_fill_only", 0)
            _progress(
                _page_progress(i, 1.0),
                f"Finished page {page_num}/{len(page_indices)} "
                f"({total_stats['primitives']} primitives, {total_stats['text_items']} text)",
            )

        doc.close()

        elapsed = time.perf_counter() - t_start
        _progress(1.0, "Import complete.")

        # Merge extended stats into return dict
        total_stats["pages"] = len(page_indices)
        total_stats["collections"] = collections_created
        try:
            total_stats["focused"] = (
                1
                if (
                    auto_focus_view
                    and _focus_view_on_import(
                        root_col,
                        keep_selected=keep_selection_after_focus,
                        prefer_material_preview=(raster_pages_imported > 0),
                    )
                )
                else 0
            )
        except Exception:
            total_stats["focused"] = 0
        total_stats["elapsed"] = elapsed
        return total_stats

    finally:
        if wm is not None:
            try:
                wm.progress_end()
            except Exception:
                pass
