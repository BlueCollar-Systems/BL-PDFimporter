from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace()
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine  # noqa: E402
from pdf_vector_importer.pdfcadcore.import_report import (  # noqa: E402
    ImportReport,
    build_import_contract_ready,
)
from pdf_vector_importer.pdfcadcore.text_delivery_report import (  # noqa: E402
    build_text_representation_delivery,
    resolve_text_representation_delivery,
)


def _producer_final_state_proof(
    attempted_type: str,
    entity_ids,
    *,
    page_number: int = 1,
):
    expected_type = {
        "labels": "FONT",
        "text": "FONT",
        "3d_text": "FONT",
        "glyphs": "CURVE",
        "geometry": "MESH",
        "raster": "MESH",
    }[attempted_type]
    expectation_kind = "raster" if attempted_type == "raster" else "span"
    entities = []
    for entity_id in entity_ids:
        entity = {
            "entity_id": entity_id,
            "actual_object_type": expected_type,
            "actual_location_m": [0.0, 0.0, 0.0],
            "expected_location_m": [0.0, 0.0],
            "object_handle_verified": True,
            "source_item_verified": True,
            "character_identity_verified": None,
            "representation_fields_verified": True,
            "text_material_binding_verified": (
                None if attempted_type == "raster" else True
            ),
            "affine_verified": True,
            "physical_ink_continuity_verified": True,
            "expectation_kind": expectation_kind,
            "live_ink_element_count": 4,
            "live_ink_measurement": "evaluated_mesh_vertices",
        }
        if attempted_type == "raster":
            entity.update(
                {
                    "raster_geometry_verified": True,
                    "raster_uv_verified": True,
                    "raster_material_binding_verified": True,
                }
            )
        entities.append(entity)
    return {
        "status": "verified",
        "page_number": page_number,
        "stack_offset_m": 0.0,
        "representation": attempted_type,
        "canonical_parent_verified": True,
        "provenance_parent_handle_verified": True,
        "entities": entities,
        "failures": [],
    }


def _producer_impossibility_evidence(
    attempted_type: str,
    *,
    item_id: str = "page:1:text:1",
    page_number: int = 1,
    source_span_id: int = 1,
):
    common = {
        "importer_id": "bc_pdf_vector_importer.blender",
        "item_id": item_id,
        "page_number": page_number,
        "source_span_id": source_span_id,
    }
    if attempted_type == "labels":
        return {
            **common,
            "host": "blender",
            "host_version": "4.1.0",
            "capability": "persistent_renderable_model_label",
            "persistent": False,
            "model_scaled": False,
            "renderable": False,
        }
    return {
        **common,
        "reason": "no_exact_embedded_font_match",
        "proof_category": "source_font_absent_for_item",
        "font_name": "ExactFixtureFont",
        "font_failure_page_number": page_number,
        "font_failure_span_font_name": "ExactFixtureFont",
    }


def _attempt(
    attempted_type: str,
    status: str,
    *,
    evidence=None,
    cleanup_status: str | None = None,
    entity_ids=(),
    owned_artifacts=(),
    removed=(),
):
    if cleanup_status is None:
        cleanup_status = "not_required" if status == "delivered" else "complete"
    default_evidence = (
        {"_bind_test_impossibility": True}
        if status == "impossible" and evidence is None
        else {"actual_location_m": [0.0, 0.0]}
        if status == "delivered" and evidence is None
        else {"proof": "bound"}
    )
    attempt = {
        "attempt_index": 0,
        "attempted_representation": attempted_type,
        "status": status,
        "reason": (
            "verified"
            if status == "delivered"
            else "blender_has_no_persistent_renderable_model_label_entity_for_item"
            if status == "impossible"
            and attempted_type == "labels"
            and isinstance(evidence, dict)
            and evidence.get("importer_id") == "bc_pdf_vector_importer.blender"
            else "exact_source_font_unavailable_for_item"
            if status == "impossible"
            and isinstance(evidence, dict)
            and evidence.get("importer_id") == "bc_pdf_vector_importer.blender"
            else "item_specific_proof"
        ),
        "evidence": dict(evidence or default_evidence),
        "entity_ids": list(entity_ids),
        "owned_artifacts": [dict(artifact) for artifact in owned_artifacts],
        "superseded": status != "delivered",
        "cleanup": {"status": cleanup_status, "removed": list(removed)},
    }
    if status == "delivered":
        attempt["final_state_verification"] = _producer_final_state_proof(
            attempted_type,
            entity_ids,
        )
    return attempt


