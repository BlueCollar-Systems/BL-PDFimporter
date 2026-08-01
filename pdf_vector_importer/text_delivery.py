"""Finite item-scoped requested-representation delivery controller."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Sequence, Tuple


REPRESENTATIONS = ("labels", "text", "3d_text", "glyphs", "geometry", "raster")
IMPORTER_ID = "bc_pdf_vector_importer.blender"
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
    "embedded_type3_font_program_unavailable": {
        "source_specific_impossibility",
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

def deliver_item(
    *,
    item_id: str,
    page_number: int,
    source_span_id: int,
    requested: object,
    attempt: Callable[[str], AttemptOutcome],
    cleanup: Callable[[AttemptOutcome], Dict[str, Any]],
) -> tuple[Any, Dict[str, Any]]:
    """Attempt one finite ladder and return the verified entity plus record."""
    requested_mode = normalize_representation(requested)
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
            if outcome.entity is None or not attempt_record["entity_ids"]:
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
    "deliver_item",
    "fallback_ladder",
    "normalize_representation",
]
