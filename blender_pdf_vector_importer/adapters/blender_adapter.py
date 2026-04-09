"""Blender host adapter: build native objects from extracted PDF data."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
from typing import Dict, Optional

from ..importer import run_import

MM_TO_M = 0.001


@dataclass
class BlenderImportOptions:
    pages: Optional[str] = None
    import_text: bool = True
    import_images: bool = True
    detect_arcs: bool = True
    group_by_layer: bool = True
    group_by_color: bool = True


def import_into_blender(pdf_path: str, preset: str = "general",
                        options: Optional[BlenderImportOptions] = None):
    try:
        import bpy
    except ImportError as exc:  # pragma: no cover - Blender runtime only
        raise RuntimeError("Blender Python API (bpy) is required for this adapter.") from exc

    opts = options or BlenderImportOptions()
    overrides = {
        "import_text": opts.import_text,
        "text_mode": "labels" if opts.import_text else "none",
        "ignore_images": not opts.import_images,
        "detect_arcs": opts.detect_arcs,
    }
    if opts.pages:
        overrides["pages"] = opts.pages

    run = run_import(pdf_path, preset=preset, overrides=overrides)
    extraction = run.extraction

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
                _create_text_object(text, page_collection)

        if opts.import_images:
            for placement in page.images:
                if os.path.isfile(placement.path):
                    _create_image_plane(placement, page_collection)

    # Autofit viewport to imported geometry
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                with bpy.context.temp_override(area=area):
                    bpy.ops.view3d.view_all()
                break
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


def _create_text_object(text_item, collection):
    import bpy

    curve = bpy.data.curves.new(name=f"Text_{text_item.id}", type="FONT")
    curve.body = text_item.text
    curve.size = max(text_item.font_size * MM_TO_M, 0.001)

    obj = bpy.data.objects.new(f"PDF_Text_{text_item.id}", curve)
    obj.location = (
        text_item.insertion[0] * MM_TO_M,
        text_item.insertion[1] * MM_TO_M,
        0.0,
    )
    obj.rotation_euler = (0.0, 0.0, math.radians(text_item.rotation or 0.0))
    obj["pdf_text_id"] = int(text_item.id)
    collection.objects.link(obj)


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