def _record(
    source_item_id: str,
    attempts,
    *,
    final_type: str | None,
    status: str = "delivered",
    requested_type: str = "text",
    source_span_id: int = 1,
):
    for attempt in attempts:
        raw_evidence = attempt.get("evidence")
        if not (
            isinstance(raw_evidence, dict)
            and raw_evidence.pop("_bind_test_impossibility", False) is True
        ):
            continue
        attempted_type = attempt.get("attempted_representation")
        attempt["reason"] = (
            "blender_has_no_persistent_renderable_model_label_entity_for_item"
            if attempted_type == "labels"
            else "exact_source_font_unavailable_for_item"
        )
        attempt["evidence"] = _producer_impossibility_evidence(
            attempted_type,
            item_id=source_item_id,
            page_number=1,
            source_span_id=source_span_id,
        )
    entity_ids = list(attempts[-1].get("entity_ids") or []) if attempts else []
    return {
        "item_id": source_item_id,
        "page": 1,
        "source_span_id": source_span_id,
        "requested_representation": requested_type,
        "final_representation": final_type,
        "status": status,
        "fallback_attempted": len(attempts) > 1,
        "fallback_used": bool(
            status == "delivered" and final_type and final_type != requested_type
        ),
        "entity_ids": entity_ids,
        "attempts": list(attempts),
    }


def _raster_fallback_attempts(
    *,
    first_attempt=None,
    entity_ids=("Raster001",),
):
    attempts = [
        first_attempt or _attempt("text", "impossible"),
        _attempt("3d_text", "impossible"),
        _attempt("glyphs", "impossible"),
        _attempt("geometry", "impossible"),
        _attempt("raster", "delivered", entity_ids=entity_ids),
    ]
    for index, attempt in enumerate(attempts):
        attempt["attempt_index"] = index
    return attempts


def _projection(records, *, expected_ids, requested_type="text"):
    ledger = bl_import_engine._canonical_text_delivery_attempts(records)
    delivery = build_text_representation_delivery(
        ledger,
        requested_type=requested_type,
        required=True,
        expected_source_item_ids=expected_ids,
    )
    return ledger, delivery


def _contract_report(ledger, delivery, obligations):
    return ImportReport(
        report_meta={"build_stamp": "test-build"},
        result={"status": "success", "text_entities": 0},
        extra={
            "scale_crosscheck": {},
            "result_status": "success",
            "import_text": True,
            "text_mode": "text",
            "text_delivery_attempts": ledger,
            "text_representation_delivery": delivery,
            "text_delivery_obligations": obligations,
        },
    )


