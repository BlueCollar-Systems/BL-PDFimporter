"""Finite item-scoped requested-representation delivery controller."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from typing import Any, Callable, Dict, Sequence, Tuple


REPRESENTATIONS = ("labels", "text", "3d_text", "glyphs", "geometry", "raster")
IMPORTER_ID = "bc_pdf_vector_importer.blender"
ZERO_INK_SOURCE_MANIFEST_SCHEMA = "positioned_zero_ink_source_manifest_v1"
_ZERO_INK_CHARACTER_SOURCE_FIELDS = (
    "character_item_id",
    "character_index",
    "text",
    "glyph_id",
    "advance_width_model",
    "glyph_height_model",
    "source_origin_pdf",
    "source_bbox_pdf",
    "source_quad_pdf",
    "target_origin_model",
    "target_quad_model",
)
_LADDERS = {
    "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
    "text": ("text", "3d_text", "glyphs", "geometry", "raster"),
    "3d_text": ("3d_text", "text", "glyphs", "geometry", "raster"),
    "glyphs": ("glyphs", "geometry", "raster"),
    "geometry": ("geometry", "glyphs", "raster"),
    "raster": ("raster",),
}

_FONT_PROOF_CATEGORIES = {
    "no_exact_embedded_font_match": {"source_font_absent_for_item"},
    "ambiguous_exact_embedded_font_match": {"source_font_ambiguous_for_item"},
    "embedded_font_asset_build_failed": {
        "source_specific_impossibility",
        "runtime_capability_unavailable_for_item",
    },
    "page_font_inventory_failed": {"runtime_inventory_unavailable_for_item"},
    "page_text_trace_inventory_failed": {"runtime_inventory_unavailable_for_item"},
    "invalid_page_font_record": {"source_inventory_invalid_for_page"},
    "source_document_unavailable": {
        "runtime_source_document_unavailable_for_item"
    },
}


def normalize_representation(value: object) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        mode = "3d_text"
    if mode not in REPRESENTATIONS:
        raise ValueError(
            f"Unknown requested representation: {value!r}. "
            f"Valid representations: {', '.join(REPRESENTATIONS)}."
        )
    return mode


def fallback_ladder(requested: object) -> Tuple[str, ...]:
    mode = normalize_representation(requested)
    ladder = tuple(_LADDERS[mode])
    if not ladder or ladder[0] != mode or len(ladder) != len(set(ladder)):
        raise RuntimeError(f"Invalid representation ladder for {mode!r}: {ladder!r}")
    return ladder


@dataclass
class AttemptOutcome:
    status: str
    reason: str
    entity: Any = None
    entity_ids: Sequence[str] = field(default_factory=tuple)
    evidence: Dict[str, Any] = field(default_factory=dict)
    owned_artifacts: Sequence[Dict[str, str]] = field(default_factory=tuple)
    owned_objects: Sequence[Any] = field(default_factory=tuple, repr=False)
    owned_datablocks: Sequence[Any] = field(default_factory=tuple, repr=False)

    @classmethod
    def delivered(
        cls,
        entity: Any,
        *,
        entity_ids=(),
        evidence=None,
        owned_artifacts=(),
        owned_objects=(),
        owned_datablocks=(),
    ):
        return cls(
            "delivered",
            "verified",
            entity,
            tuple(entity_ids),
            dict(evidence or {}),
            tuple(owned_artifacts),
            tuple(owned_objects),
            tuple(owned_datablocks),
        )

    @classmethod
    def impossible(
        cls,
        reason: str,
        *,
        evidence=None,
        owned_artifacts=(),
        owned_objects=(),
        owned_datablocks=(),
    ):
        return cls(
            "impossible",
            str(reason),
            None,
            (),
            dict(evidence or {}),
            tuple(owned_artifacts),
            tuple(owned_objects),
            tuple(owned_datablocks),
        )

    @classmethod
    def failed(
        cls,
        reason: str,
        *,
        evidence=None,
        owned_artifacts=(),
        owned_objects=(),
        owned_datablocks=(),
    ):
        return cls(
            "failed",
            str(reason),
            None,
            (),
            dict(evidence or {}),
            tuple(owned_artifacts),
            tuple(owned_objects),
            tuple(owned_datablocks),
        )


def _host_version_is_bound(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return len(value) >= 2 and all(str(part).strip() for part in value[:2])
    return bool(str(value or "").strip())


def _impossibility_proof_failures(
    *,
    attempted_representation: str,
    item_id: str,
    page_number: int,
    source_span_id: int,
    outcome: AttemptOutcome,
) -> list[str]:
    """Return every reason an ``impossible`` claim is not affirmative proof."""
    evidence = dict(outcome.evidence or {})
    failures: list[str] = []
    if evidence.get("importer_id") != IMPORTER_ID:
        failures.append("importer_identity_unbound")
    if str(evidence.get("item_id") or "") != str(item_id):
        failures.append("item_identity_unbound")
    try:
        evidence_page = int(evidence.get("page_number"))
    except (TypeError, ValueError):
        evidence_page = None
    if evidence_page != int(page_number):
        failures.append("page_identity_unbound")
    try:
        evidence_span = int(evidence.get("source_span_id"))
    except (TypeError, ValueError):
        evidence_span = None
    if evidence_span != int(source_span_id):
        failures.append("source_span_identity_unbound")

    reason = str(outcome.reason or "")
    if attempted_representation == "labels":
        if reason != "blender_has_no_persistent_renderable_model_label_entity_for_item":
            failures.append("label_capability_reason_not_affirmative")
        if evidence.get("host") != "blender":
            failures.append("label_host_unbound")
        if not _host_version_is_bound(evidence.get("host_version")):
            failures.append("label_host_version_unbound")
        if evidence.get("capability") != "persistent_renderable_model_label":
            failures.append("label_capability_unbound")
        for field in ("persistent", "model_scaled", "renderable"):
            if evidence.get(field) is not False:
                failures.append(f"label_{field}_absence_unproven")
        return failures

    if reason == "exact_source_font_unavailable_for_item":
        if attempted_representation not in {"text", "3d_text", "glyphs", "geometry"}:
            failures.append("font_proof_not_valid_for_rung")
        font_failure_reason = str(evidence.get("reason") or "")
        proof_category = str(evidence.get("proof_category") or "")
        allowed_categories = _FONT_PROOF_CATEGORIES.get(font_failure_reason, set())
        if proof_category not in allowed_categories:
            failures.append("font_proof_category_not_affirmative")
        if font_failure_reason == "embedded_font_asset_build_failed":
            if evidence.get("source_xref") in {None, ""}:
                failures.append("font_source_xref_unbound")
        elif font_failure_reason not in _FONT_PROOF_CATEGORIES:
            failures.append("font_absence_reason_not_affirmative")
        if proof_category.startswith("runtime_") or font_failure_reason in {
            "invalid_page_font_record",
        }:
            if not str(evidence.get("error_type") or "").strip():
                failures.append("font_failure_error_type_unbound")
            if not str(evidence.get("detail") or "").strip():
                failures.append("font_failure_detail_unbound")
        if not str(evidence.get("font_name") or "").strip():
            failures.append("font_name_unbound")
        try:
            failure_page = int(evidence.get("font_failure_page_number"))
        except (TypeError, ValueError):
            failure_page = None
        if failure_page != int(page_number):
            failures.append("font_failure_page_identity_unbound")
        failure_span_font = str(
            evidence.get("font_failure_span_font_name") or ""
        ).strip()
        if not failure_span_font or failure_span_font != str(
            evidence.get("font_name") or ""
        ).strip():
            failures.append("font_failure_span_font_identity_unbound")
        return failures

    capability_proofs = {
        "glyphs": (
            "evaluated_font_to_curve_capability_absent_for_item",
            "Object.to_curve",
        ),
        "geometry": (
            "evaluated_font_to_mesh_capability_absent_for_item",
            "meshes.new_from_object",
        ),
    }
    expected = capability_proofs.get(attempted_representation)
    if expected is None or reason != expected[0]:
        failures.append("unrecognized_impossibility_claim")
        return failures
    if evidence.get("host") != "blender":
        failures.append("capability_host_unbound")
    if not _host_version_is_bound(evidence.get("host_version")):
        failures.append("capability_host_version_unbound")
    if evidence.get("capability") != expected[1]:
        failures.append("capability_identity_unbound")
    if evidence.get("capability_present") is not False:
        failures.append("capability_absence_unproven")
    return failures


def _finite_sequence(value: Any, length: int) -> bool:
    try:
        return (
            isinstance(value, (list, tuple))
            and len(value) == int(length)
            and all(math.isfinite(float(part)) for part in value)
        )
    except (TypeError, ValueError):
        return False


def _strict_int(value: Any):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def freeze_zero_ink_source_manifest(manifest: Any) -> tuple[Dict[str, Any], str]:
    """Return an immutable-by-value JSON snapshot and its canonical digest."""
    if not isinstance(manifest, dict):
        raise TypeError("zero-ink source manifest must be a dictionary")
    payload = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    snapshot = json.loads(payload)
    return snapshot, sha256(payload.encode("utf-8")).hexdigest()


def _prepare_zero_ink_source_manifest(manifest: Any):
    if manifest is None:
        return None, None, None
    try:
        snapshot, digest = freeze_zero_ink_source_manifest(manifest)
    except (TypeError, ValueError, OverflowError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return snapshot, digest, None


def _zero_ink_manifest_contract_failures(
    manifest: Any,
    *,
    item_id: str,
    page_number: int,
    source_span_id: int,
    requested_representation: str,
) -> list[str]:
    failures: list[str] = []
    if not isinstance(manifest, dict):
        return ["zero_ink_source_manifest_invalid"]
    if manifest.get("schema") != ZERO_INK_SOURCE_MANIFEST_SCHEMA:
        failures.append("zero_ink_source_manifest_schema_unverified")
    if manifest.get("importer_id") != IMPORTER_ID:
        failures.append("zero_ink_source_manifest_importer_identity_unbound")
    if str(manifest.get("item_id") or "") != str(item_id):
        failures.append("zero_ink_source_manifest_item_identity_unbound")
    if _strict_int(manifest.get("page_number")) != int(page_number):
        failures.append("zero_ink_source_manifest_page_identity_unbound")
    if _strict_int(manifest.get("source_span_id")) != int(source_span_id):
        failures.append("zero_ink_source_manifest_span_identity_unbound")
    if manifest.get("requested_representation") != requested_representation:
        failures.append("zero_ink_source_manifest_representation_unbound")
    source_text = manifest.get("source_text")
    if not isinstance(source_text, str) or not source_text or source_text.strip():
        failures.append("zero_ink_source_manifest_text_not_zero_ink")
    characters = manifest.get("characters")
    if not isinstance(characters, list):
        failures.append("zero_ink_source_manifest_characters_invalid")
        characters = []
    character_count = _strict_int(manifest.get("character_count"))
    if character_count is None or character_count <= 0:
        failures.append("zero_ink_source_manifest_character_count_invalid")
    elif character_count != len(characters):
        failures.append("zero_ink_source_manifest_character_coverage_mismatch")
    elif "".join(
        str(character.get("text") or "")
        if isinstance(character, dict)
        else ""
        for character in characters
    ) != source_text:
        failures.append("zero_ink_source_manifest_text_coverage_mismatch")

    for expected_index, character in enumerate(characters):
        if not isinstance(character, dict):
            failures.append("zero_ink_source_manifest_character_invalid")
            continue
        expected_character_id = f"{item_id}:char:{expected_index}"
        if str(character.get("character_item_id") or "") != expected_character_id:
            failures.append("zero_ink_source_manifest_character_identity_unbound")
        if _strict_int(character.get("character_index")) != expected_index:
            failures.append("zero_ink_source_manifest_character_index_unbound")
        character_text = character.get("text")
        if (
            not isinstance(character_text, str)
            or not character_text
            or character_text.strip()
        ):
            failures.append("zero_ink_source_manifest_character_text_not_zero_ink")
        glyph_id = character.get("glyph_id")
        if glyph_id is not None and _strict_int(glyph_id) is None:
            failures.append("zero_ink_source_manifest_glyph_identity_invalid")
        for metric_field in ("advance_width_model", "glyph_height_model"):
            try:
                metric = float(character.get(metric_field))
            except (TypeError, ValueError):
                metric = math.nan
            if not math.isfinite(metric) or metric <= 0.0:
                failures.append(f"zero_ink_source_manifest_{metric_field}_invalid")
        for origin_field in ("source_origin_pdf", "target_origin_model"):
            if not _finite_sequence(character.get(origin_field), 2):
                failures.append(
                    f"zero_ink_source_manifest_{origin_field}_invalid"
                )
        if not _finite_sequence(character.get("source_bbox_pdf"), 4):
            failures.append("zero_ink_source_manifest_source_bbox_pdf_invalid")
        for quad_field in ("source_quad_pdf", "target_quad_model"):
            quad = character.get(quad_field)
            if not (
                isinstance(quad, list)
                and len(quad) == 4
                and all(_finite_sequence(point, 2) for point in quad)
            ):
                failures.append(f"zero_ink_source_manifest_{quad_field}_invalid")
    return list(dict.fromkeys(failures))


def _zero_ink_source_manifest_from_evidence(evidence: Dict[str, Any]):
    characters = evidence.get("character_entities")
    if not isinstance(characters, (list, tuple)):
        characters = ()
    return {
        "schema": evidence.get("source_manifest_schema"),
        "importer_id": evidence.get("importer_id"),
        "item_id": evidence.get("item_id"),
        "page_number": evidence.get("page_number"),
        "source_span_id": evidence.get("source_span_id"),
        "requested_representation": evidence.get("requested_representation"),
        "source_text": evidence.get("source_text"),
        "character_count": evidence.get("source_character_count"),
        "characters": [
            {
                field_name: character.get(field_name)
                for field_name in _ZERO_INK_CHARACTER_SOURCE_FIELDS
            }
            if isinstance(character, dict)
            else {field_name: None for field_name in _ZERO_INK_CHARACTER_SOURCE_FIELDS}
            for character in characters
        ],
    }


def _zero_ink_delivery_proof_failures(
    *,
    attempted_representation: str,
    requested_representation: str,
    item_id: str,
    page_number: int,
    source_span_id: int,
    outcome: AttemptOutcome,
    expected_source_manifest=None,
    expected_source_manifest_sha256=None,
    expected_source_manifest_error=None,
) -> list[str]:
    """Reject an entity-less success unless every zero-ink fact is bound."""
    evidence = dict(outcome.evidence or {})
    failures: list[str] = []
    if evidence.get("proof_kind") != "positioned_zero_ink_delivery_v1":
        failures.append("zero_ink_proof_kind_unverified")
    if evidence.get("importer_id") != IMPORTER_ID:
        failures.append("zero_ink_importer_identity_unbound")
    if str(evidence.get("item_id") or "") != str(item_id):
        failures.append("zero_ink_item_identity_unbound")
    evidence_page = _strict_int(evidence.get("page_number"))
    if evidence_page != int(page_number):
        failures.append("zero_ink_page_identity_unbound")
    evidence_span = _strict_int(evidence.get("source_span_id"))
    if evidence_span != int(source_span_id):
        failures.append("zero_ink_source_span_identity_unbound")
    if attempted_representation != requested_representation:
        failures.append("zero_ink_delivery_not_requested_rung")
    if attempted_representation not in {"glyphs", "geometry"}:
        failures.append("zero_ink_delivery_representation_not_convertible")
    if evidence.get("requested_representation") != requested_representation:
        failures.append("zero_ink_requested_representation_unbound")
    if evidence.get("delivered_representation") != attempted_representation:
        failures.append("zero_ink_delivered_representation_unbound")
    expected_logical_id = f"{item_id}:zero-ink:{attempted_representation}"
    if str(evidence.get("logical_delivery_id") or "") != expected_logical_id:
        failures.append("zero_ink_logical_delivery_identity_unbound")
    if evidence.get("zero_ink_delivery") is not True:
        failures.append("zero_ink_delivery_claim_missing")
    if evidence.get("zero_ink_identity_verified") is not True:
        failures.append("zero_ink_identity_unverified")
    if evidence.get("no_visible_ink_expected") is not True:
        failures.append("zero_ink_visible_ink_absence_unverified")
    if outcome.owned_artifacts or outcome.owned_objects or outcome.owned_datablocks:
        failures.append("zero_ink_delivery_retains_owned_artifacts")

    if expected_source_manifest_error:
        failures.append("zero_ink_source_manifest_invalid")
    elif expected_source_manifest is None or not expected_source_manifest_sha256:
        failures.append("zero_ink_source_manifest_missing")
    else:
        failures.extend(
            _zero_ink_manifest_contract_failures(
                expected_source_manifest,
                item_id=item_id,
                page_number=page_number,
                source_span_id=source_span_id,
                requested_representation=requested_representation,
            )
        )
        if evidence.get("source_manifest_schema") != ZERO_INK_SOURCE_MANIFEST_SCHEMA:
            failures.append("zero_ink_source_manifest_schema_unverified")
        if evidence.get("source_manifest_sha256") != expected_source_manifest_sha256:
            failures.append("zero_ink_source_manifest_identity_unbound")
        actual_manifest = _zero_ink_source_manifest_from_evidence(evidence)
        try:
            actual_snapshot, actual_digest = freeze_zero_ink_source_manifest(
                actual_manifest
            )
        except (TypeError, ValueError, OverflowError):
            actual_snapshot, actual_digest = None, None
        if (
            actual_snapshot != expected_source_manifest
            or actual_digest != expected_source_manifest_sha256
        ):
            failures.append("zero_ink_source_manifest_mismatch")

    counts: Dict[str, Any] = {}
    for field_name in (
        "physical_entity_count",
        "source_character_count",
        "character_count",
        "attempted_character_count",
        "visible_character_count",
        "zero_ink_character_count",
    ):
        raw_value = evidence.get(field_name)
        value = _strict_int(raw_value)
        counts[field_name] = value
    if counts["physical_entity_count"] != 0:
        failures.append("zero_ink_physical_entity_count_not_zero")
    if counts["visible_character_count"] != 0:
        failures.append("zero_ink_visible_character_count_not_zero")
    source_count = counts["source_character_count"]
    if source_count is None or source_count <= 0:
        failures.append("zero_ink_source_character_count_invalid")
    elif any(
        counts[field_name] != source_count
        for field_name in (
            "character_count",
            "attempted_character_count",
            "zero_ink_character_count",
        )
    ):
        failures.append("zero_ink_source_character_coverage_mismatch")

    cleanup = evidence.get("cleanup")
    if evidence.get("cleanup_verified") is not True:
        failures.append("zero_ink_cleanup_not_verified")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
        failures.append("zero_ink_cleanup_incomplete")
        top_removed: set[str] = set()
    else:
        removed = cleanup.get("removed")
        top_removed = (
            {str(value) for value in removed}
            if isinstance(removed, (list, tuple))
            else set()
        )

    characters = evidence.get("character_entities")
    if not isinstance(characters, (list, tuple)):
        characters = ()
        failures.append("zero_ink_character_evidence_missing")
    if source_count is not None and len(characters) != source_count:
        failures.append("zero_ink_source_character_coverage_mismatch")
    seen_character_ids: set[str] = set()
    for expected_index, character in enumerate(characters):
        if not isinstance(character, dict):
            failures.append("zero_ink_character_evidence_invalid")
            continue
        expected_character_id = f"{item_id}:char:{expected_index}"
        character_id = str(character.get("character_item_id") or "")
        if character_id != expected_character_id or character_id in seen_character_ids:
            failures.append("zero_ink_character_identity_unbound")
        seen_character_ids.add(character_id)
        character_index = _strict_int(character.get("character_index"))
        if character_index != expected_index:
            failures.append("zero_ink_character_index_unbound")
        if str(character.get("item_id") or "") != str(item_id):
            failures.append("zero_ink_character_parent_identity_unbound")
        character_text = character.get("text")
        if not isinstance(character_text, str) or not character_text:
            failures.append("zero_ink_character_text_missing")
        elif character_text.strip():
            failures.append("zero_ink_character_has_visible_text")
        if character.get("requested_representation") != requested_representation:
            failures.append("zero_ink_character_requested_representation_unbound")
        if character.get("delivered_representation") != attempted_representation:
            failures.append("zero_ink_character_delivered_representation_unbound")
        if character.get("positioned_character") is not True:
            failures.append("zero_ink_character_positioning_unverified")
        character_entity_ids = character.get("entity_ids")
        if not isinstance(character_entity_ids, (list, tuple)) or character_entity_ids:
            failures.append("zero_ink_character_has_physical_entity_identity")
        try:
            advance = float(character.get("advance_width_model"))
        except (TypeError, ValueError):
            advance = math.nan
        if not math.isfinite(advance) or advance <= 0.0:
            failures.append("zero_ink_character_advance_unverified")
        try:
            glyph_height = float(character.get("glyph_height_model"))
        except (TypeError, ValueError):
            glyph_height = math.nan
        if not math.isfinite(glyph_height) or glyph_height <= 0.0:
            failures.append("zero_ink_character_glyph_height_unverified")
        for origin_field in ("source_origin_pdf", "target_origin_model"):
            if not _finite_sequence(character.get(origin_field), 2):
                failures.append(f"zero_ink_character_{origin_field}_unverified")
        if not _finite_sequence(character.get("source_bbox_pdf"), 4):
            failures.append("zero_ink_character_source_bbox_unverified")
        for quad_field in ("source_quad_pdf", "target_quad_model"):
            quad = character.get(quad_field)
            valid_quad = (
                isinstance(quad, (list, tuple))
                and len(quad) == 4
                and all(_finite_sequence(point, 2) for point in quad)
            )
            if not valid_quad:
                failures.append(f"zero_ink_character_{quad_field}_unverified")
        verification = character.get("verification")
        if not isinstance(verification, dict):
            failures.append("zero_ink_character_identity_unverified")
            continue
        if (
            verification.get("zero_ink_identity") is not True
            or verification.get("evaluated_ink_bounds_verified") is not True
            or verification.get("conversion_outcome")
            != "verified_zero_ink_no_physical_entity"
        ):
            failures.append("zero_ink_character_identity_unverified")
        if (
            verification.get("item_id") != str(item_id)
            or _strict_int(verification.get("page_number")) != int(page_number)
            or _strict_int(verification.get("source_span_id"))
            != int(source_span_id)
            or verification.get("character_item_id") != expected_character_id
            or _strict_int(verification.get("character_index")) != expected_index
            or verification.get("source_character_text") != character_text
            or verification.get("source_glyph_id") != character.get("glyph_id")
            or verification.get("requested_representation")
            != requested_representation
        ):
            failures.append(
                "zero_ink_character_verification_item_identity_unbound"
            )
        if (
            verification.get("source_manifest_schema")
            != ZERO_INK_SOURCE_MANIFEST_SCHEMA
            or verification.get("source_manifest_sha256")
            != expected_source_manifest_sha256
            or character.get("source_manifest_sha256")
            != expected_source_manifest_sha256
        ):
            failures.append(
                "zero_ink_character_verification_manifest_identity_unbound"
            )
        character_cleanup = verification.get("cleanup")
        if (
            not isinstance(character_cleanup, dict)
            or character_cleanup.get("status") != "complete"
            or verification.get("zero_ink_source_font_cleaned") is not True
            or verification.get("empty_conversion_datablock_cleaned") is not True
        ):
            failures.append("zero_ink_character_cleanup_incomplete")
        else:
            character_removed = character_cleanup.get("removed")
            if not isinstance(character_removed, (list, tuple)) or not character_removed:
                failures.append("zero_ink_character_cleanup_ledger_missing")
            elif not {
                str(value) for value in character_removed
            }.issubset(top_removed):
                failures.append("zero_ink_cleanup_ledger_incomplete")
    return list(dict.fromkeys(failures))


def zero_ink_delivery_proof_failures(
    *,
    attempted_representation: str,
    requested_representation: str,
    item_id: str,
    page_number: int,
    source_span_id: int,
    outcome: AttemptOutcome,
    expected_zero_ink_manifest=None,
) -> list[str]:
    """Validate a logical zero-ink delivery against independent source truth."""
    snapshot, digest, error = _prepare_zero_ink_source_manifest(
        expected_zero_ink_manifest
    )
    return _zero_ink_delivery_proof_failures(
        attempted_representation=attempted_representation,
        requested_representation=requested_representation,
        item_id=item_id,
        page_number=page_number,
        source_span_id=source_span_id,
        outcome=outcome,
        expected_source_manifest=snapshot,
        expected_source_manifest_sha256=digest,
        expected_source_manifest_error=error,
    )


def deliver_item(
    *,
    item_id: str,
    page_number: int,
    source_span_id: int,
    requested: object,
    expected_zero_ink_manifest=None,
    attempt: Callable[[str], AttemptOutcome],
    cleanup: Callable[[AttemptOutcome], Dict[str, Any]],
) -> tuple[Any, Dict[str, Any]]:
    """Attempt one finite ladder and return the verified entity plus record."""
    requested_mode = normalize_representation(requested)
    expected_manifest, expected_manifest_sha256, expected_manifest_error = (
        _prepare_zero_ink_source_manifest(expected_zero_ink_manifest)
    )
    record: Dict[str, Any] = {
        "item_id": str(item_id),
        "page": int(page_number),
        "source_span_id": int(source_span_id),
        "requested_representation": requested_mode,
        "attempts": [],
        "final_representation": None,
        "status": "failed",
        "fallback_attempted": False,
        "fallback_used": False,
        "entity_ids": [],
    }

    for index, representation in enumerate(fallback_ladder(requested_mode)):
        if index > 0:
            record["fallback_attempted"] = True
        try:
            outcome = attempt(representation)
            if not isinstance(outcome, AttemptOutcome):
                raise TypeError("attempt callback did not return AttemptOutcome")
        except Exception as exc:
            outcome = AttemptOutcome.failed(
                "attempt_exception_not_impossibility_proof",
                evidence={
                    "item_id": str(item_id),
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )

        if outcome.status == "impossible":
            proof_failures = _impossibility_proof_failures(
                attempted_representation=representation,
                item_id=str(item_id),
                page_number=int(page_number),
                source_span_id=int(source_span_id),
                outcome=outcome,
            )
            if proof_failures:
                outcome = AttemptOutcome.failed(
                    "impossibility_evidence_not_affirmative",
                    evidence={
                        **dict(outcome.evidence or {}),
                        "claimed_impossibility_reason": str(outcome.reason or ""),
                        "proof_failures": proof_failures,
                    },
                    owned_artifacts=outcome.owned_artifacts,
                    owned_objects=outcome.owned_objects,
                    owned_datablocks=outcome.owned_datablocks,
                )

        attempt_record: Dict[str, Any] = {
            "attempt_index": index,
            "attempted_representation": representation,
            "status": outcome.status,
            "reason": outcome.reason,
            "evidence": dict(outcome.evidence or {}),
            "entity_ids": [str(value) for value in outcome.entity_ids if str(value)],
            "owned_artifacts": [dict(value) for value in outcome.owned_artifacts],
            "superseded": outcome.status != "delivered",
        }
        if outcome.status == "delivered":
            zero_ink_claimed = (
                outcome.entity is None
                and not attempt_record["entity_ids"]
                and (
                    outcome.evidence.get("zero_ink_delivery") is True
                    or outcome.evidence.get("proof_kind")
                    == "positioned_zero_ink_delivery_v1"
                )
            )
            if zero_ink_claimed:
                proof_failures = _zero_ink_delivery_proof_failures(
                    attempted_representation=representation,
                    requested_representation=requested_mode,
                    item_id=str(item_id),
                    page_number=int(page_number),
                    source_span_id=int(source_span_id),
                    outcome=outcome,
                    expected_source_manifest=expected_manifest,
                    expected_source_manifest_sha256=expected_manifest_sha256,
                    expected_source_manifest_error=expected_manifest_error,
                )
                if proof_failures:
                    outcome = AttemptOutcome.failed(
                        "delivered_attempt_zero_ink_evidence_not_verified",
                        evidence={
                            **dict(outcome.evidence or {}),
                            "proof_failures": proof_failures,
                        },
                        owned_artifacts=outcome.owned_artifacts,
                        owned_objects=outcome.owned_objects,
                        owned_datablocks=outcome.owned_datablocks,
                    )
                    attempt_record.update(
                        status=outcome.status,
                        reason=outcome.reason,
                        evidence=outcome.evidence,
                        entity_ids=[],
                        superseded=True,
                    )
                else:
                    attempt_record["cleanup"] = dict(outcome.evidence["cleanup"])
                    record["attempts"].append(attempt_record)
                    record["final_representation"] = representation
                    record["status"] = "delivered"
                    record["fallback_used"] = False
                    record["entity_ids"] = []
                    record["zero_ink_delivery"] = True
                    record["logical_delivery_id"] = str(
                        outcome.evidence["logical_delivery_id"]
                    )
                    record["physical_entity_count"] = 0
                    record["source_manifest_sha256"] = str(
                        expected_manifest_sha256 or ""
                    )
                    record["_delivered_outcome"] = outcome
                    return None, record
            elif outcome.entity is None or not attempt_record["entity_ids"]:
                outcome = AttemptOutcome.failed(
                    "delivered_attempt_missing_verified_entity_identity",
                    evidence={"item_id": str(item_id)},
                    owned_artifacts=outcome.owned_artifacts,
                    owned_objects=outcome.owned_objects,
                    owned_datablocks=outcome.owned_datablocks,
                )
                attempt_record.update(
                    status=outcome.status,
                    reason=outcome.reason,
                    evidence=outcome.evidence,
                    entity_ids=[],
                    superseded=True,
                )
            else:
                attempt_record["cleanup"] = {"status": "not_required", "removed": []}
                record["attempts"].append(attempt_record)
                record["final_representation"] = representation
                record["status"] = "delivered"
                record["fallback_used"] = index > 0
                record["entity_ids"] = list(attempt_record["entity_ids"])
                record["_delivered_outcome"] = outcome
                return outcome.entity, record

        try:
            cleanup_result = cleanup(outcome)
            if not isinstance(cleanup_result, dict):
                raise TypeError("cleanup callback did not return a dictionary")
        except Exception as exc:
            cleanup_result = {
                "status": "failed",
                "removed": [],
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            }
        attempt_record["cleanup"] = cleanup_result
        record["attempts"].append(attempt_record)
        if cleanup_result.get("status") != "complete":
            break
        if outcome.status != "impossible":
            break

    return None, record


__all__ = [
    "AttemptOutcome",
    "REPRESENTATIONS",
    "ZERO_INK_SOURCE_MANIFEST_SCHEMA",
    "deliver_item",
    "fallback_ladder",
    "freeze_zero_ink_source_manifest",
    "normalize_representation",
    "zero_ink_delivery_proof_failures",
]
