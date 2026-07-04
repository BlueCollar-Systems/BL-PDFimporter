"""Blender host adapter: build native objects from extracted PDF data."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
from typing import Dict, Optional

from ..importer import run_import
from ..view_focus import focus_view_on_collection

MM_TO_M = 0.001
_TEXT_MODES = {"labels", "3d_text", "glyphs", "geometry"}


@dataclass
class BlenderImportOptions:
    pages: Optional[str] = None
    import_text: bool = True
    text_mode: str = "3d_text"
    import_images: bool = True
    group_by_layer: bool = True
    group_by_color: bool = True


def import_into_blender(pdf_path: str, mode: str = "auto",
                        options: Optional[BlenderImportOptions] = None):
    try:
        import bpy
    except ImportError as exc:  # pragma: no cover - Blender runtime only
        raise RuntimeError("Blender Python API (bpy) is required for this adapter.") from exc

    opts = options or BlenderImportOptions()
    overrides = {
        "import_text": opts.import_text,
        "text_mode": opts.text_mode,
        "ignore_images": not opts.import_images,
    }
    if opts.pages:
        overrides["pages"] = opts.pages

    run = run_import(pdf_path, mode=mode, overrides=overrides)
    extraction = run.extraction
    provenance_opts = run.config

    root_name = f"PDF_{Path(pdf_path).stem}"
    root_collection = _ensure_root_collection(root_name)

    mat_cache: Dict[str, object] = {}

    for page in extraction.pages:
        page_collection = bpy.data.collections.new(f"Page_{page.page_data.page_number:03d}")
        root_collection.children.link(page_collection)
        layer_cache: Dict[str, object] = {}

        for primitive in page.page_data.primitives:
            target_collection = page_collection
            if opts.group_by_layer and primitive.layer_name:
                layer_key = _safe_name(str(primitive.layer_name))
                target_collection = layer_cache.get(layer_key)
                if target_collection is None:
                    target_collection = bpy.data.collections.new(layer_key)
                    page_collection.children.link(target_collection)
                    layer_cache[layer_key] = target_collection

            obj = _create_curve_object(primitive, target_collection)
            if obj is None:
                continue

            if primitive.stroke_color:
                color_key = ",".join(f"{c:.3f}" for c in primitive.stroke_color)
                material = _material_for_color(color_key, primitive.stroke_color, mat_cache)
                _assign_material(obj, material)

            if opts.group_by_color and primitive.stroke_color:
                obj["pdf_color_group"] = color_key
            if primitive.layer_name:
                obj["pdf_layer"] = str(primitive.layer_name)
            obj["pdf_primitive_id"] = int(primitive.id)
            obj["pdf_primitive_type"] = str(primitive.type)

        if opts.import_text:
            for text in page.page_data.text_items:
                _create_text_object(
                    text,
                    page_collection,
                    opts.text_mode,
                    page.page_data.page_number,
                    provenance_opts=provenance_opts,
                )

        if opts.import_images:
            for placement in page.images:
                if os.path.isfile(placement.path):
                    _create_image_plane(placement, page_collection)

    try:
        focus_view_on_collection(root_collection)
    except Exception:
        pass

    return extraction


def _ensure_root_collection(name: str):
    import bpy

    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _create_curve_object(primitive, collection):
    import bpy

    if not primitive.points or len(primitive.points) < 2:
        return None

    curve = bpy.data.curves.new(name=f"Prim_{primitive.id}", type="CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(len(primitive.points) - 1)

    for idx, (x_mm, y_mm) in enumerate(primitive.points):
        spline.points[idx].co = (x_mm * MM_TO_M, y_mm * MM_TO_M, 0.0, 1.0)

    if primitive.closed:
        spline.use_cyclic_u = True

    obj = bpy.data.objects.new(f"PDF_{primitive.id}", curve)
    collection.objects.link(obj)
    return obj


# Unicode symbols that Blender's built-in font can't render (shows as tofu boxes).
# Map to ASCII equivalents for readability.
_UNICODE_SUBSTITUTIONS = {
    "\u2190": "<-",   # ←
    "\u2191": "^",    # ↑
    "\u2192": "->",   # →
    "\u2193": "v",    # ↓
    "\u25C0": "<",    # ◀
    "\u25B6": ">",    # ▶
    "\u25CF": "*",    # ●
    "\u2022": "*",    # •
    "\u00B7": ".",    # ·
    "\u2013": "-",    # –
    "\u2014": "--",   # —
    "\u2015": "--",   # ―
    "\u2018": "'",    # '
    "\u2019": "'",    # '
    "\u201C": '"',    # "
    "\u201D": '"',    # "
    "\u2032": "'",    # ′ (prime / feet)
    "\u2033": '"',    # ″ (double prime / inches)
    "\u00D8": "DIA",  # Ø (diameter)
}


def _sanitize_text_for_blender(text: str) -> str:
    """Replace unicode symbols that Blender's default font can't render."""
    for char, replacement in _UNICODE_SUBSTITUTIONS.items():
        if char in text:
            text = text.replace(char, replacement)
    # Strip any remaining chars outside BMP or in Private Use Area
    # that would render as tofu boxes
    cleaned = []
    for c in text:
        cp = ord(c)
        if cp > 0xFFFF:  # Supplementary planes (emoji, etc.)
            continue
        if 0xE000 <= cp <= 0xF8FF:  # Private Use Area
            continue
        if cp == 0xFFFD:  # Replacement character
            continue
        cleaned.append(c)
    return "".join(cleaned)


def _normalize_text_mode(text_mode: str) -> str:
    mode = (text_mode or "3d_text").strip().lower()
    if mode in _TEXT_MODES:
        return mode
    return "3d_text"


def _text_extrusion_depth(font_size: float) -> float:
    return max(font_size * 0.12, 0.00025)


def _create_text_object(text_item, collection, text_mode="3d_text", page_number=0, provenance_opts=None):
    import bpy

    mode = _normalize_text_mode(text_mode)
    body = _sanitize_text_for_blender(str(getattr(text_item, "text", "") or ""))
    if not body.strip():
        return None

    text_id = int(getattr(text_item, "id", 0) or 0)
    curve = bpy.data.curves.new(name=f"P{page_number}_text_{mode}_{text_id}", type="FONT")
    curve.body = body
    curve.size = max(float(getattr(text_item, "font_size", 1.0) or 1.0) * MM_TO_M, 0.001)
    curve.align_x = "LEFT"
    try:
        curve.align_y = "BOTTOM_BASELINE"
    except Exception:
        curve.align_y = "BOTTOM"
    if mode == "3d_text":
        curve.extrude = _text_extrusion_depth(curve.size)
    else:
        curve.extrude = 0.0
    if mode in {"glyphs", "geometry"}:
        try:
            curve.resolution_u = max(int(getattr(curve, "resolution_u", 12) or 12), 24)
        except Exception:
            pass

    obj = bpy.data.objects.new(f"PDF_Text_{mode}_{text_id}", curve)
    insertion = getattr(text_item, "insertion", (0.0, 0.0)) or (0.0, 0.0)
    obj.location = (
        float(insertion[0]) * MM_TO_M,
        float(insertion[1]) * MM_TO_M,
        0.0,
    )
    obj.rotation_euler = (
        0.0,
        0.0,
        math.radians(float(getattr(text_item, "rotation", 0.0) or 0.0)),
    )
    obj["pdf_text_id"] = text_id
    obj["pdf_text_mode"] = mode
    collection.objects.link(obj)
    if mode in {"glyphs", "geometry"}:
        obj = _meshify_text_object(obj, collection, mode)
    if provenance_opts is not None:
        try:
            from pdf_vector_importer.pdfcadcore.source_provenance import record_text_span_provenance

            bbox = getattr(text_item, "bbox", None)
            insertion = getattr(text_item, "insertion", (0.0, 0.0)) or (0.0, 0.0)
            entity_type = "outline_curve_or_mesh" if mode in {"glyphs", "geometry"} else "native_3d_text"
            record_text_span_provenance(
                provenance_opts,
                page=int(page_number),
                span={
                    "text": body,
                    "bbox": list(bbox) if bbox else None,
                    "origin": [float(insertion[0]), float(insertion[1])],
                    "size": float(getattr(text_item, "font_size", 0.0) or 0.0),
                },
                text=body,
                created_entity_type=entity_type,
                parent_handle=str(getattr(obj, "name", "") or ""),
                import_mode=str(getattr(provenance_opts, "import_mode", "") or ""),
                text_mode=mode,
            )
        except (ImportError, TypeError, ValueError):
            pass
    return obj


def _meshify_text_object(obj, collection, mode: str):
    """Convert text curves to mesh geometry when Blender can evaluate them."""
    import bpy

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
        mesh.name = f"{obj.name}_mesh"
        mesh_obj = bpy.data.objects.new(obj.name, mesh)
        mesh_obj.matrix_world = obj.matrix_world.copy()
        mesh_obj.color = obj.color
        for mat in getattr(obj.data, "materials", []):
            mesh.materials.append(mat)
        mesh_obj["pdf_text_mode"] = mode
        mesh_obj["pdf_text_source"] = getattr(obj.data, "body", "")
        collection.objects.link(mesh_obj)
        try:
            collection.objects.unlink(obj)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
        return mesh_obj
    except Exception:
        return obj


def _create_image_plane(placement, collection):
    import bpy

    width = max(placement.width_mm * MM_TO_M, 1e-6)
    height = max(placement.height_mm * MM_TO_M, 1e-6)
    x = placement.x_mm * MM_TO_M
    y = placement.y_mm * MM_TO_M

    mesh = bpy.data.meshes.new(f"ImgMesh_{placement.page_number}_{placement.xref}")
    verts = [
        (x, y, 0.0),
        (x + width, y, 0.0),
        (x + width, y + height, 0.0),
        (x, y + height, 0.0),
    ]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()

    obj = bpy.data.objects.new(f"PDF_Image_{placement.page_number}_{placement.xref}", mesh)
    collection.objects.link(obj)

    material = bpy.data.materials.new(name=f"PDF_Image_Mat_{placement.xref}")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    tex = nodes.new(type="ShaderNodeTexImage")
    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
    output = nodes.new(type="ShaderNodeOutputMaterial")

    image = bpy.data.images.load(placement.path, check_existing=True)
    tex.image = image

    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    _assign_material(obj, material)


def _assign_material(obj, material):
    if getattr(obj.data, "materials", None) is None:
        return
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)


def _material_for_color(key: str, rgb, cache: Dict[str, object]):
    import bpy

    material = cache.get(key)
    if material is not None:
        return material

    material = bpy.data.materials.new(name=f"PDF_RGB_{key.replace(',', '_')}")
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
    cache[key] = material
    return material


def _safe_name(raw: str) -> str:
    keep = [ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw.strip()]
    name = "".join(keep).strip("_")
    return name or "Layer"