def test_blender_normalizes_records_into_shared_ledger_and_terminal_indexes() -> None:
    sentinel = "UNIQUE-EVIDENCE-SENTINEL-" + ("x" * 16_384)
    direct = _record(
        "page:1:text:1",
        [_attempt("text", "delivered", entity_ids=["Text001"])],
        final_type="text",
        source_span_id=1,
    )
    fallback_attempts = _raster_fallback_attempts(
        first_attempt=_attempt(
            "text",
            "impossible",
            evidence={
                **_producer_impossibility_evidence(
                    "text",
                    item_id="page:1:text:2",
                    source_span_id=2,
                ),
                "proof_blob": sentinel,
            },
        ),
        entity_ids=("Raster002",),
    )
    fallback = _record(
        "page:1:text:2",
        fallback_attempts,
        final_type="raster",
        source_span_id=2,
    )

    ledger, delivery = _projection(
        [direct, fallback],
        expected_ids={"page:1:text:1", "page:1:text:2"},
    )

    assert json.dumps({"ledger": ledger, "delivery": delivery}).count(sentinel) == 1
    assert len(ledger) == 6
    assert [attempt["source_item_id"] for attempt in ledger] == [
        "page:1:text:1",
        "page:1:text:2",
        "page:1:text:2",
        "page:1:text:2",
        "page:1:text:2",
        "page:1:text:2",
    ]
    assert [attempt["outcome"] for attempt in ledger] == [
        "verified",
        "proven_impossible",
        "proven_impossible",
        "proven_impossible",
        "proven_impossible",
        "verified",
    ]
    required_fields = {
        "source_item_id",
        "requested_type",
        "attempted_type",
        "final_type",
        "outcome",
        "cleanup_complete",
        "created_entity_ids",
        "removed_entity_ids",
        "delivery_entity_ids",
        "support_entity_ids",
        "referenced_entity_ids",
        "reused_entity_ids",
        "record_verified",
        "type_verified",
        "visual_verified",
        "ownership_verified",
        "evidence",
    }
    assert all(required_fields.issubset(attempt) for attempt in ledger)
    assert delivery == {
        "schema": "bcs.text_representation_delivery/1.1",
        "required": True,
        "requested_type": "text",
        "verified": True,
        "attempt_count": 6,
        "source_item_count": 2,
        "delivered_item_count": 2,
        "failed_item_count": 0,
        "items": [
            {
                "source_item_id": "page:1:text:1",
                "terminal_attempt_index": 0,
                "final_type": "text",
                "verified": True,
            },
            {
                "source_item_id": "page:1:text:2",
                "terminal_attempt_index": 5,
                "final_type": "raster",
                "verified": True,
            },
        ],
        "invalid_reasons": [],
    }
    resolution = resolve_text_representation_delivery(
        ledger,
        delivery,
        expected_source_item_ids={"page:1:text:1", "page:1:text:2"},
    )
    assert resolution["verified"] is True
    assert resolution["terminal_attempts"] == [ledger[0], ledger[5]]


