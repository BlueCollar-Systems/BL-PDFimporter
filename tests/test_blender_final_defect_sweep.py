"""Regression gates for the final Blender defect sweep."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_representation_fidelity_blender as fidelity  # noqa: E402
import test_terminal_raster_delivery_blender as terminal_raster  # noqa: E402


bl_import_engine = fidelity.bl_import_engine
bl_text_builder = fidelity.bl_text_builder


def test_delivery_controller_preserves_exception_attached_exact_ownership():
    owned_object = object()
    owned_datablock = object()
    owned_file = ("C:/import-temp/item.png", "C:/import-temp")
    captured = []

    def explode(_representation):
        error = RuntimeError("host callback failed after construction")
        error.owned_artifacts = ({"object_id": "attempt-object"},)
        error.owned_objects = (owned_object,)
        error.owned_datablocks = (owned_datablock,)
        error.owned_files = (owned_file,)
        raise error

    def cleanup(outcome):
        captured.append(outcome)
        return {"status": "complete", "removed": []}

    entity, record = fidelity.deliver_item(
        item_id="page:2:text:41",
        page_number=2,
        source_span_id=41,
        requested="text",
        attempt=explode,
        cleanup=cleanup,
    )

    assert entity is None
    assert record["status"] == "failed"
    assert len(captured) == 1
    assert tuple(captured[0].owned_objects) == (owned_object,)
    assert tuple(captured[0].owned_datablocks) == (owned_datablock,)
    assert tuple(captured[0].owned_files) == (owned_file,)


def test_failed_cleanup_retains_exact_runtime_outcome_for_remediation(monkeypatch):
    fake, collection = fidelity._install(monkeypatch)

    class _BrokenNodes:
        def clear(self):
            pass

        def new(self, **_kwargs):
            raise RuntimeError("node factory failed")

    material = fidelity._Material("attempt-material")
    material.node_tree = types.SimpleNamespace(nodes=_BrokenNodes(), links=object())

    class _FailingMaterials:
        @staticmethod
        def new(**_kwargs):
            return material

        @staticmethod
        def remove(_value):
            raise RuntimeError("material removal blocked")

        @staticmethod
        def get(name):
            return material if name == material.name else None

    fake.data.materials = _FailingMaterials()
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        fidelity._item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    record = opts._text_delivery_records[-1]
    assert record["attempts"][0]["cleanup"]["status"] == "failed"
    retained = opts._text_cleanup_outcomes[record["item_id"]]
    assert material in tuple(retained.owned_datablocks)


@pytest.mark.parametrize(
    ("mode", "attempt_name"),
    (("glyphs", "_attempt_glyphs"), ("geometry", "_attempt_geometry")),
)
def test_superseded_source_cleanup_failure_retains_shared_material_ref(
    monkeypatch,
    mode,
    attempt_name,
):
    fake, collection = fidelity._install(monkeypatch)

    def blocked_cleanup(obj, data, _collection):
        return (
            {"status": "failed", "removed": [], "detail": "data removal blocked"},
            (obj,),
            (data,),
        )

    monkeypatch.setattr(bl_text_builder, "_remove_object_and_data", blocked_cleanup)
    outcome = getattr(bl_text_builder, attempt_name)(
        fidelity._item(),
        collection,
        page_number=2,
        requested=mode,
        item_id="page:2:text:41",
        visual_style="source",
        z_offset_m=0.0,
    )

    assert outcome.status == "failed"
    material = next(iter(fake.data.materials.items.values()))
    assert material in tuple(outcome.owned_datablocks)


def test_raster_ownership_uses_exact_bound_refs_when_registries_are_unreadable(
    monkeypatch,
    tmp_path,
):
    fake, _collection = fidelity._install(monkeypatch)
    image = fake.data.images.add_packed("owned-image", b"owned")
    material = fake.data.materials.add(fidelity._Material("owned-material", image))
    plane = fidelity._Object("owned-plane", fidelity._MeshData("owned-mesh"))
    plane.data.materials.append(material)
    plane["pdf_image_material"] = material.name
    plane["pdf_image_material_owned"] = True
    plane["pdf_image_datablock"] = image.name
    plane["pdf_image_datablock_owned"] = True
    plane["pdf_image_path"] = str(tmp_path / "owned.png")
    plane["pdf_image_owner_root"] = str(tmp_path)

    def unreadable(_name):
        raise RuntimeError("registry lookup unavailable")

    fake.data.materials.get = unreadable
    fake.data.images.get = unreadable

    ownership = bl_text_builder._raster_attempt_ownership(plane)
    frozen = bl_import_engine._freeze_created_image_plane_ownership(plane)

    assert tuple(ownership[1]) == (plane,)
    assert tuple(ownership[2]) == (plane.data, material, image)
    assert frozen["object"] is plane
    assert frozen["mesh"] is plane.data
    assert frozen["material"] is material
    assert frozen["image"] is image


def test_raster_attempt_ownership_never_trusts_unbound_mutable_names(monkeypatch):
    fake, _collection = fidelity._install(monkeypatch)
    user_image = fake.data.images.add_packed("user-image", b"user")
    user_material = fake.data.materials.add(
        fidelity._Material("user-material", user_image)
    )
    plane = fidelity._Object("attempt-plane", fidelity._MeshData("attempt-mesh"))
    plane["pdf_image_material"] = user_material.name
    plane["pdf_image_material_owned"] = True
    plane["pdf_image_datablock"] = user_image.name
    plane["pdf_image_datablock_owned"] = True

    ownership = bl_text_builder._raster_attempt_ownership(plane)

    assert tuple(ownership[1]) == (plane,)
    assert tuple(ownership[2]) == (plane.data,)
    assert user_material not in ownership[2]
    assert user_image not in ownership[2]


def test_raster_callback_partial_creation_exception_retains_ownership(
    monkeypatch,
    tmp_path,
):
    fake, collection = fidelity._install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    create_plane = fidelity._raster_callback_for_test(clip)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    def create_then_raise(*args, **kwargs):
        plane = create_plane(*args, **kwargs)
        material = fake.data.materials.get(plane["pdf_image_material"])
        image = fake.data.images.get(plane["pdf_image_datablock"])
        error = RuntimeError("callback failed after linking plane")
        error.owned_artifacts = (bl_text_builder._raster_artifact(plane),)
        error.owned_objects = (plane,)
        error.owned_datablocks = (plane.data, material, image)
        error.owned_files = ((str(clip), str(tmp_path)),)
        raise error

    obj = bl_text_builder.build_text(
        fidelity._item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=create_then_raise,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["reason"] == "terminal_raster_attempt_raised"
    assert attempt["cleanup"]["status"] == "complete"
    assert collection.objects.items == []
    assert fake.data.materials.removed == ["P2_text_41_raster_material"]
    assert fake.data.images.removed == ["P2_text_41_raster_image"]
    assert not clip.exists()


def test_terminal_raster_metadata_cleanup_failure_retains_frozen_ledger(
    monkeypatch,
    tmp_path,
):
    plane = terminal_raster._MetadataRejectingRasterObject()
    monkeypatch.setattr(
        bl_import_engine,
        "_create_image_plane",
        lambda *_args, **_kwargs: plane,
    )
    monkeypatch.setattr(
        bl_import_engine,
        "_remove_created_image_plane",
        lambda _ownership, _collection: {
            "status": "failed",
            "removed": [],
            "detail": "object removal blocked",
        },
    )
    item = types.SimpleNamespace(
        id=41,
        text="WELD",
        source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
        bbox=(12.0, 24.0, 52.0, 30.0),
    )

    with pytest.raises(RuntimeError) as caught:
        bl_import_engine._render_text_item_raster(
            terminal_raster._TextRasterPage(),
            item,
            object(),
            page_num=2,
            item_id="page:2:text:41",
            import_cfg=types.SimpleNamespace(raster_dpi=288),
            image_dir=str(tmp_path),
        )

    error = caught.value
    assert tuple(error.owned_objects) == (plane,)
    assert plane.data in tuple(error.owned_datablocks)
    assert len(tuple(error.owned_files)) == 1
    owned_path, owned_root = tuple(error.owned_files)[0]
    assert Path(owned_path).is_file()
    assert Path(owned_root).resolve() == tmp_path.resolve()


def test_cmap_recovered_invalid_metrics_authorize_finite_raster_fallback(monkeypatch):
    _fake, collection = fidelity._install(monkeypatch)
    item = fidelity._item()
    item.font_asset = fidelity._source_cmap_metric_font_asset()
    item.font_asset.glyph_advances = (500, 500, -1)
    item.font_name = "MetricFixture"
    item.text = " "
    item.normalized = ""
    layout = fidelity._whitespace_only_character_layout()[0]
    layout.glyph_id = None
    item.source_char_layout = (layout,)
    item.requires_individual_positioning = True
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
        terminal_raster_callback=lambda *_args, **_kwargs: None,
    )

    assert obj is None
    attempts = opts._text_delivery_records[-1]["attempts"]
    assert [entry["attempted_representation"] for entry in attempts] == [
        "3d_text",
        "text",
        "glyphs",
        "geometry",
        "raster",
    ]
    assert all(entry["status"] == "impossible" for entry in attempts[:-1])
    proof = attempts[0]["evidence"]
    assert proof["source_glyph_id"] == 2
    assert proof["source_glyph_identity"] == "exact_source_unicode_cmap"
    assert proof["source_trace_glyph_id"] is None
    assert len(proof["source_ink_mapping_sha256"]) == 64


def test_clean_positioned_baseline_capability_absence_authorizes_fallback(monkeypatch):
    fake, collection = fidelity._install(monkeypatch)

    class _NoSupportedBaselineFont(fidelity._FontData):
        @property
        def align_y(self):
            return self._align_y

        @align_y.setter
        def align_y(self, value):
            if value in {"BOTTOM_BASELINE", "BOTTOM"}:
                raise TypeError(f"{value} enum unavailable")
            self._align_y = value

    original_new = fake.data.curves.new

    def no_baseline_probe(name, type):
        if name == "BCPDF_PositionedBaselineProbe" and type == "FONT":
            return _NoSupportedBaselineFont(name)
        return original_new(name, type)

    monkeypatch.setattr(fake.data.curves, "new", no_baseline_probe)
    item = fidelity._item()
    item.font_asset = fidelity._metric_font_asset()
    item.font_name = "MetricFixture"
    item.text = "A"
    item.normalized = "A"
    item.source_char_layout = (fidelity._character_layout()[0],)
    item.requires_individual_positioning = True
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
        terminal_raster_callback=lambda *_args, **_kwargs: None,
    )

    assert obj is None
    attempts = opts._text_delivery_records[-1]["attempts"]
    assert len(attempts) == 5
    assert all(entry["status"] == "impossible" for entry in attempts[:-1])
    assert all(
        entry["reason"]
        == "positioned_font_baseline_alignment_unavailable_for_item"
        for entry in attempts[:-1]
    )
    proof = attempts[0]["evidence"]
    assert proof["capability"] == "FONT.align_y.BOTTOM_BASELINE_or_BOTTOM"
    assert proof["capability_present"] is False


def test_positioned_baseline_probe_cleanup_fault_stops_without_fallback(monkeypatch):
    fake, collection = fidelity._install(monkeypatch)

    def fail_cleanup(_probe):
        raise RuntimeError("baseline probe cleanup blocked")

    monkeypatch.setattr(fake.data.curves, "remove", fail_cleanup)
    item = fidelity._item()
    item.font_asset = fidelity._metric_font_asset()
    item.font_name = "MetricFixture"
    item.text = "A"
    item.normalized = "A"
    item.source_char_layout = (fidelity._character_layout()[0],)
    item.requires_individual_positioning = True
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")
    raster_calls = []

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
        terminal_raster_callback=lambda *_args, **_kwargs: raster_calls.append(True),
    )

    assert obj is None
    attempts = opts._text_delivery_records[-1]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["reason"] == (
        "positioned_baseline_capability_probe_failed_not_impossibility_proof"
    )
    assert raster_calls == []


def test_initial_text_material_verifier_requires_exact_bsdf_surface_socket(monkeypatch):
    fake, collection = fidelity._install(monkeypatch)
    original = bl_text_builder._get_or_create_text_material

    def wrong_surface_socket(*args, **kwargs):
        material = original(*args, **kwargs)
        link = next(
            candidate
            for candidate in material.node_tree.links
            if candidate.from_node.type == "BSDF_PRINCIPLED"
        )
        link.to_socket = link.to_node.inputs["Alpha"]
        return material

    monkeypatch.setattr(
        bl_text_builder,
        "_get_or_create_text_material",
        wrong_surface_socket,
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")

    obj = bl_text_builder.build_text(
        fidelity._item(),
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert "text_material_node_mode_unverified" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert fake.data.materials.removed


def test_initial_text_material_verifier_binds_color_and_surface_to_same_shader(
    monkeypatch,
):
    fake, collection = fidelity._install(monkeypatch)
    original = bl_text_builder._get_or_create_text_material

    def split_color_from_surface(*args, **kwargs):
        material = original(*args, **kwargs)
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        correct_shader = next(
            node for node in nodes if node.type == "BSDF_PRINCIPLED"
        )
        output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")
        correct_link = next(
            link
            for link in links
            if link.from_node is correct_shader and link.to_node is output
        )
        links.remove(correct_link)
        wrong_shader = nodes.new(type="ShaderNodeBsdfPrincipled")
        wrong_shader.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
        wrong_shader.inputs["Alpha"].default_value = 0.25
        links.new(wrong_shader.outputs["BSDF"], output.inputs["Surface"])
        return material

    monkeypatch.setattr(
        bl_text_builder,
        "_get_or_create_text_material",
        split_color_from_surface,
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")

    obj = bl_text_builder.build_text(
        fidelity._item(),
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert "text_material_color_mismatch" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert fake.data.materials.removed


def test_exact_contour_fallback_bypasses_font_baseline_only_on_its_rung(monkeypatch):
    _fake, collection = fidelity._install(monkeypatch)
    fidelity._install_mathutils(monkeypatch)
    item = fidelity._visible_zero_advance_item()
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")

    def no_font_baseline():
        raise bl_text_builder._PositionedBaselineCapabilityAbsent(
            "host has no FONT baseline alignment"
        )

    monkeypatch.setattr(
        bl_text_builder,
        "_probe_positioned_baseline_alignment",
        no_font_baseline,
    )

    def verify_exact_contour(obj, _text_item):
        matrix = list(obj.get("pdf_affine_matrix", []))
        return [], {
            "full_affine_applied": True,
            "metric_affine_applied": True,
            "zero_ink_identity": False,
            "zero_advance_logical_proof": False,
            "evaluated_bounds_verified": True,
            "evaluated_ink_bounds_verified": True,
            "local_advance_m": 0.0,
            "intended_affine_matrix": matrix,
            "evaluated_affine_matrix": list(matrix),
        }

    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        verify_exact_contour,
    )

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
        terminal_raster_callback=lambda *_args, **_kwargs: None,
    )

    record = opts._text_delivery_records[-1]
    assert obj is not None and obj.type == "CURVE", record
    assert [entry["attempted_representation"] for entry in record["attempts"]] == [
        "3d_text",
        "text",
        "glyphs",
    ]
    assert [entry["status"] for entry in record["attempts"]] == [
        "impossible",
        "impossible",
        "delivered",
    ]
    assert record["requested_representation"] == "3d_text"
    assert record["final_representation"] == "glyphs"
    assert record["fallback_used"] is True
    assert obj["pdf_exact_contour_bypassed_host_font_shaping"] is True


@pytest.mark.parametrize(
    "corruption",
    [
        "material_unassigned",
        "diffuse_recolored",
        "shader_recolored",
        "shader_alpha_changed",
        "bsdf_wrong_surface_socket",
        "inactive_material_output",
    ],
)
def test_post_stack_text_material_seal_rejects_live_corruption_and_drops_authority(
    monkeypatch,
    corruption,
):
    fake, _collection, opts, record, objects = (
        fidelity._positioned_mixed_delivery_for_final_state(monkeypatch, "3d_text")
    )
    item_id = record["item_id"]
    entity = objects[record["entity_ids"][0]]
    material = entity.data.materials[0]
    shader = next(
        node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
    )
    output = next(
        node for node in material.node_tree.nodes if node.type == "OUTPUT_MATERIAL"
    )
    if corruption == "material_unassigned":
        user_material = fake.data.materials.add(fidelity._Material("UserMaterial"))
        entity.data.materials[0] = user_material
    elif corruption == "diffuse_recolored":
        material.diffuse_color = (0.0, 1.0, 0.0, 1.0)
    elif corruption == "shader_recolored":
        shader.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
    elif corruption == "shader_alpha_changed":
        shader.inputs["Alpha"].default_value = 0.25
    elif corruption == "bsdf_wrong_surface_socket":
        link = next(
            candidate
            for candidate in material.node_tree.links
            if candidate.from_node is shader
        )
        link.to_socket = output.inputs["Alpha"]
    elif corruption == "inactive_material_output":
        output.is_active_output = False

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=0.0,
        provenance_opts=opts,
    )

    assert len(failures) == 1
    assert any(
        failure.startswith("final_entity_text_material_binding_mismatch:")
        for failure in failures[0]["failures"]
    )
    assert failures[0]["cleanup"]["status"] == "complete"
    assert item_id not in opts._text_delivery_outcomes
    assert item_id not in opts._final_entity_expectation_authorities


@pytest.mark.parametrize("fault_target", ["material_links", "mesh_vertices"])
def test_post_stack_runtime_fault_fails_closed_cleans_and_drops_authority(
    monkeypatch,
    tmp_path,
    fault_target,
):
    fake, _collection, opts, record, obj = fidelity._raster_delivery_for_final_state(
        monkeypatch,
        tmp_path,
    )
    item_id = record["item_id"]

    class _RuntimeIterable:
        def __iter__(self):
            raise RuntimeError(f"{fault_target} became unreadable")

    if fault_target == "material_links":
        material = fake.data.materials.get(str(obj["pdf_image_material"]))
        material.node_tree.links = _RuntimeIterable()
    else:
        obj.data.vertices = _RuntimeIterable()

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=0.0,
        provenance_opts=opts,
    )

    assert len(failures) == 1
    assert failures[0]["cleanup"]["status"] == "complete"
    assert item_id not in opts._text_delivery_outcomes
    assert item_id not in opts._final_entity_expectation_authorities


def test_post_stack_registry_lookup_exception_fails_closed_per_item(monkeypatch):
    fake, _collection, opts, record, _objects = (
        fidelity._positioned_mixed_delivery_for_final_state(monkeypatch, "3d_text")
    )
    item_id = record["item_id"]

    def unreadable_registry(_entity_id):
        raise RuntimeError("object registry lookup failed")

    fake.data.objects.get = unreadable_registry

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=0.0,
        provenance_opts=opts,
    )

    assert len(failures) == 1
    assert any(
        failure.startswith("final_entity_registry_lookup_unreadable:")
        for failure in failures[0]["failures"]
    )
    assert failures[0]["cleanup"]["status"] == "complete"
    assert record["status"] == "failed"
    assert item_id not in opts._text_delivery_outcomes
    assert item_id not in opts._final_entity_expectation_authorities


def test_post_stack_cleanup_failure_drops_delivery_authority_but_retains_exact_refs(
    monkeypatch,
):
    fake, _collection, opts, record, _objects = (
        fidelity._positioned_mixed_delivery_for_final_state(monkeypatch, "3d_text")
    )
    item_id = record["item_id"]
    runtime_outcome = opts._text_delivery_outcomes[item_id]

    def unreadable_registry(_entity_id):
        raise RuntimeError("object registry lookup failed")

    fake.data.objects.get = unreadable_registry
    monkeypatch.setattr(
        bl_import_engine,
        "cleanup_delivery_outcome",
        lambda _outcome: (_ for _ in ()).throw(RuntimeError("cleanup blocked")),
    )

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=0.0,
        provenance_opts=opts,
    )

    assert len(failures) == 1
    assert failures[0]["cleanup"]["status"] == "failed"
    assert item_id not in opts._text_delivery_outcomes
    assert opts._text_cleanup_outcomes[item_id] is runtime_outcome
    assert item_id not in opts._final_entity_expectation_authorities
    assert item_id not in opts._zero_ink_source_manifests
    assert item_id not in opts._zero_ink_delivery_manifests
    assert all(
        getattr(authority, "item_id", None) != item_id
        for authority in opts._zero_ink_reconciliation_authorities
    )


def test_public_contract_locks_frozen_ownership_and_reopened_material_truth():
    repo_root = Path(__file__).resolve().parents[1]
    contract = (repo_root / "REPRESENTATION_FIDELITY.md").read_text(
        encoding="utf-8"
    )
    normalized_contract = " ".join(contract.split())

    for required in (
        "frozen at creation time",
        "importer-owned temporary root",
        "expected RGBA and shader Alpha",
        "exact BSDF-to-active-Surface socket link",
        "saved and reopened",
        "actual `import_pdf` raster-page path",
        "exact source-glyph evidence proves that every mapped glyph has zero contours",
    ):
        assert required in normalized_contract
    assert "Whitespace-only source spans legitimately have no visible geometry" not in contract
    assert "Image/material/font caches are content-addressed or explicitly shared" not in (
        contract
    )


def test_aws_acceptance_uses_actual_raster_import_and_reopens_its_plane():
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        repo_root / "tools" / "headless_text_representation_acceptance.py"
    ).read_text(encoding="utf-8")
    aws_section = source.split(
        'print("BC_BL_ACCEPTANCE_STAGE=full_import_aws:start", flush=True)',
        1,
    )[1].split(
        'print("BC_BL_ACCEPTANCE_STAGE=full_import_aws:complete", flush=True)',
        1,
    )[0]

    assert '"mode": "raster"' in aws_section
    assert '"ignore_images": False' in aws_section
    assert 'aws_stats["images"] == 1' in aws_section
    assert "aws_raster_object_name" in source
    assert "reopened_aws_raster" in source
    assert "_raster_material_chain_verified" in source
    assert "full_entity_snapshots" in source
    assert "_assert_reopened_physical_entity(" in source


def _load_headless_acceptance(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setitem(
        sys.modules,
        "mathutils",
        types.SimpleNamespace(Vector=lambda values: tuple(values)),
    )
    module_path = repo_root / "tools" / "headless_text_representation_acceptance.py"
    spec = importlib.util.spec_from_file_location(
        "_blender_headless_acceptance_regression",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reopened_curve_oracle_rejects_coordinate_corruption(monkeypatch):
    acceptance = _load_headless_acceptance(monkeypatch)
    point = types.SimpleNamespace(
        co=[0.0, 1.0, 0.0],
        handle_left=[-0.25, 0.75, 0.0],
        handle_right=[0.25, 1.25, 0.0],
        handle_left_type="FREE",
        handle_right_type="FREE",
        radius=1.0,
        tilt=0.0,
        weight_softbody=0.0,
    )
    spline = types.SimpleNamespace(
        type="BEZIER",
        use_cyclic_u=True,
        resolution_u=12,
        order_u=4,
        use_endpoint_u=False,
        use_bezier_u=False,
        bezier_points=[point],
        points=[],
    )
    curve = types.SimpleNamespace(splines=[spline])

    expected = acceptance._curve_physical_state(curve)
    point.co[0] = 99.0

    with pytest.raises(AssertionError):
        acceptance._assert_curve_physical_state(curve, expected)


def test_reopened_mesh_oracle_rejects_coordinate_corruption(monkeypatch):
    acceptance = _load_headless_acceptance(monkeypatch)
    mesh = types.SimpleNamespace(
        vertices=[
            types.SimpleNamespace(co=[0.0, 0.0, 0.0]),
            types.SimpleNamespace(co=[1.0, 0.0, 0.0]),
            types.SimpleNamespace(co=[0.0, 1.0, 0.0]),
        ],
        edges=[
            types.SimpleNamespace(vertices=[0, 1]),
            types.SimpleNamespace(vertices=[1, 2]),
            types.SimpleNamespace(vertices=[2, 0]),
        ],
        loops=[
            types.SimpleNamespace(vertex_index=0, edge_index=0),
            types.SimpleNamespace(vertex_index=1, edge_index=1),
            types.SimpleNamespace(vertex_index=2, edge_index=2),
        ],
        polygons=[
            types.SimpleNamespace(
                vertices=[0, 1, 2],
                loop_indices=[0, 1, 2],
                material_index=0,
                use_smooth=False,
            )
        ],
    )

    expected = acceptance._mesh_physical_state(mesh)
    mesh.vertices[1].co[0] = 3.0

    with pytest.raises(AssertionError):
        acceptance._assert_mesh_physical_state(mesh, expected)


def test_evaluated_physical_oracle_checks_every_text_entity(monkeypatch):
    acceptance = _load_headless_acceptance(monkeypatch)

    class _IdentityMatrix:
        def __matmul__(self, value):
            return tuple(value)

        def __iter__(self):
            return iter(
                (
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0, 0.0),
                    (0.0, 0.0, 1.0, 0.0),
                    (0.0, 0.0, 0.0, 1.0),
                )
            )

    class _Evaluated:
        def __init__(self, offset):
            self.matrix_world = _IdentityMatrix()
            self.mesh = types.SimpleNamespace(
                vertices=[
                    types.SimpleNamespace(co=[offset, 0.0, 0.0]),
                    types.SimpleNamespace(co=[offset + 1.0, 0.0, 0.0]),
                    types.SimpleNamespace(co=[offset, 1.0, 0.0]),
                ],
                edges=[types.SimpleNamespace(vertices=[0, 1])],
                polygons=[types.SimpleNamespace(vertices=[0, 1, 2])],
            )
            self.to_mesh_calls = 0
            self.to_mesh_clear_calls = 0

        def to_mesh(self, **_kwargs):
            self.to_mesh_calls += 1
            return self.mesh

        def to_mesh_clear(self):
            self.to_mesh_clear_calls += 1

    class _Object:
        def __init__(self, offset):
            self.evaluated = _Evaluated(offset)

        def evaluated_get(self, _depsgraph):
            return self.evaluated

    acceptance.bpy = types.SimpleNamespace(
        context=types.SimpleNamespace(evaluated_depsgraph_get=lambda: object())
    )
    first = _Object(0.0)
    second = _Object(10.0)

    first_state = acceptance._evaluated_physical_state(first)
    second_state = acceptance._evaluated_physical_state(second)

    assert first.evaluated.to_mesh_calls == 1
    assert second.evaluated.to_mesh_calls == 1
    assert first.evaluated.to_mesh_clear_calls == 1
    assert second.evaluated.to_mesh_clear_calls == 1
    assert first_state != second_state

    second.evaluated.mesh.vertices[0].co[0] = 11.0
    with pytest.raises(AssertionError):
        acceptance._assert_evaluated_physical_state(second, second_state)


def test_save_reopen_acceptance_wires_physical_oracle_per_entity():
    repo_root = Path(__file__).resolve().parents[1]
    source = (
        repo_root / "tools" / "headless_text_representation_acceptance.py"
    ).read_text(encoding="utf-8")
    snapshot_section = source.split("def _physical_entity_snapshot", 1)[1].split(
        "def _assert_reopened_physical_entity", 1
    )[0]
    reopened_section = source.split("def _assert_reopened_physical_entity", 1)[1].split(
        "def main", 1
    )[0]

    assert "_evaluated_physical_state(obj)" in snapshot_section
    assert "_assert_evaluated_physical_state(" in reopened_section
    assert "_curve_physical_state(obj.data)" in snapshot_section
    assert "_assert_curve_physical_state(" in reopened_section
    assert "_mesh_physical_state(obj.data)" in snapshot_section
    assert "_assert_mesh_physical_state(" in reopened_section
    assert "reopened_rendered_font_shas" not in source
