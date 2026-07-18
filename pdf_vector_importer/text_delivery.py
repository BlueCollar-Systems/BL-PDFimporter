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
ZERO_INK_CHARACTER_MANIFEST_SCHEMA = "positioned_zero_ink_character_manifest_v1"
ZERO_INK_DELIVERY_MANIFEST_SCHEMA = "positioned_zero_ink_delivery_manifest_v1"
# Unicode 17.0 UCD, DerivedCoreProperties.txt, Default_Ignorable_Code_Point:
# https://www.unicode.org/Public/17.0.0/ucd/DerivedCoreProperties.txt
_DEFAULT_IGNORABLE_CODE_POINT_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# Unicode 17.0 UCD, PropList.txt, White_Space:
# https://www.unicode.org/Public/17.0.0/ucd/PropList.txt
_WHITE_SPACE_CODE_POINT_RANGES = (
    (0x0009, 0x000D),
    (0x0020, 0x0020),
    (0x0085, 0x0085),
    (0x00A0, 0x00A0),
    (0x1680, 0x1680),
    (0x2000, 0x200A),
    (0x2028, 0x2029),
    (0x202F, 0x202F),
    (0x205F, 0x205F),
    (0x3000, 0x3000),
)
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
    "intended_affine_matrix",
    "source_ink_classification",
    "source_ink_evidence",
    "source_ink_glyph_id",
    "source_ink_font_sha256",
    "source_ink_glyph_identity",
    "source_ink_code_point",
    "source_ink_mapping_sha256",
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


def _is_default_ignorable_code_point(code_point: int) -> bool:
    """Return Unicode 17.0 Default_Ignorable_Code_Point membership."""
    for lower, upper in _DEFAULT_IGNORABLE_CODE_POINT_RANGES:
        if code_point < lower:
            return False
        if code_point <= upper:
            return True
    return False


def _is_white_space_code_point(code_point: int) -> bool:
    """Return Unicode 17.0 White_Space property membership."""
    for lower, upper in _WHITE_SPACE_CODE_POINT_RANGES:
        if code_point < lower:
            return False
        if code_point <= upper:
            return True
    return False


def classify_text_ink(value: Any) -> str:
    """Conservatively classify text without font or rendered-source evidence.

    Unicode properties are semantic hints, never physical ink authority. Real
    fonts can map any non-empty character, including White_Space and
    Default_Ignorable_Code_Point values, to visible contours. Preserve every
    non-empty value until exact source-font/glyph evidence proves zero ink.
    """
    text = value if isinstance(value, str) else "" if value is None else str(value)
    if not text:
        return "empty"
    return "visible"


def text_has_visible_ink(value: Any) -> bool:
    return classify_text_ink(value) == "visible"


def text_is_unicode_whitespace(value: Any) -> bool:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return bool(text) and all(_is_white_space_code_point(ord(char)) for char in text)


def text_is_unicode_default_ignorable(value: Any) -> bool:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return bool(text) and all(
        _is_default_ignorable_code_point(ord(char)) for char in text
    )