def test_blender_rejects_attempt_sequence_that_skips_fallback_ladder_rungs() -> None:
    attempts = [
        _attempt("text", "impossible"),
        _attempt("raster", "delivered", entity_ids=["Raster001"]),
    ]
    attempts[1]["attempt_index"] = 1

    ledger, delivery = _projection(
        [_record("page:1:text:1", attempts, final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    assert [attempt["attempted_type"] for attempt in ledger] == ["text", "raster"]
    assert ledger[-1]["attempt_sequence_verified"] is False
    assert ledger[-1]["record_verified"] is False
    assert ledger[-1]["type_verified"] is False
    assert ledger[-1]["visual_verified"] is False
    assert ledger[-1]["ownership_verified"] is False
    assert delivery["verified"] is False


@pytest.mark.parametrize(
    "tamper",
    ["shallow", "item_identity", "proof_category", "reason"],
)
def test_blender_rejects_nonterminal_impossibility_without_deep_proof(tamper) -> None:
    prior = _attempt("text", "impossible")
    terminal = _attempt("3d_text", "delivered", entity_ids=["Text3D001"])
    terminal["attempt_index"] = 1
    record = _record(
        "page:1:text:1",
        [prior, terminal],
        final_type="3d_text",
    )
    if tamper == "shallow":
        prior["reason"] = "item_specific_proof"
        prior["evidence"] = {"proof": "bound"}
    elif tamper == "item_identity":
        prior["evidence"]["item_id"] = "page:1:text:999"
    elif tamper == "proof_category":
        prior["evidence"]["proof_category"] = "claimed_only"
    else:
        prior["reason"] = "item_specific_proof"

    ledger, delivery = _projection(
        [record],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["impossibility_proof_verified"] is False
    assert ledger[0]["outcome"] == "failed"
    assert ledger[-1]["attempt_sequence_verified"] is False
    assert delivery["verified"] is False


@pytest.mark.parametrize(
    "tamper",
    [
        "superficial",
        "page",
        "stack_offset",
        "numeric_stack_offset",
        "canonical_parent",
        "provenance_parent",
        "entity_id",
        "object_type",
        "actual_location",
        "expected_location",
        "missing_expected_location",
        "nonfinite_unstacked_location",
        "object_handle",
        "source_item",
        "character_identity",
        "expectation_kind",
        "representation_fields",
        "material_binding",
        "affine",
        "physical_ink",
        "live_ink_count",
        "live_ink_measurement",
        "test_only_live_ink_measurement",
    ],
)
def test_blender_rejects_unbound_or_superficial_final_state_proof(tamper) -> None:
    terminal = _attempt("text", "delivered", entity_ids=["Text001"])
    proof = terminal["final_state_verification"]
    entity = proof["entities"][0]
    if tamper == "superficial":
        terminal["final_state_verification"] = {
            "status": "verified",
            "representation": "text",
            "failures": [],
        }
    elif tamper == "page":
        proof["page_number"] = 99
    elif tamper == "stack_offset":
        proof["stack_offset_m"] = "0.0"
    elif tamper == "numeric_stack_offset":
        proof["stack_offset_m"] = 0.25
    elif tamper == "canonical_parent":
        proof["canonical_parent_verified"] = False
    elif tamper == "provenance_parent":
        proof["provenance_parent_handle_verified"] = False
    elif tamper == "entity_id":
        entity["entity_id"] = "OtherText"
    elif tamper == "object_type":
        entity["actual_object_type"] = "MESH"
    elif tamper == "actual_location":
        entity["actual_location_m"] = [0.0, "invalid", 0.0]
    elif tamper == "expected_location":
        entity["expected_location_m"] = [1.0, 1.0]
    elif tamper == "missing_expected_location":
        entity.pop("expected_location_m")
    elif tamper == "nonfinite_unstacked_location":
        terminal["evidence"]["actual_location_m"] = [float("nan"), 0.0]
    elif tamper == "object_handle":
        entity["object_handle_verified"] = False
    elif tamper == "source_item":
        entity["source_item_verified"] = False
    elif tamper == "character_identity":
        entity["character_identity_verified"] = True
    elif tamper == "expectation_kind":
        entity["expectation_kind"] = "character"
        entity["character_identity_verified"] = True
        entity["expected_world_ink_bounds_m"] = [0.0, 0.0, 1.0, 1.0]
        entity["actual_world_ink_bounds_m"] = [0.0, 0.0, 1.0, 1.0]
    elif tamper == "representation_fields":
        entity["representation_fields_verified"] = False
    elif tamper == "material_binding":
        entity["text_material_binding_verified"] = False
    elif tamper == "affine":
        entity["affine_verified"] = False
    elif tamper == "physical_ink":
        entity["physical_ink_continuity_verified"] = False
    elif tamper == "live_ink_count":
        entity["live_ink_element_count"] = 0
    elif tamper == "live_ink_measurement":
        entity["live_ink_measurement"] = "claimed_measurement"
    elif tamper == "test_only_live_ink_measurement":
        entity["live_ink_measurement"] = "explicit_test_host_points"

    ledger, delivery = _projection(
        [_record("page:1:text:1", [terminal], final_type="text")],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[-1]["type_verified"] is False
    assert ledger[-1]["visual_verified"] is False
    assert delivery["verified"] is False


@pytest.mark.parametrize(
    "field",
    [
        "raster_geometry_verified",
        "raster_uv_verified",
        "raster_material_binding_verified",
    ],
)
def test_blender_rejects_unverified_raster_continuity(field) -> None:
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    entity = terminal["final_state_verification"]["entities"][0]
    entity[field] = False

    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                [terminal],
                final_type="raster",
                requested_type="raster",
            )
        ],
        expected_ids={"page:1:text:1"},
        requested_type="raster",
    )

    assert ledger[-1]["visual_verified"] is False
    assert delivery["verified"] is False


@pytest.mark.parametrize(
    "tamper",
    ["requested", "attempted", "final", "proof"],
)
def test_blender_rejects_representation_evidence_repaired_by_trimming(tamper) -> None:
    terminal = _attempt("text", "delivered", entity_ids=["Text001"])
    record = _record("page:1:text:1", [terminal], final_type="text")
    if tamper == "requested":
        record["requested_representation"] = " text "
    elif tamper == "attempted":
        terminal["attempted_representation"] = " text "
    elif tamper == "final":
        record["final_representation"] = " text "
    else:
        terminal["final_state_verification"]["representation"] = " text "

    ledger, delivery = _projection(
        [record],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[-1]["record_verified"] is False
    assert delivery["verified"] is False


def test_blender_rejects_whitespace_padded_source_identity() -> None:
    records = [
        _record(
            " page:1:text:1 ",
            [_attempt("text", "delivered", entity_ids=["Text001"])],
            final_type="text",
        )
    ]

    ledger, delivery = _projection(
        records,
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["source_item_id"] == ""
    assert ledger[0]["host_record"]["item_id"] == " page:1:text:1 "
    assert delivery["verified"] is False
    assert any(
        "source_item_id is not an exact nonempty string" in reason
        for reason in delivery["invalid_reasons"]
    )
    resolution = resolve_text_representation_delivery(
        ledger,
        delivery,
        expected_source_item_ids={"page:1:text:1"},
    )
    assert resolution["verified"] is False


def test_blender_preserves_prior_attempt_cleanup_ownership_evidence() -> None:
    prior = _attempt(
        "text",
        "impossible",
        owned_artifacts=[
            {
                "object_id": "TemporaryText",
                "datablock_id": "TemporaryCurve",
                "datablock_kind": "CURVE",
                "ownership": "created_by_this_item_attempt",
            }
        ],
        removed=["TemporaryText", "TemporaryCurve"],
    )
    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                _raster_fallback_attempts(first_attempt=prior),
                final_type="raster",
            )
        ],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"] == [
        "blender:attempt:page:1:text:1:0:object:TemporaryText",
        "blender:attempt:page:1:text:1:0:datablock:curve:TemporaryCurve",
    ]
    assert ledger[0]["removed_entity_ids"] == [
        "blender:attempt:page:1:text:1:0:object:TemporaryText",
        "blender:attempt:page:1:text:1:0:datablock:curve:TemporaryCurve",
    ]
    assert delivery["verified"] is True


def test_blender_partitions_terminal_delivery_and_support_ownership() -> None:
    terminal = _attempt(
        "text",
        "delivered",
        entity_ids=["Text001"],
        owned_artifacts=[
            {
                "object_id": "Text001",
                "datablock_id": "TextCurve001",
                "datablock_kind": "CURVE",
                "ownership": "created_by_this_item_attempt",
            }
        ],
    )

    ledger, delivery = _projection(
        [_record("page:1:text:1", [terminal], final_type="text")],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"] == [
        "blender:object:Text001",
        "blender:datablock:curve:TextCurve001",
    ]
    assert ledger[0]["delivery_entity_ids"] == ["blender:object:Text001"]
    assert ledger[0]["support_entity_ids"] == [
        "blender:datablock:curve:TextCurve001"
    ]
    assert ledger[0]["removed_entity_ids"] == []
    assert ledger[0]["reused_entity_ids"] == []
    assert ledger[0]["ownership_verified"] is True
    assert delivery["verified"] is True


def test_blender_preserves_invalid_padded_entity_id_for_fail_closed_validation() -> None:
    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                [_attempt("text", "delivered", entity_ids=[" Text001 "])],
                final_type="text",
            )
        ],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["delivery_entity_ids"] == [" Text001 "]
    assert delivery["verified"] is False
    assert any(
        "delivery_entity_ids is invalid" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_blender_canonical_ids_do_not_alias_object_and_datablock_namespaces() -> None:
    first = _record(
        "page:1:text:1",
        [
            _attempt(
                "text",
                "delivered",
                entity_ids=["SharedName"],
                owned_artifacts=[
                    {
                        "object_id": "SharedName",
                        "datablock_id": "FirstCurve",
                        "datablock_kind": "CURVE",
                        "ownership": "created_by_this_item_attempt",
                    }
                ],
            )
        ],
        final_type="text",
    )
    second = _record(
        "page:1:text:2",
        [
            _attempt(
                "text",
                "delivered",
                entity_ids=["SecondObject"],
                owned_artifacts=[
                    {
                        "object_id": "SecondObject",
                        "datablock_id": "SharedName",
                        "datablock_kind": "CURVE",
                        "ownership": "created_by_this_item_attempt",
                    }
                ],
            )
        ],
        final_type="text",
        source_span_id=2,
    )

    ledger, delivery = _projection(
        [first, second],
        expected_ids={"page:1:text:1", "page:1:text:2"},
    )

    assert ledger[0]["delivery_entity_ids"] == ["blender:object:SharedName"]
    assert ledger[1]["support_entity_ids"] == [
        "blender:datablock:curve:SharedName"
    ]
    assert set(ledger[0]["delivery_entity_ids"]).isdisjoint(
        ledger[1]["support_entity_ids"]
    )
    assert delivery["verified"] is True


def test_blender_scopes_cleaned_identity_before_same_host_name_is_recreated() -> None:
    prior = _attempt(
        "text",
        "impossible",
        owned_artifacts=[
            {
                "object_id": "RecreatedObject",
                "datablock_id": "DiscardedCurve",
                "datablock_kind": "CURVE",
                "ownership": "created_by_this_item_attempt",
            }
        ],
        removed=["RecreatedObject", "DiscardedCurve"],
    )
    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                _raster_fallback_attempts(
                    first_attempt=prior,
                    entity_ids=("RecreatedObject",),
                ),
                final_type="raster",
            )
        ],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"][0] == (
        "blender:attempt:page:1:text:1:0:object:RecreatedObject"
    )
    assert ledger[0]["created_entity_ids"] == ledger[0]["removed_entity_ids"]
    assert ledger[-1]["created_entity_ids"] == [
        "blender:object:RecreatedObject"
    ]
    assert delivery["verified"] is True


