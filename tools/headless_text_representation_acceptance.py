"""Real-Blender acceptance for item-scoped PDF text representations.

Run with Blender, not CPython::

    blender --background --python tools/headless_text_representation_acceptance.py -- \
        Welding-Symbol-Chart.pdf AWSWeldSymbolchart.pdf
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
import types

import bpy
from mathutils import Vector


def _args() -> tuple[Path, Path]:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(values) != 2:
        raise SystemExit("expected Welding-Symbol-Chart.pdf and AWSWeldSymbolchart.pdf")
    paths = tuple(Path(value).expanduser().resolve() for value in values)
    if not all(path.is_file() for path in paths):
        raise SystemExit(f"missing acceptance input: {paths!r}")
    return paths  # type: ignore[return-value]


def _new_collection(name: str):
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def _remove_collection(collection) -> None:
    objects = list(collection.objects)
    object_ids = {id(obj) for obj in objects}

    def hierarchy_depth(obj) -> int:
        depth = 0
        parent = getattr(obj, "parent", None)
        while parent is not None and id(parent) in object_ids:
            depth += 1
            parent = getattr(parent, "parent", None)
        return depth

    for obj in sorted(objects, key=hierarchy_depth, reverse=True):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def _assert_delivery(record, requested: str, final: str) -> None:
    assert record["requested_representation"] == requested, record
    assert record["final_representation"] == final, record
    assert record["status"] == "delivered", record
    assert record["entity_ids"], record


def _delivery_entities(record, expected_type: str):
    entities = []
    for entity_id in record["entity_ids"]:
        obj = bpy.data.objects.get(str(entity_id))
        assert obj is not None, (entity_id, record)
        assert obj.type == expected_type, (entity_id, obj.type, expected_type)
        entities.append(obj)
    return entities


def _assert_points_close(actual, expected, tolerance: float = 1.0e-6) -> None:
    assert len(actual) == len(expected)
    assert all(
        math.isfinite(float(value)) for value in tuple(actual) + tuple(expected)
    )
    assert all(
        abs(float(left) - float(right)) <= tolerance
        for left, right in zip(actual, expected)  # noqa: B905
    ), (list(actual), list(expected))


def _assert_metric_entity(obj) -> None:
    """Prove the evaluated host transform still honors the PDF font axes."""
    assert bool(obj.get("pdf_metric_affine_applied", False)), obj.name
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    matrix = evaluated.matrix_world
    intended = [float(value) for value in obj.get("pdf_affine_matrix", [])]
    actual_matrix = [float(value) for row in matrix for value in row]
    assert len(intended) == 16
    _assert_points_close(actual_matrix, intended)

    local_advance = float(obj["pdf_metric_local_advance"])
    local_line_height = float(obj["pdf_metric_local_line_height"])
    local_baseline_y = float(obj.get("pdf_metric_local_baseline_y", 0.0) or 0.0)
    origin = [float(value) for value in obj["pdf_metric_target_origin_m"]]
    horizontal = [
        float(value) for value in obj["pdf_metric_target_horizontal_axis_m"]
    ]
    vertical = [
        float(value) for value in obj["pdf_metric_target_vertical_axis_m"]
    ]
    actual_origin = matrix @ Vector((0.0, local_baseline_y, 0.0))
    actual_advance = matrix @ Vector((local_advance, local_baseline_y, 0.0))
    actual_line = matrix @ Vector(
        (0.0, local_baseline_y + local_line_height, 0.0)
    )
    _assert_points_close(actual_origin[:2], origin)
    _assert_points_close(
        actual_advance[:2],
        [origin[0] + horizontal[0], origin[1] + horizontal[1]],
    )
    _assert_points_close(
        actual_line[:2],
        [origin[0] + vertical[0], origin[1] + vertical[1]],
    )
    carrier_name = str(obj.get("pdf_affine_carrier", "") or "")
    if carrier_name:
        assert obj.parent is not None
        assert obj.parent.name == carrier_name


def _assert_character_delivery(record, expected_type: str, source_text: str):
    entities = _delivery_entities(record, expected_type)
    delivered_attempt = next(
        attempt
        for attempt in reversed(record["attempts"])
        if attempt["status"] == "delivered"
    )
    character_entities = delivered_attempt["evidence"]["character_entities"]
    assert len(character_entities) == len(entities)
    assert "".join(str(item["text"]) for item in character_entities) == source_text
    for item, obj in zip(character_entities, entities):  # noqa: B905
        assert item["positioned_character"] is True
        assert item["entity_ids"] == [obj.name]
        verification = item["verification"]
        assert verification["metric_affine_applied"] is True
        _assert_points_close(
            verification["actual_baseline_anchor_m"],
            verification["expected_location_m"],
        )
        _assert_points_close(
            verification["actual_advance_endpoint_m"],
            verification["expected_advance_endpoint_m"],
        )
        _assert_points_close(
            verification["actual_line_axis_endpoint_m"],
            verification["expected_line_axis_endpoint_m"],
        )
        _assert_points_close(
            verification["evaluated_affine_matrix"],
            verification["intended_affine_matrix"],
        )
        _assert_metric_entity(obj)
    return entities


def main() -> None:
    welding_path, raster_path = _args()
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from pdf_vector_importer import bl_import_engine, bl_text_builder
    from pdf_vector_importer.dependency_manager import ensure_lib_path
    from pdf_vector_importer.pdfcadcore.fitz_loader import import_fitz
    from pdf_vector_importer.pdfcadcore.import_config import ImportConfig
    from pdf_vector_importer.pdfcadcore.embedded_fonts import EmbeddedFontFailure
    from pdf_vector_importer.pdfcadcore.primitive_extractor import extract_page
    from pdf_vector_importer.packed_assets import verify_packed_sha256

    ensure_lib_path()
    fitz = import_fitz()
    document = fitz.open(str(welding_path))
    page = document[0]
    page_data = extract_page(page, 1)
    exact_item = next(item for item in page_data.text_items if item.font_asset is not None)
    unresolved_items = [item for item in page_data.text_items if item.font_asset is None]
    assert not unresolved_items, [
        (item.id, item.font_name, item.font_failure)
        for item in unresolved_items
    ]
    missing_name = "BC_DeliberatelyAbsentAcceptanceFont"
    missing_item = replace(
        exact_item,
        id=9_000_017,
        font_name=missing_name,
        font_asset=None,
        font_failure=EmbeddedFontFailure(
            page_number=1,
            span_font_name=missing_name,
            reason="no_exact_embedded_font_match",
            proof_category="source_font_absent_for_item",
        ),
    )
    raster_dir = tempfile.mkdtemp(prefix="bc_bl_acceptance_text_")
    results = {
        "blender_version": list(bpy.app.version),
        "source_spans": len(page_data.text_items),
        "exact_item_font": exact_item.font_name,
        "exact_item_font_format": exact_item.font_asset.usable_format,
        "negative_fixture_font": missing_item.font_name,
        "modes": {},
    }

    config = ImportConfig.vector()
    config.raster_dpi = 180

    def terminal_raster(text_item, collection, page_number, item_id):
        return bl_import_engine._render_text_item_raster(
            page,
            text_item,
            collection,
            page_num=page_number,
            item_id=item_id,
            import_cfg=config,
            image_dir=raster_dir,
        )

    expected_types = {
        "text": "FONT",
        "3d_text": "FONT",
        "glyphs": "CURVE",
        "geometry": "MESH",
        "raster": "MESH",
    }
    for mode, expected_type in expected_types.items():
        collection = _new_collection(f"Acceptance_{mode}")
        opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
        obj = bl_text_builder.build_text(
            exact_item,
            collection,
            page_number=1,
            text_mode=mode,
            provenance_opts=opts,
            terminal_raster_callback=terminal_raster,
        )
        assert obj is not None and obj.type == expected_type, (
            mode,
            obj,
            opts._text_delivery_records[-1],
        )
        assert obj["pdf_text_mode"] == mode
        bpy.context.view_layer.update()
        record = opts._text_delivery_records[-1]
        _assert_delivery(record, mode, mode)
        dependency_graph_updates = record["attempts"][-1]["evidence"].get(
            "dependency_graph_updates"
        )
        if mode in {"glyphs", "geometry"}:
            assert dependency_graph_updates == 2, record
        if mode == "raster":
            entities = _delivery_entities(record, expected_type)
        else:
            entities = _assert_character_delivery(record, expected_type, exact_item.text)
        if mode in {"text", "3d_text"}:
            assert "".join(entity.data.body for entity in entities) == exact_item.text
            assert all(
                (entity.data.extrude > 0.0) is (mode == "3d_text")
                for entity in entities
            )
        elif mode == "glyphs":
            assert all(len(entity.data.splines) > 0 for entity in entities)
        elif mode == "geometry":
            assert all(len(entity.data.vertices) > 0 for entity in entities)
        results["modes"][mode] = {
            "object_type": obj.type,
            "entity_id": obj.name,
            "entity_count": len(entities),
            "attempt_count": len(record["attempts"]),
            "dependency_graph_updates": dependency_graph_updates,
        }
        _remove_collection(collection)

    label_collection = _new_collection("Acceptance_labels")
    label_opts = types.SimpleNamespace(import_mode="vector", text_mode="labels")
    label_obj = bl_text_builder.build_text(
        exact_item,
        label_collection,
        page_number=1,
        text_mode="labels",
        provenance_opts=label_opts,
        terminal_raster_callback=terminal_raster,
    )
    assert label_obj is not None and label_obj.type == "FONT"
    label_record = label_opts._text_delivery_records[-1]
    _assert_delivery(label_record, "labels", "text")
    assert label_record["attempts"][0]["status"] == "impossible"
    label_entities = _assert_character_delivery(label_record, "FONT", exact_item.text)
    assert "".join(entity.data.body for entity in label_entities) == exact_item.text
    results["modes"]["labels"] = {
        "object_type": label_obj.type,
        "entity_count": len(label_entities),
        "final": "text",
        "reason": label_record["attempts"][0]["reason"],
    }
    _remove_collection(label_collection)

    fallback_collection = _new_collection("Acceptance_missing_font")
    fallback_opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")
    fallback_obj = bl_text_builder.build_text(
        missing_item,
        fallback_collection,
        page_number=1,
        text_mode="3d_text",
        provenance_opts=fallback_opts,
        terminal_raster_callback=terminal_raster,
    )
    assert fallback_obj is not None and fallback_obj.type == "MESH"
    fallback_record = fallback_opts._text_delivery_records[-1]
    _assert_delivery(fallback_record, "3d_text", "raster")
    assert [attempt["attempted_representation"] for attempt in fallback_record["attempts"]] == [
        "3d_text",
        "text",
        "glyphs",
        "geometry",
        "raster",
    ]
    results["missing_font_fallback"] = {
        "final": fallback_record["final_representation"],
        "attempts": [attempt["status"] for attempt in fallback_record["attempts"]],
    }
    _remove_collection(fallback_collection)
    document.close()

    raster_document = fitz.open(str(raster_path))
    raster_page = raster_document[0]
    raster_config = ImportConfig.raster()
    raster_config.raster_dpi = 120
    page_dir = tempfile.mkdtemp(prefix="bc_bl_acceptance_page_")
    placement = bl_import_engine._render_page_raster(
        raster_page, 1, raster_config, page_dir
    )
    assert placement is not None and Path(placement["path"]).is_file()
    page_collection = _new_collection("Acceptance_rotated_raster_page")
    page_obj = bl_import_engine._create_image_plane(placement, page_collection)
    assert page_obj is not None and page_obj.type == "MESH"
    results["rotated_image_only_page"] = {
        "rotation": int(raster_page.rotation),
        "object_type": page_obj.type,
        "width_mm": placement["width_mm"],
        "height_mm": placement["height_mm"],
    }
    raster_object_name = page_obj.name
    raster_image_name = str(page_obj["pdf_image_datablock"])
    raster_image_sha = str(page_obj["pdf_image_sha256"])
    raster_image = bpy.data.images.get(raster_image_name)
    assert raster_image is not None
    assert verify_packed_sha256(raster_image, raster_image_sha) == raster_image_sha
    raster_document.close()

    full_dir = Path(tempfile.mkdtemp(prefix="bc_bl_acceptance_full_"))
    report_path = full_dir / "welding-import-report.json"
    full_stats = bl_import_engine.import_pdf(
        str(welding_path),
        config={
            "mode": "vector",
            "pages": "1",
            "import_text": True,
            "text_mode": "3d_text",
            "ignore_images": True,
            "visual_style": "source",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
            "import_report_path": str(report_path),
        },
    )
    assert full_stats["text_source_spans"] == len(page_data.text_items)
    assert full_stats["text_delivery_source_items"] == len(page_data.text_items)
    assert full_stats["text_delivery_delivered_items"] == len(page_data.text_items)
    assert full_stats["text_delivery_failed_items"] == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    delivery = report["extra"]["text_delivery"]
    summary = delivery["summary"]
    assert summary["source_items"] == len(page_data.text_items)
    assert summary["delivered_items"] == len(page_data.text_items)
    assert summary["failed_items"] == 0
    final_counts = summary["final_counts"]
    assert int(final_counts.get("3d_text", 0)) == len(page_data.text_items)
    assert int(final_counts.get("raster", 0)) == 0
    assert summary["fallback_items"] == 0
    source_text_by_id = {int(item.id): str(item.text) for item in page_data.text_items}
    full_records = delivery["items"]
    full_entity_names = []
    font_cache_files = set()
    for record in full_records:
        _assert_delivery(record, "3d_text", "3d_text")
        assert record["final_state_verification"]["status"] == "verified", record
        source_text = source_text_by_id[int(record["source_span_id"])]
        entities = _assert_character_delivery(record, "FONT", source_text)
        full_entity_names.extend(entity.name for entity in entities)
        for entity in entities:
            expected_font_sha = str(entity["pdf_exact_font_sha256"])
            assert verify_packed_sha256(entity.data.font, expected_font_sha) == expected_font_sha
            font_path = Path(bpy.path.abspath(entity.data.font.filepath)).resolve()
            if font_path.parent.name == "bc_bl_pdf_exact_fonts":
                font_cache_files.add(font_path)

    blend_path = full_dir / "welding-and-rotated-raster-persistence.blend"
    save_result = bpy.ops.wm.save_as_mainfile(
        filepath=str(blend_path),
        check_existing=False,
    )
    assert "FINISHED" in save_result
    assert blend_path.is_file()

    shutil.rmtree(raster_dir)
    shutil.rmtree(page_dir)
    for font_path in sorted(font_cache_files):
        font_path.unlink(missing_ok=True)
    assert not Path(raster_dir).exists()
    assert not Path(page_dir).exists()
    assert all(not path.exists() for path in font_cache_files)
    assert welding_path.is_file() and raster_path.is_file()

    reopen_result = bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    assert "FINISHED" in reopen_result
    bpy.context.view_layer.update()
    reopened_entity_count = 0
    for record in full_records:
        entities = _delivery_entities(record, "FONT")
        reopened_entity_count += len(entities)
        for entity in entities:
            _assert_metric_entity(entity)
            expected_font_sha = str(entity["pdf_exact_font_sha256"])
            assert verify_packed_sha256(entity.data.font, expected_font_sha) == expected_font_sha
    assert reopened_entity_count == len(full_entity_names)

    reopened_raster = bpy.data.objects.get(raster_object_name)
    assert reopened_raster is not None and reopened_raster.type == "MESH"
    reopened_image = bpy.data.images.get(str(reopened_raster["pdf_image_datablock"]))
    assert reopened_image is not None
    assert verify_packed_sha256(reopened_image, raster_image_sha) == raster_image_sha
    assert not Path(raster_dir).exists()
    assert not Path(page_dir).exists()
    assert all(not path.exists() for path in font_cache_files)
    results["full_welding_import"] = {
        "source_items": summary["source_items"],
        "delivered_items": summary["delivered_items"],
        "fallback_items": summary["fallback_items"],
        "failed_items": summary["failed_items"],
        "final_counts": final_counts,
        "report": str(report_path),
    }
    results["persistence"] = {
        "blend": str(blend_path),
        "reopened_text_entities": reopened_entity_count,
        "packed_font_cache_files_deleted": len(font_cache_files),
        "packed_rotated_raster_verified": True,
        "owned_temp_directories_deleted": 2,
    }

    print("BC_BL_REPRESENTATION_ACCEPTANCE=" + json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