def source_unicode_cmap_binding_sha256(
    font_sha256: Any,
    code_point: Any,
    glyph_id: Any,
):
    """Bind one source Unicode lookup to an exact font digest and physical glyph."""
    code_point = _strict_int(code_point)
    glyph_id = _strict_int(glyph_id)
    if (
        not _is_sha256_digest(font_sha256)
        or code_point is None
        or not 0 <= code_point <= 0x10FFFF
        or glyph_id is None
        or glyph_id < 0
    ):
        return None
    payload = json.dumps(
        ["exact_source_unicode_cmap_v1", font_sha256, code_point, glyph_id],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def source_character_is_zero_ink(character: Any) -> bool:
    """Accept zero ink only when an exact source font and glyph are sealed."""
    if not isinstance(character, dict):
        return False
    classification = character.get("source_ink_classification")
    if classification != "zero_ink":
        return False
    source_glyph_id = _strict_int(character.get("glyph_id"))
    evidence_glyph_id = _strict_int(character.get("source_ink_glyph_id"))
    font_sha256 = character.get("source_ink_font_sha256")
    if not (
        character.get("source_ink_evidence") == "exact_source_glyph_empty"
        and evidence_glyph_id is not None
        and evidence_glyph_id >= 0
        and _is_sha256_digest(font_sha256)
    ):
        return False
    glyph_identity = character.get("source_ink_glyph_identity")
    if glyph_identity == "source_trace_glyph_id":
        return bool(
            source_glyph_id is not None
            and source_glyph_id >= 0
            and evidence_glyph_id == source_glyph_id
            and character.get("source_ink_code_point") is None
            and character.get("source_ink_mapping_sha256") is None
        )
    if glyph_identity != "exact_source_unicode_cmap":
        return False
    text = character.get("text")
    code_point = _strict_int(character.get("source_ink_code_point"))
    mapping_sha256 = character.get("source_ink_mapping_sha256")
    return bool(
        character.get("glyph_id") is None
        and isinstance(text, str)
        and len(text) == 1
        and code_point == ord(text)
        and _is_sha256_digest(mapping_sha256)
        and mapping_sha256
        == source_unicode_cmap_binding_sha256(
            font_sha256,
            code_point,
            evidence_glyph_id,
        )
    )


@dataclass(frozen=True)
class ZeroInkReconciliationAuthority:
    """Immutable-by-value source and delivery truth for post-stack reconciliation."""

    item_id: str
    source_manifest_json: str
    source_manifest_sha256: str
    delivery_manifest_json: str
    delivery_manifest_sha256: str


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
    # Each entry is ``(absolute_file_path, importer_owned_temp_root)``. Cleanup
    # authority is frozen here rather than recovered from mutable host metadata.
    owned_files: Sequence[Tuple[str, str]] = field(default_factory=tuple, repr=False)

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
        owned_files=(),
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
            tuple((str(path), str(root)) for path, root in owned_files),
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
        owned_files=(),
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
            tuple((str(path), str(root)) for path, root in owned_files),
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
        owned_files=(),
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
            tuple((str(path), str(root)) for path, root in owned_files),
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
    evidence_page = _strict_int(evidence.get("page_number"))
    if evidence_page != int(page_number):
        failures.append("page_identity_unbound")
    evidence_span = _strict_int(evidence.get("source_span_id"))
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
        failure_page = _strict_int(evidence.get("font_failure_page_number"))
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

    if reason == "exact_positioned_font_metrics_unavailable_for_item":
        if attempted_representation not in {"text", "3d_text", "glyphs", "geometry"}:
            failures.append("metric_proof_not_valid_for_rung")
        if evidence.get("proof_category") != (
            "authenticated_source_metric_validation_failed"
        ):
            failures.append("metric_proof_category_not_affirmative")
        character_index = _strict_int(evidence.get("character_index"))
        if character_index is None or character_index < 0:
            failures.append("metric_failure_character_unbound")
        if not isinstance(evidence.get("source_character_text"), str) or not str(
            evidence.get("source_character_text")
        ):
            failures.append("metric_failure_text_unbound")
        source_glyph_id = _strict_int(evidence.get("source_glyph_id"))
        if source_glyph_id is None or source_glyph_id < 0:
            failures.append("metric_failure_glyph_unbound")
        glyph_identity = str(evidence.get("source_glyph_identity") or "")
        if glyph_identity not in {
            "source_trace_glyph_id",
            "exact_source_unicode_cmap",
        }:
            failures.append("metric_failure_glyph_identity_unbound")
        if glyph_identity == "exact_source_unicode_cmap":
            code_point = _strict_int(evidence.get("source_ink_code_point"))
            mapping_sha256 = evidence.get("source_ink_mapping_sha256")
            if code_point is None or code_point < 0 or code_point > 0x10FFFF:
                failures.append("metric_failure_cmap_code_point_unbound")
            elif source_glyph_id is not None and _is_sha256_digest(
                evidence.get("source_font_sha256")
            ):
                expected_mapping = source_unicode_cmap_binding_sha256(
                    evidence["source_font_sha256"],
                    code_point,
                    source_glyph_id,
                )
                if mapping_sha256 != expected_mapping:
                    failures.append("metric_failure_cmap_mapping_unbound")
        if not _is_sha256_digest(evidence.get("source_font_sha256")):
            failures.append("metric_failure_font_digest_unbound")
        if not str(evidence.get("font_name") or "").strip():
            failures.append("metric_failure_font_name_unbound")
        if _strict_int(evidence.get("font_asset_page_number")) != int(page_number):
            failures.append("metric_failure_font_page_unbound")
        if str(evidence.get("font_asset_span_font_name") or "").strip() != str(
            evidence.get("font_name") or ""
        ).strip():
            failures.append("metric_failure_span_font_unbound")
        if str(evidence.get("error_type") or "") != "RuntimeError":
            failures.append("metric_failure_error_type_unbound")
        if not str(evidence.get("detail") or "").strip():
            failures.append("metric_failure_detail_unbound")
        return failures

    if reason == "positioned_font_baseline_alignment_unavailable_for_item":
        if attempted_representation not in {"text", "3d_text", "glyphs", "geometry"}:
            failures.append("baseline_proof_not_valid_for_rung")
        if evidence.get("host") != "blender":
            failures.append("baseline_capability_host_unbound")
        if not _host_version_is_bound(evidence.get("host_version")):
            failures.append("baseline_capability_host_version_unbound")
        if evidence.get("capability") != (
            "FONT.align_y.BOTTOM_BASELINE_or_BOTTOM"
        ):
            failures.append("baseline_capability_identity_unbound")
        if evidence.get("capability_present") is not False:
            failures.append("baseline_capability_absence_unproven")
        if evidence.get("supported_baseline_alignments") != []:
            failures.append("baseline_capability_supported_set_unbound")
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


def _is_sha256_digest(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_cleanup_identities(value: Any):
    """Return exact cleanup identities, rejecting blank or non-string entries."""
    if not isinstance(value, (list, tuple)):
        return None
    identities = []
    for identity in value:
        if not isinstance(identity, str) or not identity.strip():
            return None
        identities.append(identity)
    return identities


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


def seal_zero_ink_reconciliation_authority(
    *,
    item_id: str,
    source_manifest: Any,
    source_manifest_sha256: str,
    delivery_manifest_entry: Any,
) -> ZeroInkReconciliationAuthority:
    """Seal canonical manifests as immutable JSON strings before records escape."""
    source_snapshot, source_digest = freeze_zero_ink_source_manifest(source_manifest)
    if source_digest != source_manifest_sha256:
        raise ValueError("zero-ink source manifest digest changed before sealing")
    if not isinstance(delivery_manifest_entry, dict):
        raise TypeError("zero-ink delivery manifest entry is unavailable")
    delivery_snapshot, delivery_digest = freeze_zero_ink_source_manifest(
        delivery_manifest_entry.get("manifest")
    )
    if delivery_digest != delivery_manifest_entry.get("sha256"):
        raise ValueError("zero-ink delivery manifest digest changed before sealing")
    if (
        str(source_snapshot.get("item_id") or "") != str(item_id)
        or str(delivery_snapshot.get("item_id") or "") != str(item_id)
    ):
        raise ValueError("zero-ink reconciliation item identity is unbound")
    source_payload = json.dumps(
        source_snapshot,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    delivery_payload = json.dumps(
        delivery_snapshot,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ZeroInkReconciliationAuthority(
        item_id=str(item_id),
        source_manifest_json=source_payload,
        source_manifest_sha256=source_digest,
        delivery_manifest_json=delivery_payload,
        delivery_manifest_sha256=delivery_digest,
    )


def open_zero_ink_reconciliation_authority(
    authority: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Verify and reopen one immutable reconciliation authority."""
    if not isinstance(authority, ZeroInkReconciliationAuthority):
        raise TypeError("zero-ink reconciliation authority is invalid")
    source_manifest = json.loads(authority.source_manifest_json)
    delivery_manifest = json.loads(authority.delivery_manifest_json)
    source_snapshot, source_digest = freeze_zero_ink_source_manifest(source_manifest)
    delivery_snapshot, delivery_digest = freeze_zero_ink_source_manifest(
        delivery_manifest
    )
    if (
        source_digest != authority.source_manifest_sha256
        or delivery_digest != authority.delivery_manifest_sha256
        or str(source_snapshot.get("item_id") or "") != authority.item_id
        or str(delivery_snapshot.get("item_id") or "") != authority.item_id
    ):
        raise ValueError("zero-ink reconciliation authority digest is invalid")
    return source_snapshot, delivery_snapshot


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
    require_all_zero_ink: bool = True,
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
    if not isinstance(source_text, str) or not source_text:
        failures.append("zero_ink_source_manifest_text_missing")
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
        if not isinstance(character_text, str) or not character_text:
            failures.append("zero_ink_source_manifest_character_text_missing")
        elif require_all_zero_ink and not source_character_is_zero_ink(character):
            failures.append("zero_ink_source_manifest_character_text_not_zero_ink")
        classification = character.get("source_ink_classification")
        if classification is not None and classification not in {"zero_ink", "visible"}:
            failures.append("zero_ink_source_manifest_ink_classification_invalid")
        source_ink_evidence = character.get("source_ink_evidence")
        if classification is not None and (
            not isinstance(source_ink_evidence, str) or not source_ink_evidence
        ):
            failures.append("zero_ink_source_manifest_ink_evidence_missing")
        if classification == "zero_ink" and not source_character_is_zero_ink(
            character
        ):
            failures.append("zero_ink_source_manifest_physical_ink_evidence_unbound")
        glyph_id = character.get("glyph_id")
        if glyph_id is not None and _strict_int(glyph_id) is None:
            failures.append("zero_ink_source_manifest_glyph_identity_invalid")
        for metric_field in ("advance_width_model", "glyph_height_model"):
            try:
                metric = float(character.get(metric_field))
            except (TypeError, ValueError):
                metric = math.nan
            zero_advance_is_valid = (
                metric_field == "advance_width_model"
                and metric == 0.0
            )
            if not math.isfinite(metric) or (
                metric <= 0.0 and not zero_advance_is_valid
            ):
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
        if not _finite_sequence(character.get("intended_affine_matrix"), 16):
            failures.append(
                "zero_ink_source_manifest_intended_affine_matrix_invalid"
            )
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
                if field_name not in {"source_ink_classification", "source_ink_evidence"}
                or field_name in character
            }
            if isinstance(character, dict)
            else {field_name: None for field_name in _ZERO_INK_CHARACTER_SOURCE_FIELDS}
            for character in characters
        ],
    }


def make_zero_ink_character_manifest(
    source_manifest: Dict[str, Any],
    source_manifest_sha256: str,
    character_index: int,
) -> Dict[str, Any]:
    """Derive one canonical logical-character manifest from item source truth."""
    characters = source_manifest.get("characters")
    if not isinstance(characters, list):
        raise ValueError("source manifest characters are unavailable")
    index = _strict_int(character_index)
    if index is None or index < 0 or index >= len(characters):
        raise ValueError("source manifest character index is invalid")
    character = characters[index]
    if not isinstance(character, dict):
        raise ValueError("source manifest character is invalid")
    return {
        "schema": ZERO_INK_CHARACTER_MANIFEST_SCHEMA,
        "importer_id": source_manifest.get("importer_id"),
        "item_id": source_manifest.get("item_id"),
        "page_number": source_manifest.get("page_number"),
        "source_span_id": source_manifest.get("source_span_id"),
        "requested_representation": source_manifest.get(
            "requested_representation"
        ),
        "source_manifest_sha256": str(source_manifest_sha256 or ""),
        "character": character,
    }


def _matrix_mismatch(left: Any, right: Any, tolerance: float) -> bool:
    if not _finite_sequence(left, 16) or not _finite_sequence(right, 16):
        return True
    return any(
        abs(float(actual) - float(expected)) > float(tolerance)
        for actual, expected in zip(left, right)  # noqa: B905
    )


def _zero_ink_character_proof_failures(
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
    """Verify every logical-zero child, including children in mixed batches."""
    evidence = dict(outcome.evidence or {})
    failures: list[str] = []
    native_font_representation = attempted_representation in {"text", "3d_text"}
    converted_representation = attempted_representation in {"glyphs", "geometry"}
    if not native_font_representation and not converted_representation:
        failures.append("zero_ink_character_representation_not_convertible")
    if evidence.get("importer_id") != IMPORTER_ID:
        failures.append("zero_ink_importer_identity_unbound")
    if str(evidence.get("item_id") or "") != str(item_id):
        failures.append("zero_ink_item_identity_unbound")
    if _strict_int(evidence.get("page_number")) != int(page_number):
        failures.append("zero_ink_page_identity_unbound")
    if _strict_int(evidence.get("source_span_id")) != int(source_span_id):
        failures.append("zero_ink_source_span_identity_unbound")
    if evidence.get("requested_representation") != requested_representation:
        failures.append("zero_ink_requested_representation_unbound")
    if evidence.get("delivered_representation") != attempted_representation:
        failures.append("zero_ink_delivered_representation_unbound")

    if expected_source_manifest_error:
        failures.append("zero_ink_source_manifest_invalid")
        expected_characters = []
    elif expected_source_manifest is None or not expected_source_manifest_sha256:
        failures.append("zero_ink_source_manifest_missing")
        expected_characters = []
    else:
        failures.extend(
            _zero_ink_manifest_contract_failures(
                expected_source_manifest,
                item_id=item_id,
                page_number=page_number,
                source_span_id=source_span_id,
                requested_representation=requested_representation,
                require_all_zero_ink=False,
            )
        )
        expected_characters = expected_source_manifest.get("characters")
        if not isinstance(expected_characters, list):
            expected_characters = []
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

    characters = evidence.get("character_entities")
    if not isinstance(characters, (list, tuple)):
        characters = ()
        failures.append("zero_ink_character_evidence_missing")
    expected_count = len(expected_characters)
    if expected_count and len(characters) != expected_count:
        failures.append("zero_ink_source_character_coverage_mismatch")
    for field_name in ("source_character_count", "character_count", "attempted_character_count"):
        if _strict_int(evidence.get(field_name)) != expected_count:
            failures.append("zero_ink_source_character_coverage_mismatch")
            break

    zero_indices = [
        index
        for index, character in enumerate(expected_characters)
        if isinstance(character, dict)
        and isinstance(character.get("text"), str)
        and source_character_is_zero_ink(character)
    ]
    if not zero_indices:
        failures.append("zero_ink_character_source_identity_missing")
    if _strict_int(evidence.get("zero_ink_character_count")) != len(zero_indices):
        failures.append("zero_ink_character_count_mismatch")
    expected_visible_count = expected_count - len(zero_indices)
    if _strict_int(evidence.get("visible_character_count")) != expected_visible_count:
        failures.append("zero_ink_visible_character_count_mismatch")
    physical_count = _strict_int(evidence.get("physical_entity_count"))
    raw_runtime_entity_ids = tuple(outcome.entity_ids or ())
    runtime_entity_ids = [
        value
        for value in raw_runtime_entity_ids
        if isinstance(value, str) and value
    ]
    if len(runtime_entity_ids) != len(raw_runtime_entity_ids):
        failures.append("zero_ink_character_entity_identity_mismatch")
    if physical_count is None or physical_count != len(runtime_entity_ids):
        failures.append("zero_ink_physical_entity_count_mismatch")

    verified_entity_ids: list[str] = []
    for expected_index in range(expected_count):
        if expected_index >= len(characters):
            continue
        if expected_index in zero_indices and not native_font_representation:
            continue
        character = characters[expected_index]
        character_entity_ids = (
            character.get("entity_ids") if isinstance(character, dict) else None
        )
        if not isinstance(character_entity_ids, (list, tuple)):
            failures.append("zero_ink_character_entity_identity_mismatch")
            continue
        normalized_ids = [
            value
            for value in character_entity_ids
            if isinstance(value, str) and value
        ]
        if not normalized_ids or len(normalized_ids) != len(character_entity_ids):
            failures.append("zero_ink_character_entity_identity_mismatch")
        verified_entity_ids.extend(normalized_ids)
    if (
        len(set(verified_entity_ids)) != len(verified_entity_ids)
        or runtime_entity_ids != verified_entity_ids
    ):
        failures.append("zero_ink_character_entity_identity_mismatch")

    cleanup = evidence.get("cleanup")
    if native_font_representation:
        top_removed_values: list[str] = []
    else:
        if evidence.get("cleanup_verified") is not True:
            failures.append("zero_ink_cleanup_not_verified")
        if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
            failures.append("zero_ink_cleanup_incomplete")
            top_removed_values = []
        else:
            top_removed_values = _strict_cleanup_identities(cleanup.get("removed"))
            if top_removed_values is None:
                top_removed_values = []
                failures.append("zero_ink_cleanup_ledger_incomplete")
    child_removed: set[str] = set()

    for expected_index in zero_indices:
        if expected_index >= len(characters):
            failures.append("zero_ink_character_evidence_missing")
            continue
        character = characters[expected_index]
        if not isinstance(character, dict):
            failures.append("zero_ink_character_evidence_invalid")
            continue
        expected_character = expected_characters[expected_index]
        expected_character_id = f"{item_id}:char:{expected_index}"
        if (
            str(character.get("character_item_id") or "")
            != expected_character_id
            or _strict_int(character.get("character_index")) != expected_index
        ):
            failures.append("zero_ink_character_identity_unbound")
        if str(character.get("item_id") or "") != str(item_id):
            failures.append("zero_ink_character_parent_identity_unbound")
        character_text = character.get("text")
        if (
            not isinstance(character_text, str)
            or not character_text
            or not source_character_is_zero_ink(character)
        ):
            failures.append("zero_ink_character_has_visible_text")
        if character.get("requested_representation") != requested_representation:
            failures.append("zero_ink_character_requested_representation_unbound")
        if character.get("delivered_representation") != attempted_representation:
            failures.append("zero_ink_character_delivered_representation_unbound")
        if character.get("positioned_character") is not True:
            failures.append("zero_ink_character_positioning_unverified")
        character_entity_ids = character.get("entity_ids")
        if native_font_representation:
            if (
                not isinstance(character_entity_ids, (list, tuple))
                or not character_entity_ids
                or any(
                    not isinstance(value, str) or not value
                    for value in character_entity_ids
                )
            ):
                failures.append("zero_ink_character_entity_identity_mismatch")
        elif not isinstance(character_entity_ids, (list, tuple)) or character_entity_ids:
            failures.append("zero_ink_character_has_physical_entity_identity")

        canonical_matrix = expected_character.get("intended_affine_matrix")
        verification = character.get("verification")
        if not isinstance(verification, dict):
            failures.append("zero_ink_character_identity_unverified")
            continue
        try:
            expected_advance = float(expected_character.get("advance_width_model"))
        except (TypeError, ValueError):
            expected_advance = math.nan
        if (
            expected_advance == 0.0
            and verification.get("zero_advance_logical_proof") is not True
        ):
            failures.append("zero_advance_logical_proof_unverified")
        intended_matrix = verification.get("intended_affine_matrix")
        source_affine_matrix = verification.get(
            "source_affine_matrix",
            intended_matrix,
        )
        evaluated_matrix = verification.get("evaluated_affine_matrix")
        if not _finite_sequence(intended_matrix, 16):
            failures.append("zero_ink_character_intended_affine_matrix_unverified")
        elif _matrix_mismatch(source_affine_matrix, canonical_matrix, 1e-12):
            failures.append("zero_ink_character_intended_affine_matrix_mismatch")
        if not _finite_sequence(evaluated_matrix, 16):
            failures.append("zero_ink_character_evaluated_affine_matrix_unverified")
        elif _finite_sequence(intended_matrix, 16) and _matrix_mismatch(
            evaluated_matrix, intended_matrix, 1e-6
        ):
            failures.append("zero_ink_character_evaluated_affine_matrix_mismatch")
        identity_verified = bool(
            verification.get("zero_ink_identity") is True
            and verification.get("evaluated_ink_bounds_verified") is True
        )
        if native_font_representation:
            identity_verified = bool(
                identity_verified
                and verification.get("native_zero_ink_entity_verified") is True
                and verification.get("actual_object_type") == "FONT"
                and verification.get("body_verified") is True
            )
        else:
            identity_verified = bool(
                identity_verified
                and verification.get("conversion_outcome")
                == "verified_zero_ink_no_physical_entity"
            )
        if not identity_verified:
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

        actual_character_manifest = character.get("zero_ink_character_manifest")
        if not isinstance(actual_character_manifest, dict):
            failures.append("zero_ink_character_manifest_missing")
        else:
            try:
                expected_character_manifest = make_zero_ink_character_manifest(
                    expected_source_manifest,
                    expected_source_manifest_sha256,
                    expected_index,
                )
                expected_character_snapshot, expected_character_digest = (
                    freeze_zero_ink_source_manifest(expected_character_manifest)
                )
                actual_character_snapshot, actual_character_digest = (
                    freeze_zero_ink_source_manifest(actual_character_manifest)
                )
            except (TypeError, ValueError, OverflowError):
                expected_character_snapshot = expected_character_digest = None
                actual_character_snapshot = actual_character_digest = None
            if (
                actual_character_snapshot != expected_character_snapshot
                or actual_character_digest != expected_character_digest
            ):
                failures.append("zero_ink_character_manifest_mismatch")
            if (
                character.get("zero_ink_character_manifest_sha256")
                != expected_character_digest
                or verification.get("zero_ink_character_manifest_schema")
                != ZERO_INK_CHARACTER_MANIFEST_SCHEMA
                or verification.get("zero_ink_character_manifest_sha256")
                != expected_character_digest
            ):
                failures.append("zero_ink_character_manifest_identity_unbound")

        if not native_font_representation:
            character_cleanup = verification.get("cleanup")
            if (
                not isinstance(character_cleanup, dict)
                or character_cleanup.get("status") != "complete"
                or verification.get("zero_ink_source_font_cleaned") is not True
                or verification.get("empty_conversion_datablock_cleaned") is not True
            ):
                failures.append("zero_ink_character_cleanup_incomplete")
            elif not _strict_cleanup_identities(character_cleanup.get("removed")):
                failures.append("zero_ink_character_cleanup_ledger_missing")
            else:
                child_removed.update(
                    _strict_cleanup_identities(character_cleanup.get("removed")) or ()
                )
    if not native_font_representation and (
        len(set(top_removed_values)) != len(top_removed_values)
        or set(top_removed_values) != child_removed
    ):
        failures.append("zero_ink_cleanup_ledger_incomplete")
    return list(dict.fromkeys(failures))


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
        removed = _strict_cleanup_identities(cleanup.get("removed"))
        top_removed = set(removed or ())
        if removed is None:
            failures.append("zero_ink_cleanup_ledger_incomplete")

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
        elif not source_character_is_zero_ink(character):
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
        if not math.isfinite(advance) or advance < 0.0:
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
        if advance == 0.0 and verification.get("zero_advance_logical_proof") is not True:
            failures.append("zero_advance_logical_proof_unverified")
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
            character_removed = _strict_cleanup_identities(
                character_cleanup.get("removed")
            )
            if not character_removed:
                failures.append("zero_ink_character_cleanup_ledger_missing")
            elif not set(character_removed).issubset(top_removed):
                failures.append("zero_ink_cleanup_ledger_incomplete")
    failures.extend(
        _zero_ink_character_proof_failures(
            attempted_representation=attempted_representation,
            requested_representation=requested_representation,
            item_id=item_id,
            page_number=page_number,
            source_span_id=source_span_id,
            outcome=outcome,
            expected_source_manifest=expected_source_manifest,
            expected_source_manifest_sha256=expected_source_manifest_sha256,
            expected_source_manifest_error=expected_source_manifest_error,
        )
    )
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


def zero_ink_character_proof_failures(
    *,
    attempted_representation: str,
    requested_representation: str,
    item_id: str,
    page_number: int,
    source_span_id: int,
    outcome: AttemptOutcome,
    expected_zero_ink_manifest=None,
) -> list[str]:
    """Validate nested logical-zero children in a physical or logical batch."""
    snapshot, digest, error = _prepare_zero_ink_source_manifest(
        expected_zero_ink_manifest
    )
    return _zero_ink_character_proof_failures(
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


def _zero_ink_delivery_manifest(
    *,
    item_id: str,
    page_number: int,
    source_span_id: int,
    requested_representation: str,
    delivered_representation: str,
    entity_ids: Sequence[str],
    source_manifest: Dict[str, Any],
    source_manifest_sha256: str,
    logical_zero_ink_delivery: bool,
) -> Dict[str, Any]:
    characters = source_manifest.get("characters")
    if not isinstance(characters, list):
        raise ValueError("zero-ink source characters are unavailable")
    zero_count = sum(
        1
        for character in characters
        if isinstance(character, dict)
        and isinstance(character.get("text"), str)
        and source_character_is_zero_ink(character)
    )
    normalized_ids = [str(value) for value in entity_ids if str(value)]
    contribution = 0 if logical_zero_ink_delivery else 1
    manifest = {
        "schema": ZERO_INK_DELIVERY_MANIFEST_SCHEMA,
        "importer_id": IMPORTER_ID,
        "item_id": str(item_id),
        "page_number": int(page_number),
        "source_span_id": int(source_span_id),
        "requested_representation": str(requested_representation),
        "delivered_representation": str(delivered_representation),
        "entity_ids": normalized_ids,
        "physical_entity_count": len(normalized_ids),
        "source_character_count": len(characters),
        "zero_ink_character_count": zero_count,
        "logical_zero_ink_delivery": bool(logical_zero_ink_delivery),
        "delivered_count_contribution": contribution,
        "source_manifest_sha256": str(source_manifest_sha256 or ""),
    }
    snapshot, digest = freeze_zero_ink_source_manifest(manifest)
    return {"manifest": snapshot, "sha256": digest}


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
        "delivered_count_contribution": 0,
    }

    for index, representation in enumerate(fallback_ladder(requested_mode)):
        if index > 0:
            record["fallback_attempted"] = True
        try:
            outcome = attempt(representation)
            if not isinstance(outcome, AttemptOutcome):
                raise TypeError("attempt callback did not return AttemptOutcome")
        except Exception as exc:
            def exception_owned(error, name):
                try:
                    return tuple(getattr(error, name, ()) or ())
                except Exception:
                    return ()

            outcome = AttemptOutcome.failed(
                "attempt_exception_not_impossibility_proof",
                evidence={
                    "item_id": str(item_id),
                    "exception_type": type(exc).__name__,
                    "detail": str(exc),
                },
                owned_artifacts=exception_owned(exc, "owned_artifacts"),
                owned_objects=exception_owned(exc, "owned_objects"),
                owned_datablocks=exception_owned(exc, "owned_datablocks"),
                owned_files=exception_owned(exc, "owned_files"),
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
                    owned_files=outcome.owned_files,
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
            manifest_characters = (
                expected_manifest.get("characters")
                if isinstance(expected_manifest, dict)
                else None
            )
            expected_zero_child = bool(
                representation in {"text", "3d_text", "glyphs", "geometry"}
                and isinstance(manifest_characters, list)
                and any(
                    isinstance(character, dict)
                    and isinstance(character.get("text"), str)
                    and source_character_is_zero_ink(character)
                    for character in manifest_characters
                )
            )
            evidence_zero_count = _strict_int(
                outcome.evidence.get("zero_ink_character_count")
            )
            evidence_characters = outcome.evidence.get("character_entities")
            evidence_zero_child = bool(
                isinstance(evidence_characters, (list, tuple))
                and any(
                    isinstance(character, dict)
                    and (
                        isinstance(character.get("verification"), dict)
                        and character["verification"].get("zero_ink_identity")
                        is True
                        or (
                            isinstance(character.get("text"), str)
                            and source_character_is_zero_ink(character)
                        )
                    )
                    for character in evidence_characters
                )
            )
            nested_zero_ink_claimed = bool(
                representation in {"text", "3d_text", "glyphs", "geometry"}
                and (
                    expected_zero_child
                    or evidence_zero_child
                    or (evidence_zero_count is not None and evidence_zero_count > 0)
                )
            )
            proof_failures: list[str] = []
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
            elif nested_zero_ink_claimed:
                proof_failures = _zero_ink_character_proof_failures(
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
                    owned_files=outcome.owned_files,
                )
                attempt_record.update(
                    status=outcome.status,
                    reason=outcome.reason,
                    evidence=outcome.evidence,
                    entity_ids=[],
                    superseded=True,
                )
            elif zero_ink_claimed:
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
                record["zero_ink_character_count"] = int(
                    outcome.evidence["zero_ink_character_count"]
                )
                record["source_manifest_sha256"] = str(
                    expected_manifest_sha256 or ""
                )
                record["delivered_count_contribution"] = 0
                delivery_manifest = _zero_ink_delivery_manifest(
                    item_id=str(item_id),
                    page_number=int(page_number),
                    source_span_id=int(source_span_id),
                    requested_representation=requested_mode,
                    delivered_representation=representation,
                    entity_ids=(),
                    source_manifest=expected_manifest,
                    source_manifest_sha256=str(expected_manifest_sha256 or ""),
                    logical_zero_ink_delivery=True,
                )
                record["zero_ink_delivery_manifest_sha256"] = delivery_manifest[
                    "sha256"
                ]
                record["_zero_ink_delivery_manifest"] = delivery_manifest
                record["_zero_ink_reconciliation_authority"] = (
                    seal_zero_ink_reconciliation_authority(
                        item_id=str(item_id),
                        source_manifest=expected_manifest,
                        source_manifest_sha256=str(expected_manifest_sha256 or ""),
                        delivery_manifest_entry=delivery_manifest,
                    )
                )
                record["_delivered_outcome"] = outcome
                return None, record
            elif outcome.status == "delivered" and (
                outcome.entity is None or not attempt_record["entity_ids"]
            ):
                outcome = AttemptOutcome.failed(
                    "delivered_attempt_missing_verified_entity_identity",
                    evidence={"item_id": str(item_id)},
                    owned_artifacts=outcome.owned_artifacts,
                    owned_objects=outcome.owned_objects,
                    owned_datablocks=outcome.owned_datablocks,
                    owned_files=outcome.owned_files,
                )
                attempt_record.update(
                    status=outcome.status,
                    reason=outcome.reason,
                    evidence=outcome.evidence,
                    entity_ids=[],
                    superseded=True,
                )
            elif outcome.status == "delivered":
                attempt_record["cleanup"] = {"status": "not_required", "removed": []}
                record["attempts"].append(attempt_record)
                record["final_representation"] = representation
                record["status"] = "delivered"
                record["fallback_used"] = index > 0
                record["entity_ids"] = list(attempt_record["entity_ids"])
                record["physical_entity_count"] = len(attempt_record["entity_ids"])
                if nested_zero_ink_claimed:
                    record["zero_ink_character_count"] = int(
                        outcome.evidence["zero_ink_character_count"]
                    )
                    record["source_manifest_sha256"] = str(
                        expected_manifest_sha256 or ""
                    )
                record["delivered_count_contribution"] = 1
                if nested_zero_ink_claimed:
                    delivery_manifest = _zero_ink_delivery_manifest(
                        item_id=str(item_id),
                        page_number=int(page_number),
                        source_span_id=int(source_span_id),
                        requested_representation=requested_mode,
                        delivered_representation=representation,
                        entity_ids=attempt_record["entity_ids"],
                        source_manifest=expected_manifest,
                        source_manifest_sha256=str(expected_manifest_sha256 or ""),
                        logical_zero_ink_delivery=False,
                    )
                    record["zero_ink_delivery_manifest_sha256"] = delivery_manifest[
                        "sha256"
                    ]
                    record["_zero_ink_delivery_manifest"] = delivery_manifest
                    record["_zero_ink_reconciliation_authority"] = (
                        seal_zero_ink_reconciliation_authority(
                            item_id=str(item_id),
                            source_manifest=expected_manifest,
                            source_manifest_sha256=str(
                                expected_manifest_sha256 or ""
                            ),
                            delivery_manifest_entry=delivery_manifest,
                        )
                    )
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
            # This private runtime handoff is removed before report
            # serialization.  Names in ``owned_artifacts`` are evidence only;
            # remediation must retain the exact frozen references and files.
            record["_cleanup_failure_outcome"] = outcome
            break
        if outcome.status != "impossible":
            break

    return None, record


__all__ = [
    "AttemptOutcome",
    "REPRESENTATIONS",
    "ZERO_INK_CHARACTER_MANIFEST_SCHEMA",
    "ZERO_INK_DELIVERY_MANIFEST_SCHEMA",
    "ZERO_INK_SOURCE_MANIFEST_SCHEMA",
    "classify_text_ink",
    "deliver_item",
    "fallback_ladder",
    "freeze_zero_ink_source_manifest",
    "make_zero_ink_character_manifest",
    "normalize_representation",
    "source_character_is_zero_ink",
    "source_unicode_cmap_binding_sha256",
    "text_has_visible_ink",
    "text_is_unicode_whitespace",
    "text_is_unicode_default_ignorable",
    "zero_ink_character_proof_failures",
    "zero_ink_delivery_proof_failures",
]