def test_blender_binds_duplicate_cleanup_names_across_host_namespaces() -> None:
    prior = _attempt(
        "text",
        "impossible",
        owned_artifacts=[
            {
                "object_id": "SameRawName",
                "datablock_id": "SameRawName",
                "datablock_kind": "CURVE",
                "ownership": "created_by_this_item_attempt",
            }
        ],
        removed=["SameRawName", "SameRawName"],
    )
    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                _raster_fallback_attempts(first_attempt=prior),
                final_type="raster",
            )
        ],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"] == [
        "blender:attempt:page:1:text:1:0:object:SameRawName",
        "blender:attempt:page:1:text:1:0:datablock:curve:SameRawName",
    ]
    assert ledger[0]["removed_entity_ids"] == ledger[0][
        "created_entity_ids"
    ]
    assert delivery["verified"] is True


def test_shared_projection_rejects_same_live_blender_object_across_sources() -> None:
    first = _record(
        "page:1:text:1",
        [_attempt("text", "delivered", entity_ids=["SharedObject"])],
        final_type="text",
    )
    second = _record(
        "page:1:text:2",
        [_attempt("text", "delivered", entity_ids=["SharedObject"])],
        final_type="text",
        source_span_id=2,
    )

    ledger, delivery = _projection(
        [first, second],
        expected_ids={"page:1:text:1", "page:1:text:2"},
    )

    assert ledger[0]["delivery_entity_ids"] == ledger[1][
        "delivery_entity_ids"
    ]
    assert delivery["verified"] is False
    assert any(
        "terminal retained entity identities are not unique" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_blender_rejects_missing_raw_owned_artifact_array() -> None:
    prior = _attempt("text", "impossible")
    prior.pop("owned_artifacts")
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    terminal["attempt_index"] = 1

    ledger, delivery = _projection(
        [_record("page:1:text:1", [prior, terminal], final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"] == [""]
    assert delivery["verified"] is False
    assert any(
        "created_entity_ids is invalid" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_blender_rejects_missing_raw_cleanup_removal_array() -> None:
    prior = _attempt("text", "impossible")
    prior["cleanup"].pop("removed")
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    terminal["attempt_index"] = 1

    ledger, delivery = _projection(
        [_record("page:1:text:1", [prior, terminal], final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["removed_entity_ids"] == [""]
    assert delivery["verified"] is False
    assert any(
        "removed_entity_ids is invalid" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_shared_projection_rejects_unremoved_prior_attempt_ownership() -> None:
    prior = _attempt(
        "text",
        "impossible",
        owned_artifacts=[
            {
                "object_id": "TemporaryText",
                "datablock_id": "TemporaryCurve",
                "datablock_kind": "CURVE",
                "ownership": "created_by_this_item_attempt",
            }
        ],
        removed=["TemporaryText"],
    )
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    terminal["attempt_index"] = 1

    ledger, delivery = _projection(
        [_record("page:1:text:1", [prior, terminal], final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    assert ledger[0]["created_entity_ids"] == [
        "blender:attempt:page:1:text:1:0:object:TemporaryText",
        "blender:attempt:page:1:text:1:0:datablock:curve:TemporaryCurve",
    ]
    assert ledger[0]["removed_entity_ids"] == [
        "blender:attempt:page:1:text:1:0:object:TemporaryText"
    ]
    assert delivery["verified"] is False
    assert any(
        "cleanup ownership mismatch" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_shared_projection_rejects_unproven_or_unclean_prior_attempt() -> None:
    prior = _attempt("text", "failed", cleanup_status="failed")
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    terminal["attempt_index"] = 1
    ledger, delivery = _projection(
        [_record("page:1:text:1", [prior, terminal], final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    assert delivery["verified"] is False
    assert any("lacks impossibility proof" in reason for reason in delivery[
        "invalid_reasons"
    ])
    assert any("cleanup is incomplete" in reason for reason in delivery[
        "invalid_reasons"
    ])
    assert resolve_text_representation_delivery(ledger, delivery)["verified"] is False


def test_shared_resolver_rejects_stored_terminal_and_count_tampering() -> None:
    prior = _attempt("text", "impossible")
    terminal = _attempt("raster", "delivered", entity_ids=["Raster001"])
    terminal["attempt_index"] = 1
    ledger, delivery = _projection(
        [_record("page:1:text:1", [prior, terminal], final_type="raster")],
        expected_ids={"page:1:text:1"},
    )

    tampered = json.loads(json.dumps(delivery))
    tampered["items"][0]["terminal_attempt_index"] = 0
    assert resolve_text_representation_delivery(ledger, tampered)["verified"] is False
    tampered = json.loads(json.dumps(delivery))
    tampered["items"][0]["final_type"] = "geometry"
    assert resolve_text_representation_delivery(ledger, tampered)["verified"] is False
    tampered = json.loads(json.dumps(delivery))
    tampered["delivered_item_count"] = 999
    assert resolve_text_representation_delivery(ledger, tampered)["verified"] is False
    tampered_ledger = json.loads(json.dumps(ledger))
    tampered_ledger[-1]["requested_type"] = "geometry"
    assert resolve_text_representation_delivery(
        tampered_ledger,
        delivery,
    )["verified"] is False


def test_import_report_serializes_full_evidence_only_in_canonical_ledger(
    monkeypatch,
    tmp_path: Path,
) -> None:
    sentinel = "REPORT-ONLY-ONCE-" + ("z" * 32_768)
    attempts = _raster_fallback_attempts(
        first_attempt=_attempt(
            "text",
            "impossible",
            evidence={
                **_producer_impossibility_evidence("text"),
                "large_unique_blob": sentinel,
            },
        )
    )
    provenance = types.SimpleNamespace(
        _text_delivery_records=[
            _record("page:1:text:1", attempts, final_type="raster")
        ],
        _text_delivered_entity_counts={"raster_patch": 1},
    )
    report_path = tmp_path / "import-report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")

    bl_import_engine.write_import_report(
        str(tmp_path / "input.pdf"),
        {"import_text": True, "text_mode": "text"},
        {
            "pages_imported": 1,
            "primitives": 0,
            "text_items": 1,
            "text_source_spans": 1,
            "text_source_item_ids": ["page:1:text:1"],
            "collections": 1,
            "elapsed": 0.01,
        },
        import_mode="vector",
        output_path=str(report_path),
        provenance_opts=provenance,
    )

    raw_report = report_path.read_text(encoding="utf-8")
    assert raw_report.count(sentinel) == 1
    report = json.loads(raw_report)
    assert set(report["extra"]["text_delivery"]) == {"schema", "summary"}
    ledger = report["extra"]["text_delivery_attempts"]
    delivery = report["extra"]["text_representation_delivery"]
    assert ledger[0]["evidence"] == {
        **_producer_impossibility_evidence("text"),
        "large_unique_blob": sentinel,
    }
    assert delivery["schema"] == "bcs.text_representation_delivery/1.1"
    assert delivery["verified"] is True
    assert report["extra"]["text_delivery_obligations"] == {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": True,
        "requested_type": "text",
        "source_item_ids": ["page:1:text:1"],
    }
    assert resolve_text_representation_delivery(
        ledger,
        delivery,
        expected_source_item_ids={"page:1:text:1"},
    )["verified"] is True
    assert report["extra"]["import_contract_ready"]["checks"][
        "text_delivery"
    ] is True


def test_import_report_normalizes_text3d_alias_before_shared_projection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    provenance = types.SimpleNamespace(
        _text_delivery_records=[
            _record(
                "page:1:text:1",
                [_attempt("3d_text", "delivered", entity_ids=["Text3D001"])],
                final_type="3d_text",
                requested_type="3d_text",
            )
        ],
        _text_delivered_entity_counts={"native_3d_text": 1},
    )
    report_path = tmp_path / "text3d-alias-report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")

    bl_import_engine.write_import_report(
        str(tmp_path / "input.pdf"),
        {"import_text": True, "text_mode": "text3d"},
        {
            "pages_imported": 1,
            "text_items": 1,
            "text_source_spans": 1,
            "text_source_item_ids": ["page:1:text:1"],
            "collections": 1,
            "elapsed": 0.01,
        },
        import_mode="vector",
        output_path=str(report_path),
        provenance_opts=provenance,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    delivery = report["extra"]["text_representation_delivery"]
    assert delivery["requested_type"] == "3d_text"
    assert delivery["verified"] is True


def test_import_contract_recomputes_ledger_and_binds_independent_obligations() -> None:
    ledger, delivery = _projection(
        [
            _record(
                "page:1:text:1",
                [_attempt("text", "delivered", entity_ids=["Text001"])],
                final_type="text",
            )
        ],
        expected_ids=["page:1:text:1"],
    )
    obligations = {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": True,
        "requested_type": "text",
        "source_item_ids": ["page:1:text:1"],
    }
    report = _contract_report(ledger, delivery, obligations)

    assert build_import_contract_ready(report)["ready"] is True

    missing_ledger = _contract_report(ledger, delivery, obligations)
    del missing_ledger.extra["text_delivery_attempts"]
    assert build_import_contract_ready(missing_ledger)["ready"] is False

    mutated_ledger = _contract_report(
        json.loads(json.dumps(ledger)),
        delivery,
        obligations,
    )
    mutated_ledger.extra["text_delivery_attempts"][0]["outcome"] = "failed"
    assert build_import_contract_ready(mutated_ledger)["ready"] is False

    mutated_obligations = _contract_report(
        ledger,
        delivery,
        dict(obligations, source_item_ids=["page:1:text:999"]),
    )
    assert build_import_contract_ready(mutated_obligations)["ready"] is False


def test_import_contract_accepts_verified_zero_obligations() -> None:
    ledger = []
    delivery = build_text_representation_delivery(
        ledger,
        requested_type="text",
        required=False,
        expected_source_item_ids=[],
    )
    obligations = {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": False,
        "requested_type": "text",
        "source_item_ids": [],
    }

    readiness = build_import_contract_ready(
        _contract_report(ledger, delivery, obligations)
    )

    assert delivery["verified"] is True
    assert readiness["ready"] is True
    assert readiness["checks"]["text_delivery"] is True
