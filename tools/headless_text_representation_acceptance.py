"""Real-Blender acceptance for item-scoped PDF text representations.

Run with Blender, not CPython::

    blender --background --python tools/headless_text_representation_acceptance.py -- \
        Welding-Symbol-Chart.pdf AWSWeldSymbolchart.pdf
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
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


def _finite_vector(value) -> list[float]:
    result = [float(component) for component in value]
    assert result and all(math.isfinite(component) for component in result), result
    return result


def _optional_scalar(subject, name: str, converter):
    marker = object()
    value = getattr(subject, name, marker)
    return None if value is marker else converter(value)


def _curve_physical_state(curve_data) -> dict:
    """Return editable CURVE topology and control-point truth."""

    curve_settings = {
        name: _optional_scalar(curve_data, name, converter)
        for name, converter in (
            ("dimensions", str),
            ("fill_mode", str),
            ("resolution_u", int),
            ("resolution_v", int),
            ("bevel_depth", float),
            ("bevel_resolution", int),
            ("extrude", float),
            ("offset", float),
            ("twist_mode", str),
            ("use_fill_caps", bool),
        )
    }
    splines = []
    for spline in tuple(curve_data.splines):
        bezier_points = []
        for point in tuple(getattr(spline, "bezier_points", ()) or ()):
            bezier_points.append({
                "co": _finite_vector(point.co),
                "handle_left": _finite_vector(point.handle_left),
                "handle_right": _finite_vector(point.handle_right),
                "handle_left_type": str(point.handle_left_type),
                "handle_right_type": str(point.handle_right_type),
                "radius": _optional_scalar(point, "radius", float),
                "tilt": _optional_scalar(point, "tilt", float),
                "weight_softbody": _optional_scalar(
                    point, "weight_softbody", float
                ),
            })
        points = []
        for point in tuple(getattr(spline, "points", ()) or ()):
            points.append({
                "co": _finite_vector(point.co),
                "radius": _optional_scalar(point, "radius", float),
                "tilt": _optional_scalar(point, "tilt", float),
                "weight": _optional_scalar(point, "weight", float),
                "weight_softbody": _optional_scalar(
                    point, "weight_softbody", float
                ),
            })
        splines.append({
            "type": str(spline.type),
            "use_cyclic_u": bool(spline.use_cyclic_u),
            "resolution_u": _optional_scalar(spline, "resolution_u", int),
            "order_u": _optional_scalar(spline, "order_u", int),
            "use_endpoint_u": _optional_scalar(spline, "use_endpoint_u", bool),
            "use_bezier_u": _optional_scalar(spline, "use_bezier_u", bool),
            "bezier_points": bezier_points,
            "points": points,
        })
    assert splines
    return {"settings": curve_settings, "splines": splines}


def _assert_curve_physical_state(curve_data, expected: dict) -> None:
    actual = _curve_physical_state(curve_data)
    assert actual == expected, (actual, expected)


def _mesh_physical_state(mesh_data) -> dict:
    """Return editable MESH coordinates, connectivity, loops, and faces."""

    state = {
        "vertices_local": [
            _finite_vector(vertex.co) for vertex in tuple(mesh_data.vertices)
        ],
        "edges": [
            [int(value) for value in edge.vertices]
            for edge in tuple(getattr(mesh_data, "edges", ()) or ())
        ],
        "loops": [
            {
                "vertex_index": int(loop.vertex_index),
                "edge_index": int(loop.edge_index),
            }
            for loop in tuple(getattr(mesh_data, "loops", ()) or ())
        ],
        "polygons": [
            {
                "vertices": [int(value) for value in polygon.vertices],
                "loop_indices": [int(value) for value in polygon.loop_indices],
                "material_index": int(polygon.material_index),
                "use_smooth": bool(polygon.use_smooth),
            }
            for polygon in tuple(mesh_data.polygons)
        ],
    }
    assert state["vertices_local"] and state["polygons"]
    return state


def _assert_mesh_physical_state(mesh_data, expected: dict) -> None:
    actual = _mesh_physical_state(mesh_data)
    assert actual == expected, (actual, expected)


def _evaluated_physical_state(obj) -> dict:
    """Fingerprint the actual evaluated geometry for one physical text entity."""

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    render_mesh = None
    try:
        try:
            render_mesh = evaluated.to_mesh(
                preserve_all_data_layers=False,
                depsgraph=depsgraph,
            )
        except TypeError:
            render_mesh = evaluated.to_mesh()
        vertices = [
            _finite_vector(vertex.co) for vertex in tuple(render_mesh.vertices)
        ]
        edges = [
            [int(value) for value in edge.vertices]
            for edge in tuple(getattr(render_mesh, "edges", ()) or ())
        ]
        polygons = [
            [int(value) for value in polygon.vertices]
            for polygon in tuple(getattr(render_mesh, "polygons", ()) or ())
        ]
        canonical_geometry = json.dumps(
            {
                "vertices": vertices,
                "edges": edges,
                "polygons": polygons,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        world_points = [
            _finite_vector(evaluated.matrix_world @ vertex.co)
            for vertex in tuple(render_mesh.vertices)
        ]
        world_bounds = None
        if world_points:
            world_bounds = [
                min(point[0] for point in world_points),
                min(point[1] for point in world_points),
                min(point[2] for point in world_points),
                max(point[0] for point in world_points),
                max(point[1] for point in world_points),
                max(point[2] for point in world_points),
            ]
        return {
            "vertex_count": len(vertices),
            "edge_count": len(edges),
            "polygon_count": len(polygons),
            "local_geometry_sha256": sha256(canonical_geometry).hexdigest(),
            "world_affine": [
                float(value) for row in evaluated.matrix_world for value in row
            ],
            "world_bounds": world_bounds,
        }
    finally:
        if render_mesh is not None:
            evaluated.to_mesh_clear()


def _assert_evaluated_physical_state(obj, expected: dict) -> None:
    actual = _evaluated_physical_state(obj)
    for key in (
        "vertex_count",
        "edge_count",
        "polygon_count",
        "local_geometry_sha256",
    ):
        assert actual[key] == expected[key], (key, actual, expected)
    _assert_points_close(actual["world_affine"], expected["world_affine"])
    if expected["world_bounds"] is None:
        assert actual["world_bounds"] is None, (actual, expected)
    else:
        assert actual["world_bounds"] is not None, (actual, expected)
        _assert_points_close(
            actual["world_bounds"],
            expected["world_bounds"],
            tolerance=5.0e-7,
        )


def _same_blender_identity(left, right) -> bool:
    if left is right:
        return True
    try:
        if left == right:
            return True
    except (ReferenceError, TypeError, ValueError):
        pass
    try:
        left_pointer = int(left.as_pointer())
        right_pointer = int(right.as_pointer())
    except (AttributeError, ReferenceError, TypeError, ValueError):
        return False
    return left_pointer != 0 and left_pointer == right_pointer


def _node_socket(node, collection_name: str, socket_name: str):
    sockets = getattr(node, collection_name)
    getter = getattr(sockets, "get", None)
    return getter(socket_name) if callable(getter) else sockets[socket_name]


def _exact_node_link(
    links,
    *,
    from_node,
    from_socket,
    to_node,
    to_socket,
) -> bool:
    return any(
        _same_blender_identity(link.from_node, from_node)
        and _same_blender_identity(link.from_socket, from_socket)
        and _same_blender_identity(link.to_node, to_node)
        and _same_blender_identity(link.to_socket, to_socket)
        for link in links
    )


def _assert_text_material_state(obj, material_name: str, expected_rgba) -> None:
    material = bpy.data.materials.get(material_name)
    assert material is not None and bool(material.use_nodes)
    assert any(
        _same_blender_identity(candidate, material)
        for candidate in obj.data.materials
    )
    expected = [float(value) for value in expected_rgba]
    assert len(expected) == 4
    _assert_points_close(material.diffuse_color, expected)
    nodes = list(material.node_tree.nodes)
    links = list(material.node_tree.links)
    shaders = [node for node in nodes if node.type == "BSDF_PRINCIPLED"]
    outputs = [node for node in nodes if node.type == "OUTPUT_MATERIAL"]
    verified = False
    for shader in shaders:
        _assert_points_close(
            _node_socket(shader, "inputs", "Base Color").default_value,
            expected,
        )
        assert abs(
            float(_node_socket(shader, "inputs", "Alpha").default_value)
            - expected[3]
        ) <= 1.0e-6
        for output in outputs:
            if not bool(getattr(output, "is_active_output", True)):
                continue
            if _exact_node_link(
                links,
                from_node=shader,
                from_socket=_node_socket(shader, "outputs", "BSDF"),
                to_node=output,
                to_socket=_node_socket(output, "inputs", "Surface"),
            ):
                verified = True
                break
    assert verified, (material_name, "exact BSDF to active Surface link missing")


def _raster_material_chain_verified(material, image) -> bool:
    nodes = list(material.node_tree.nodes)
    links = list(material.node_tree.links)
    textures = [
        node
        for node in nodes
        if node.type == "TEX_IMAGE"
        and _same_blender_identity(node.image, image)
    ]
    shaders = [node for node in nodes if node.type == "BSDF_PRINCIPLED"]
    outputs = [node for node in nodes if node.type == "OUTPUT_MATERIAL"]
    for texture in textures:
        for shader in shaders:
            if not (
                _exact_node_link(
                    links,
                    from_node=texture,
                    from_socket=_node_socket(texture, "outputs", "Color"),
                    to_node=shader,
                    to_socket=_node_socket(shader, "inputs", "Base Color"),
                )
                and _exact_node_link(
                    links,
                    from_node=texture,
                    from_socket=_node_socket(texture, "outputs", "Alpha"),
                    to_node=shader,
                    to_socket=_node_socket(shader, "inputs", "Alpha"),
                )
            ):
                continue
            for output in outputs:
                if not bool(getattr(output, "is_active_output", True)):
                    continue
                if _exact_node_link(
                    links,
                    from_node=shader,
                    from_socket=_node_socket(shader, "outputs", "BSDF"),
                    to_node=output,
                    to_socket=_node_socket(output, "inputs", "Surface"),
                ):
                    return True
    return False


def _assert_metric_entity(obj, *, verify_rendered_ink: bool = False) -> None:
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

    zero_ink = bool(obj.get("pdf_metric_zero_ink_identity", False))
    if obj.type != "FONT" or zero_ink:
        return
    source_em_size = float(obj["pdf_metric_source_em_size_m"])
    expected_host_size = float(obj["pdf_metric_host_font_size_m"])
    calibration_ratio = float(obj["pdf_metric_host_font_size_calibration_ratio"])
    units_per_em = int(obj["pdf_metric_units_per_em"])
    bbox_y_min = int(obj["pdf_metric_host_font_bbox_y_min_units"])
    bbox_y_max = int(obj["pdf_metric_host_font_bbox_y_max_units"])
    normalization_units = int(obj["pdf_metric_host_font_normalization_units"])
    assert str(obj["pdf_metric_host_font_size_calibration"]) == (
        "blender_font_bbox_normalization_v1"
    )
    assert units_per_em > 0 and normalization_units == bbox_y_max - bbox_y_min > 0
    derived_ratio = float(normalization_units) / float(units_per_em)
    derived_host_size = source_em_size * derived_ratio
    _assert_points_close(
        [calibration_ratio, expected_host_size, float(obj.data.size)],
        [derived_ratio, derived_host_size, derived_host_size],
        tolerance=max(1.0e-10, derived_host_size * 1.0e-6),
    )

    if not verify_rendered_ink:
        return
    expected_ink_bounds = tuple(
        float(value) for value in obj["pdf_metric_expected_world_ink_bounds_m"]
    )
    assert len(expected_ink_bounds) == 4
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    render_mesh = None
    try:
        render_mesh = evaluated.to_mesh(
            preserve_all_data_layers=False,
            depsgraph=depsgraph,
        )
        vertices = tuple(render_mesh.vertices)
        assert vertices
        points = tuple(evaluated.matrix_world @ vertex.co for vertex in vertices)
        actual_ink_bounds = (
            min(float(point[0]) for point in points),
            min(float(point[1]) for point in points),
            max(float(point[0]) for point in points),
            max(float(point[1]) for point in points),
        )
    finally:
        evaluated.to_mesh_clear()
    _assert_points_close(
        actual_ink_bounds,
        expected_ink_bounds,
        tolerance=5.0e-5,
    )


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
        actual_entity_text = (
            str(obj.data.body)
            if obj.type == "FONT"
            else str(obj["pdf_text_source"])
        )
        assert actual_entity_text == str(item["text"])
        assert str(obj["pdf_source_item_id"]) == str(record["item_id"])
        assert int(obj["pdf_source_span_id"]) == int(record["source_span_id"])
        assert str(obj["pdf_text_requested_mode"]) == str(
            record["requested_representation"]
        )
        assert str(obj["pdf_text_mode"]) == str(record["final_representation"])
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


def _assert_final_state_proof(record) -> None:
    proof = record["final_state_verification"]
    assert proof["status"] == "verified", record
    assert proof["canonical_parent_verified"] is True, record
    assert proof["provenance_parent_handle_verified"] is True, record
    for entity in proof["entities"]:
        assert entity["object_handle_verified"] is True, entity
        assert entity["source_item_verified"] is True, entity
        if entity["expectation_kind"] == "character":
            assert entity["character_identity_verified"] is True, entity
        else:
            assert entity["character_identity_verified"] is None, entity
        assert entity["representation_fields_verified"] is True, entity
        assert entity["affine_verified"] is True, entity
        assert entity["physical_ink_continuity_verified"] is True, entity
        if entity["expectation_kind"] == "raster":
            assert entity["raster_geometry_verified"] is True, entity
            assert entity["raster_uv_verified"] is True, entity
            assert entity["raster_material_binding_verified"] is True, entity


def _assert_provenance_parent_links(records, sidecar_path: Path) -> dict:
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    objects = sidecar["objects"]
    for record in records:
        matches = [
            item
            for item in objects
            if int(item["page"]) == int(record["page"])
            and int(item["span_id"]) == int(record["source_span_id"])
            and item["source_kind"] == "text_span"
        ]
        assert len(matches) == 1, (record, matches)
        expected_parent = (
            str(record["logical_delivery_id"])
            if record.get("zero_ink_delivery") is True
            else str(record["entity_ids"][0])
        )
        assert matches[0]["parent_handle"] == expected_parent, (
            matches[0],
            expected_parent,
        )
    return sidecar


def _delivery_record_views(attempt_ledger, resolution) -> list[dict]:
    """Rebuild rich Blender record views in memory from canonical terminals."""

    records = []
    items = list(resolution["items"])
    terminals = list(resolution["terminal_attempts"])
    assert len(items) == len(terminals)
    for item, terminal in zip(items, terminals, strict=True):
        source_id = str(item["source_item_id"])
        host_record = terminal.get("host_record")
        assert isinstance(host_record, dict), terminal
        record = dict(host_record)
        record["item_id"] = source_id
        record["attempts"] = [
            attempt
            for attempt in attempt_ledger
            if str(attempt.get("source_item_id") or "") == source_id
        ]
        if isinstance(terminal.get("final_state_verification"), dict):
            record["final_state_verification"] = dict(
                terminal["final_state_verification"]
            )
        records.append(record)
    return records


def _physical_entity_snapshot(obj) -> dict:
    snapshot = {
        "name": str(obj.name),
        "type": str(obj.type),
        "source_item_id": str(obj["pdf_source_item_id"]),
        "source_span_id": int(obj["pdf_source_span_id"]),
        "text_mode": str(obj["pdf_text_mode"]),
        "requested_mode": str(obj["pdf_text_requested_mode"]),
    }
    if snapshot["text_mode"] == "raster":
        uv_layer = obj.data.uv_layers.get("UVMap")
        assert uv_layer is not None
        snapshot.update({
            "raster": True,
            "vertices_local": [
                [float(value) for value in vertex.co]
                for vertex in obj.data.vertices
            ],
            "loops_vertex_indices": [
                int(loop.vertex_index) for loop in obj.data.loops
            ],
            "polygons": [
                {
                    "vertices": [int(value) for value in polygon.vertices],
                    "loop_indices": [
                        int(value) for value in polygon.loop_indices
                    ],
                    "material_index": int(polygon.material_index),
                }
                for polygon in obj.data.polygons
            ],
            "uv_coordinates": [
                [float(value) for value in item.uv]
                for item in uv_layer.data
            ],
            "world_affine": [
                float(value) for row in obj.matrix_world for value in row
            ],
            "image_datablock": str(obj["pdf_image_datablock"]),
            "image_sha256": str(obj["pdf_image_sha256"]),
            "material": str(obj["pdf_image_material"]),
        })
        image = bpy.data.images.get(snapshot["image_datablock"])
        material = bpy.data.materials.get(snapshot["material"])
        assert image is not None and material is not None
        assert _raster_material_chain_verified(material, image)
        return snapshot
    snapshot.update({
        "source_char_index": int(obj["pdf_source_char_index"]),
        "source_glyph_id": int(obj["pdf_source_glyph_id"]),
        "physical_glyph_id": int(obj["pdf_physical_glyph_id"]),
        "source_text": (
            str(obj.data.body)
            if obj.type == "FONT"
            else str(obj["pdf_text_source"])
        ),
        "affine": [float(value) for value in obj["pdf_affine_matrix"]],
        "zero_ink": bool(obj["pdf_metric_zero_ink_identity"]),
        "font_sha256": str(obj.get("pdf_exact_font_sha256", "") or ""),
        "material": str(obj["pdf_text_material"]),
        "expected_rgba": [
            float(value) for value in obj["pdf_text_expected_rgba"]
        ],
        "world_affine": [
            float(value) for row in obj.matrix_world for value in row
        ],
    })
    _assert_text_material_state(
        obj,
        snapshot["material"],
        snapshot["expected_rgba"],
    )
    snapshot["evaluated_physical_state"] = _evaluated_physical_state(obj)
    if snapshot["zero_ink"]:
        assert snapshot["evaluated_physical_state"]["vertex_count"] == 0
    else:
        assert snapshot["evaluated_physical_state"]["vertex_count"] > 0
    if obj.type == "FONT":
        snapshot["extrusion_m"] = float(obj.data.extrude)
    elif obj.type == "CURVE":
        snapshot["curve_physical_state"] = _curve_physical_state(obj.data)
    elif obj.type == "MESH":
        snapshot["mesh_physical_state"] = _mesh_physical_state(obj.data)
    return snapshot


def _assert_reopened_physical_entity(snapshot, verify_packed_sha256) -> None:
    obj = bpy.data.objects.get(snapshot["name"])
    assert obj is not None and obj.type == snapshot["type"], snapshot
    assert str(obj["pdf_source_item_id"]) == snapshot["source_item_id"]
    assert int(obj["pdf_source_span_id"]) == snapshot["source_span_id"]
    assert str(obj["pdf_text_mode"]) == snapshot["text_mode"]
    assert str(obj["pdf_text_requested_mode"]) == snapshot["requested_mode"]
    if snapshot.get("raster") is True:
        assert [
            [float(value) for value in vertex.co]
            for vertex in obj.data.vertices
        ] == snapshot["vertices_local"]
        assert [
            int(loop.vertex_index) for loop in obj.data.loops
        ] == snapshot["loops_vertex_indices"]
        assert [
            {
                "vertices": [int(value) for value in polygon.vertices],
                "loop_indices": [int(value) for value in polygon.loop_indices],
                "material_index": int(polygon.material_index),
            }
            for polygon in obj.data.polygons
        ] == snapshot["polygons"]
        uv_layer = obj.data.uv_layers.get("UVMap")
        assert uv_layer is not None
        assert [
            [float(value) for value in item.uv]
            for item in uv_layer.data
        ] == snapshot["uv_coordinates"]
        _assert_points_close(
            [float(value) for row in obj.matrix_world for value in row],
            snapshot["world_affine"],
        )
        image = bpy.data.images.get(snapshot["image_datablock"])
        material = bpy.data.materials.get(snapshot["material"])
        assert image is not None and material is not None
        assert str(obj["pdf_image_sha256"]) == snapshot["image_sha256"]
        assert verify_packed_sha256(image, snapshot["image_sha256"]) == (
            snapshot["image_sha256"]
        )
        assigned = list(obj.data.materials)
        assert any(
            _same_blender_identity(candidate, material)
            for candidate in assigned
        )
        assert all(
            _same_blender_identity(
                assigned[int(polygon.material_index)],
                material,
            )
            for polygon in obj.data.polygons
        )
        assert _raster_material_chain_verified(material, image)
        return
    assert int(obj["pdf_source_char_index"]) == snapshot["source_char_index"]
    assert int(obj["pdf_source_glyph_id"]) == snapshot["source_glyph_id"]
    assert int(obj["pdf_physical_glyph_id"]) == snapshot["physical_glyph_id"]
    actual_text = (
        str(obj.data.body)
        if obj.type == "FONT"
        else str(obj["pdf_text_source"])
    )
    assert actual_text == snapshot["source_text"]
    assert bool(obj["pdf_metric_zero_ink_identity"]) is snapshot["zero_ink"]
    _assert_text_material_state(
        obj,
        snapshot["material"],
        snapshot["expected_rgba"],
    )
    _assert_points_close(obj["pdf_affine_matrix"], snapshot["affine"])
    _assert_points_close(
        [float(value) for row in obj.matrix_world for value in row],
        snapshot["world_affine"],
    )
    _assert_metric_entity(obj)
    _assert_evaluated_physical_state(
        obj,
        snapshot["evaluated_physical_state"],
    )
    if obj.type == "FONT":
        assert abs(float(obj.data.extrude) - snapshot["extrusion_m"]) <= 1.0e-12
        expected_sha = snapshot["font_sha256"]
        assert str(obj["pdf_exact_font_sha256"]) == expected_sha
        assert verify_packed_sha256(obj.data.font, expected_sha) == expected_sha
    elif obj.type == "CURVE":
        _assert_curve_physical_state(obj.data, snapshot["curve_physical_state"])
    elif obj.type == "MESH":
        _assert_mesh_physical_state(obj.data, snapshot["mesh_physical_state"])


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
    from pdf_vector_importer.pdfcadcore.text_delivery_report import (
        resolve_text_representation_delivery,
    )
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
    focused_persistence = {}

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
        if mode in {"text", "3d_text", "glyphs", "geometry", "raster"}:
            final_failures = bl_import_engine._reverify_text_delivery_after_stack(
                [record],
                page_number=1,
                stack_offset_m=0.0,
                provenance_opts=opts,
            )
            assert final_failures == [], (mode, final_failures, record)
            _assert_final_state_proof(record)
            provenance_matches = [
                item
                for item in opts._source_provenance_objects
                if int(item.page) == 1 and int(item.span_id) == int(exact_item.id)
            ]
            assert len(provenance_matches) == 1
            assert provenance_matches[0].parent_handle == record["entity_ids"][0]
            focused_persistence[mode] = {
                "canonical_parent_handle": str(record["entity_ids"][0]),
                "provenance_parent_handle": str(
                    provenance_matches[0].parent_handle
                ),
                "entities": [_physical_entity_snapshot(entity) for entity in entities],
            }
        results["modes"][mode] = {
            "object_type": obj.type,
            "entity_id": obj.name,
            "entity_count": len(entities),
            "attempt_count": len(record["attempts"]),
            "dependency_graph_updates": dependency_graph_updates,
        }
        print(f"BC_BL_ACCEPTANCE_STAGE=mode:{mode}", flush=True)

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
    label_final_failures = bl_import_engine._reverify_text_delivery_after_stack(
        [label_record],
        page_number=1,
        stack_offset_m=0.0,
        provenance_opts=label_opts,
    )
    assert label_final_failures == [], (label_final_failures, label_record)
    _assert_final_state_proof(label_record)
    label_provenance_matches = [
        item
        for item in label_opts._source_provenance_objects
        if int(item.page) == 1 and int(item.span_id) == int(exact_item.id)
    ]
    assert len(label_provenance_matches) == 1
    assert label_provenance_matches[0].parent_handle == label_record["entity_ids"][0]
    focused_persistence["labels"] = {
        "canonical_parent_handle": str(label_record["entity_ids"][0]),
        "provenance_parent_handle": str(
            label_provenance_matches[0].parent_handle
        ),
        "entities": [
            _physical_entity_snapshot(entity) for entity in label_entities
        ],
    }
    results["modes"]["labels"] = {
        "object_type": label_obj.type,
        "entity_count": len(label_entities),
        "final": "text",
        "reason": label_record["attempts"][0]["reason"],
    }
    print("BC_BL_ACCEPTANCE_STAGE=mode:labels", flush=True)

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
    raster_page_data = extract_page(raster_page, 1)
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
    print("BC_BL_ACCEPTANCE_STAGE=full_import:start", flush=True)
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
    print("BC_BL_ACCEPTANCE_STAGE=full_import:complete", flush=True)
    assert full_stats["text_delivery_source_items"] == len(page_data.text_items)
    assert full_stats["text_delivery_delivered_items"] == len(page_data.text_items)
    assert full_stats["text_delivery_failed_items"] == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    delivery_summary = report["extra"]["text_delivery"]
    assert set(delivery_summary) == {"schema", "summary"}
    summary = delivery_summary["summary"]
    assert summary["source_items"] == len(page_data.text_items)
    assert summary["delivered_items"] == len(page_data.text_items)
    assert summary["failed_items"] == 0
    final_counts = summary["final_counts"]
    assert int(final_counts.get("3d_text", 0)) == len(page_data.text_items)
    assert int(final_counts.get("raster", 0)) == 0
    assert summary["fallback_items"] == 0
    source_text_by_id = {int(item.id): str(item.text) for item in page_data.text_items}
    delivery = report["extra"]["text_representation_delivery"]
    attempt_ledger = report["extra"]["text_delivery_attempts"]
    expected_source_ids = {
        f"page:1:text:{int(item.id)}" for item in page_data.text_items
    }
    delivery_resolution = resolve_text_representation_delivery(
        attempt_ledger,
        delivery,
        expected_source_item_ids=expected_source_ids,
    )
    assert delivery_resolution["verified"] is True, delivery_resolution
    full_records = _delivery_record_views(attempt_ledger, delivery_resolution)
    welding_provenance_path = report_path.with_name(
        report["extra"]["source_provenance_path"]
    )
    _assert_provenance_parent_links(full_records, welding_provenance_path)
    full_entity_names = []
    full_entity_snapshots = []
    font_cache_files = set()
    for record in full_records:
        _assert_delivery(record, "3d_text", "3d_text")
        _assert_final_state_proof(record)
        source_text = source_text_by_id[int(record["source_span_id"])]
        entities = _assert_character_delivery(record, "FONT", source_text)
        full_entity_names.extend(entity.name for entity in entities)
        for entity in entities:
            full_entity_snapshots.append(_physical_entity_snapshot(entity))
            expected_font_sha = str(entity["pdf_exact_font_sha256"])
            assert verify_packed_sha256(entity.data.font, expected_font_sha) == expected_font_sha
            font_path = Path(bpy.path.abspath(entity.data.font.filepath)).resolve()
            if font_path.parent.name == "bc_bl_pdf_exact_fonts":
                font_cache_files.add(font_path)

    aws_dir = full_dir / "aws"
    aws_dir.mkdir()
    aws_report_path = aws_dir / "aws-import-report.json"
    aws_existing_object_names = {str(obj.name) for obj in bpy.data.objects}
    print("BC_BL_ACCEPTANCE_STAGE=full_import_aws:start", flush=True)
    aws_stats = bl_import_engine.import_pdf(
        str(raster_path),
        config={
            "mode": "raster",
            "pages": "1",
            "import_text": True,
            "text_mode": "3d_text",
            "ignore_images": False,
            "visual_style": "source",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
            "import_report_path": str(aws_report_path),
        },
    )
    assert raster_page_data.text_items == []
    assert aws_stats["text_source_spans"] == 0
    assert aws_stats["text_delivery_source_items"] == 0
    assert aws_stats["text_delivery_delivered_items"] == 0
    assert aws_stats["text_delivery_failed_items"] == 0
    assert aws_stats["images"] == 1
    assert aws_stats["raster_delivery_failures"] == []
    aws_raster_objects = [
        obj
        for obj in bpy.data.objects
        if str(obj.name) not in aws_existing_object_names
        and obj.type == "MESH"
        and bool(obj.get("pdf_image_packed", False))
    ]
    assert len(aws_raster_objects) == 1, aws_raster_objects
    aws_raster_obj = aws_raster_objects[0]
    aws_raster_object_name = str(aws_raster_obj.name)
    aws_raster_image_name = str(aws_raster_obj["pdf_image_datablock"])
    aws_raster_material_name = str(aws_raster_obj["pdf_image_material"])
    aws_raster_image_sha = str(aws_raster_obj["pdf_image_sha256"])
    aws_raster_image = bpy.data.images.get(aws_raster_image_name)
    aws_raster_material = bpy.data.materials.get(aws_raster_material_name)
    assert aws_raster_image is not None and aws_raster_material is not None
    assert verify_packed_sha256(
        aws_raster_image,
        aws_raster_image_sha,
    ) == aws_raster_image_sha
    assert _raster_material_chain_verified(aws_raster_material, aws_raster_image)
    aws_report = json.loads(aws_report_path.read_text(encoding="utf-8"))
    assert aws_report["fallback"] == {"used": False, "reason": None}
    aws_delivery = aws_report["extra"].get("text_delivery")
    assert aws_delivery is None
    aws_records = []
    aws_provenance_path = None
    print("BC_BL_ACCEPTANCE_STAGE=full_import_aws:complete", flush=True)

    all_full_records = list(full_records) + list(aws_records)

    blend_path = full_dir / "canonical-pdfs-text-persistence.blend"
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
    print("BC_BL_ACCEPTANCE_STAGE=reopen:loaded", flush=True)
    reopened_entity_count = 0
    reopened_font_shas = set()
    for record in all_full_records:
        entities = _delivery_entities(record, "FONT")
        reopened_entity_count += len(entities)
        for entity in entities:
            expected_font_sha = str(entity["pdf_exact_font_sha256"])
            reopened_font_shas.add(expected_font_sha)
            assert verify_packed_sha256(entity.data.font, expected_font_sha) == expected_font_sha
    assert reopened_entity_count == len(full_entity_names)
    assert len(full_entity_snapshots) == len(full_entity_names)
    for entity_snapshot in full_entity_snapshots:
        _assert_reopened_physical_entity(
            entity_snapshot,
            verify_packed_sha256,
        )
    reopened_rendered_ink_entities = sum(
        not bool(snapshot["zero_ink"])
        for snapshot in full_entity_snapshots
    )

    for _mode, delivery_snapshot in focused_persistence.items():
        assert delivery_snapshot["canonical_parent_handle"] == (
            delivery_snapshot["provenance_parent_handle"]
        )
        assert bpy.data.objects.get(
            delivery_snapshot["canonical_parent_handle"]
        ) is not None
        for entity_snapshot in delivery_snapshot["entities"]:
            _assert_reopened_physical_entity(
                entity_snapshot,
                verify_packed_sha256,
            )
    _assert_provenance_parent_links(full_records, welding_provenance_path)
    if aws_provenance_path is not None:
        _assert_provenance_parent_links(aws_records, aws_provenance_path)

    reopened_raster = bpy.data.objects.get(raster_object_name)
    assert reopened_raster is not None and reopened_raster.type == "MESH"
    reopened_image = bpy.data.images.get(str(reopened_raster["pdf_image_datablock"]))
    assert reopened_image is not None
    assert verify_packed_sha256(reopened_image, raster_image_sha) == raster_image_sha
    reopened_aws_raster = bpy.data.objects.get(aws_raster_object_name)
    assert reopened_aws_raster is not None and reopened_aws_raster.type == "MESH"
    reopened_aws_image = bpy.data.images.get(aws_raster_image_name)
    reopened_aws_material = bpy.data.materials.get(aws_raster_material_name)
    assert reopened_aws_image is not None and reopened_aws_material is not None
    assert verify_packed_sha256(
        reopened_aws_image,
        aws_raster_image_sha,
    ) == aws_raster_image_sha
    assert any(
        _same_blender_identity(candidate, reopened_aws_material)
        for candidate in reopened_aws_raster.data.materials
    )
    assert _raster_material_chain_verified(
        reopened_aws_material,
        reopened_aws_image,
    )
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
    results["aws_image_only_validation"] = {
        "semantic_text_spans": 0,
        "semantic_text_delivery_records": 0,
        "rotated_raster_page_persisted": True,
        "actual_raster_import_plane_persisted": True,
        "report": str(aws_report_path),
    }
    results["persistence"] = {
        "blend": str(blend_path),
        "reopened_text_entities": reopened_entity_count,
        "reopened_rendered_ink_entities": reopened_rendered_ink_entities,
        "reopened_rendered_font_assets": len(reopened_font_shas),
        "packed_font_cache_files_deleted": len(font_cache_files),
        "packed_rotated_raster_verified": True,
        "focused_physical_modes_reopened": sorted(focused_persistence),
        "focused_physical_entities_reopened": sum(
            len(item["entities"]) for item in focused_persistence.values()
        ),
        "canonical_semantic_3d_text_imports": 1,
        "aws_image_only_zero_text_verified": True,
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
