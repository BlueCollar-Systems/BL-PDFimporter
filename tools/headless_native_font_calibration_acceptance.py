"""Canonical Blender acceptance for packed native-FONT size calibration.

Run with Blender, not CPython::

    blender --background --factory-startup \
        --python tools/headless_native_font_calibration_acceptance.py -- \
        Welding-Symbol-Chart.pdf evidence.blend evidence.json
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import traceback
import types

import bpy


INK_TOLERANCE_M = 5.0e-5


def _args() -> tuple[Path, Path, Path]:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(values) != 3:
        raise SystemExit("expected Welding PDF, output .blend, and output .json")
    source, blend, report = (Path(value).expanduser().resolve() for value in values)
    if not source.is_file():
        raise SystemExit(f"missing canonical Welding PDF: {source}")
    blend.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    return source, blend, report


def _close(actual, expected, tolerance: float) -> bool:
    return math.isclose(
        float(actual),
        float(expected),
        rel_tol=1.0e-6,
        abs_tol=tolerance,
    )


def _flatten_matrix(matrix) -> list[float]:
    return [float(value) for row in matrix for value in row]


def _rendered_world_ink_bounds(obj) -> tuple[tuple[float, ...], bool]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    cleared = False
    try:
        mesh = evaluated.to_mesh(
            preserve_all_data_layers=False,
            depsgraph=depsgraph,
        )
        vertices = tuple(mesh.vertices)
        assert vertices, obj.name
        points = tuple(evaluated.matrix_world @ vertex.co for vertex in vertices)
        bounds = (
            min(float(point[0]) for point in points),
            min(float(point[1]) for point in points),
            max(float(point[0]) for point in points),
            max(float(point[1]) for point in points),
        )
        assert all(math.isfinite(value) for value in bounds), (obj.name, bounds)
        return bounds, True
    finally:
        evaluated.to_mesh_clear()
        cleared = True
        assert cleared


def _assert_bounds(actual, expected) -> None:
    assert len(actual) == len(expected) == 4
    assert all(
        abs(float(left) - float(right)) <= INK_TOLERANCE_M
        for left, right in zip(actual, expected)  # noqa: B905
    ), (tuple(actual), tuple(expected))


def _assert_calibration(obj) -> dict:
    assert obj.type == "FONT", (obj.name, obj.type)
    assert bool(obj.get("pdf_exact_font_packed", False)), obj.name
    source_em = float(obj["pdf_metric_source_em_size_m"])
    host_size = float(obj["pdf_metric_host_font_size_m"])
    ratio = float(obj["pdf_metric_host_font_size_calibration_ratio"])
    units_per_em = int(obj["pdf_metric_units_per_em"])
    bbox_y_min = int(obj["pdf_metric_host_font_bbox_y_min_units"])
    bbox_y_max = int(obj["pdf_metric_host_font_bbox_y_max_units"])
    normalization_units = int(obj["pdf_metric_host_font_normalization_units"])
    method = str(obj["pdf_metric_host_font_size_calibration"])
    assert method == "blender_font_bbox_normalization_v1"
    assert normalization_units == bbox_y_max - bbox_y_min > 0
    derived_ratio = float(normalization_units) / float(units_per_em)
    derived_host_size = source_em * derived_ratio
    assert _close(ratio, derived_ratio, 1.0e-12), (obj.name, ratio, derived_ratio)
    assert _close(host_size, derived_host_size, 1.0e-10), (
        obj.name,
        host_size,
        derived_host_size,
    )
    assert _close(float(obj.data.size), derived_host_size, 1.0e-10), (
        obj.name,
        float(obj.data.size),
        derived_host_size,
    )
    return {
        "source_em_size_m": source_em,
        "host_font_size_m": float(obj.data.size),
        "calibration_ratio": ratio,
        "units_per_em": units_per_em,
        "font_bbox_y_min_units": bbox_y_min,
        "font_bbox_y_max_units": bbox_y_max,
        "normalization_units": normalization_units,
        "calibration_method": method,
    }


def main() -> None:
    source_path, blend_path, report_path = _args()
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from pdf_vector_importer import bl_text_builder
    from pdf_vector_importer.dependency_manager import ensure_lib_path
    from pdf_vector_importer.packed_assets import verify_packed_sha256
    from pdf_vector_importer.pdfcadcore.fitz_loader import import_fitz
    from pdf_vector_importer.pdfcadcore.primitive_extractor import extract_page

    ensure_lib_path()
    document = import_fitz().open(str(source_path))
    page_data = extract_page(document[0], 1)
    item = next(text_item for text_item in page_data.text_items if int(text_item.id) == 1)
    assert item.text == "Not", (item.id, item.text)
    assert item.font_asset is not None
    assert len(item.source_char_layout or ()) == 3

    snapshots = []
    cache_files = set()
    mode_results = {}
    for mode in ("text", "3d_text"):
        collection = bpy.data.collections.new(f"NativeFontCalibration_{mode}")
        bpy.context.scene.collection.children.link(collection)
        opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
        first = bl_text_builder.build_text(
            item,
            collection,
            page_number=1,
            text_mode=mode,
            provenance_opts=opts,
        )
        record = opts._text_delivery_records[-1]
        assert first is not None and first.type == "FONT", record
        assert record["status"] == "delivered", record
        assert record["requested_representation"] == mode
        assert record["final_representation"] == mode
        assert record["fallback_used"] is False
        assert len(record["attempts"]) == 1
        delivered_attempt = record["attempts"][0]
        character_evidence = delivered_attempt["evidence"]["character_entities"]
        assert len(character_evidence) == len(record["entity_ids"]) == 3
        entities = [bpy.data.objects[str(name)] for name in record["entity_ids"]]
        assert "".join(entity.data.body for entity in entities) == item.text
        for char_evidence, entity in zip(character_evidence, entities):  # noqa: B905
            verification = char_evidence["verification"]
            assert verification["native_font_size_calibration_verified"] is True
            assert verification["evaluated_ink_bounds_verified"] is True
            assert verification["evaluated_ink_bounds_source"] == (
                "evaluated_font_render_mesh"
            )
            assert verification["evaluated_font_render_mesh_cleared"] is True
            calibration = _assert_calibration(entity)
            expected_sha = str(entity["pdf_exact_font_sha256"])
            assert verify_packed_sha256(entity.data.font, expected_sha) == expected_sha
            font_path = Path(bpy.path.abspath(entity.data.font.filepath)).resolve()
            if font_path.parent.name == "bc_bl_pdf_exact_fonts":
                cache_files.add(font_path)
            expected_bounds = tuple(
                float(value)
                for value in entity["pdf_metric_expected_world_ink_bounds_m"]
            )
            snapshots.append({
                "name": entity.name,
                "mode": mode,
                "body": entity.data.body,
                "font_sha256": expected_sha,
                "matrix": _flatten_matrix(entity.matrix_world),
                "expected_world_ink_bounds_m": list(expected_bounds),
                "extrusion_m": float(entity.data.extrude),
                "calibration": calibration,
            })
        assert all(
            (float(entity.data.extrude) > 0.0) is (mode == "3d_text")
            for entity in entities
        )
        mode_results[mode] = {
            "representation": "FONT",
            "entity_count": len(entities),
            "fallback_used": False,
            "render_mesh_verified": len(entities),
        }

    sampled_font_shas = {str(item.font_asset.usable_sha256)}
    font_asset_samples = [{
        "source_item_id": int(item.id),
        "source_font": item.font_name,
        "font_sha256": str(item.font_asset.usable_sha256),
        "source_text": item.text,
        "modes": ["text", "3d_text"],
    }]
    for source_item in page_data.text_items:
        asset = source_item.font_asset
        if asset is None or str(asset.usable_sha256) in sampled_font_shas:
            continue
        layout = next(
            (
                candidate
                for candidate in source_item.source_char_layout or ()
                if candidate.glyph_id is not None
                and str(candidate.text)
                and not str(candidate.text).isspace()
            ),
            None,
        )
        if layout is None:
            continue
        sample_text = str(layout.text)
        sample = replace(
            source_item,
            id=9_100_000 + len(font_asset_samples),
            text=sample_text,
            normalized=sample_text,
            source_char_layout=(layout,),
            requires_individual_positioning=True,
        )
        collection = bpy.data.collections.new(
            f"NativeFontCalibration_asset_{len(font_asset_samples)}"
        )
        bpy.context.scene.collection.children.link(collection)
        opts = types.SimpleNamespace(import_mode="vector", text_mode="text")
        entity = bl_text_builder.build_text(
            sample,
            collection,
            page_number=1,
            text_mode="text",
            provenance_opts=opts,
        )
        record = opts._text_delivery_records[-1]
        assert entity is not None and entity.type == "FONT", record
        assert record["status"] == "delivered", record
        assert record["fallback_used"] is False
        assert len(record["attempts"]) == 1
        assert record["entity_ids"] == [entity.name]
        verification = record["attempts"][0]["evidence"]["character_entities"][0][
            "verification"
        ]
        assert verification["native_font_size_calibration_verified"] is True
        assert verification["evaluated_ink_bounds_verified"] is True
        assert verification["evaluated_ink_bounds_source"] == (
            "evaluated_font_render_mesh"
        )
        assert verification["evaluated_font_render_mesh_cleared"] is True
        calibration = _assert_calibration(entity)
        expected_sha = str(entity["pdf_exact_font_sha256"])
        assert expected_sha == str(asset.usable_sha256)
        assert verify_packed_sha256(entity.data.font, expected_sha) == expected_sha
        font_path = Path(bpy.path.abspath(entity.data.font.filepath)).resolve()
        if font_path.parent.name == "bc_bl_pdf_exact_fonts":
            cache_files.add(font_path)
        expected_bounds = tuple(
            float(value)
            for value in entity["pdf_metric_expected_world_ink_bounds_m"]
        )
        snapshots.append({
            "name": entity.name,
            "mode": "text",
            "body": entity.data.body,
            "font_sha256": expected_sha,
            "matrix": _flatten_matrix(entity.matrix_world),
            "expected_world_ink_bounds_m": list(expected_bounds),
            "extrusion_m": float(entity.data.extrude),
            "calibration": calibration,
            "sample_kind": "font_asset_representative",
            "source_font": source_item.font_name,
        })
        sampled_font_shas.add(expected_sha)
        font_asset_samples.append({
            "source_item_id": int(source_item.id),
            "source_font": source_item.font_name,
            "font_sha256": expected_sha,
            "source_text": sample_text,
            "glyph_id": int(layout.glyph_id),
            "modes": ["text"],
        })

    expected_font_shas = {
        str(source_item.font_asset.usable_sha256)
        for source_item in page_data.text_items
        if source_item.font_asset is not None
    }
    assert sampled_font_shas == expected_font_shas

    document.close()
    save_result = bpy.ops.wm.save_as_mainfile(
        filepath=str(blend_path),
        check_existing=False,
    )
    assert "FINISHED" in save_result
    assert blend_path.is_file()
    for path in sorted(cache_files):
        path.unlink(missing_ok=True)
    assert all(not path.exists() for path in cache_files)

    reopen_result = bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    assert "FINISHED" in reopen_result
    bpy.context.view_layer.update()
    reopened = []
    for snapshot in snapshots:
        entity = bpy.data.objects.get(snapshot["name"])
        assert entity is not None
        assert entity.type == "FONT"
        assert entity.data.body == snapshot["body"]
        assert snapshot["mode"] == str(entity["pdf_text_mode"])
        calibration = _assert_calibration(entity)
        assert _close(
            calibration["host_font_size_m"],
            snapshot["calibration"]["host_font_size_m"],
            1.0e-10,
        )
        actual_matrix = _flatten_matrix(entity.matrix_world)
        assert all(
            _close(actual, expected, 1.0e-7)
            for actual, expected in zip(  # noqa: B905
                actual_matrix,
                snapshot["matrix"],
            )
        )
        expected_sha = snapshot["font_sha256"]
        assert verify_packed_sha256(entity.data.font, expected_sha) == expected_sha
        actual_bounds, cleared = _rendered_world_ink_bounds(entity)
        assert cleared is True
        _assert_bounds(actual_bounds, snapshot["expected_world_ink_bounds_m"])
        reopened.append({
            "name": entity.name,
            "mode": snapshot["mode"],
            "body": entity.data.body,
            "object_type": entity.type,
            "host_font_size_m": float(entity.data.size),
            "extrusion_m": float(entity.data.extrude),
            "actual_world_ink_bounds_m": list(actual_bounds),
            "expected_world_ink_bounds_m": snapshot["expected_world_ink_bounds_m"],
            "packed_font_sha256": expected_sha,
            "temporary_render_mesh_cleared": cleared,
        })

    artifact = {
        "status": "passed",
        "blender_version": list(bpy.app.version),
        "source_pdf": str(source_path),
        "source_pdf_sha256": sha256(source_path.read_bytes()).hexdigest(),
        "source_item_id": int(item.id),
        "source_text": item.text,
        "source_font": item.font_name,
        "source_glyph_ids": [
            int(layout.glyph_id) for layout in item.source_char_layout or ()
        ],
        "modes": mode_results,
        "font_asset_samples": font_asset_samples,
        "blend": str(blend_path),
        "font_cache_files_deleted_before_reopen": len(cache_files),
        "reopened_entities": reopened,
    }
    report_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "BC_BL_NATIVE_FONT_CALIBRATION_ACCEPTANCE="
        + json.dumps(artifact, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
