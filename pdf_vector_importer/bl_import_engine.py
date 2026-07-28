# -*- coding: utf-8 -*-
# bl_import_engine.py — Main import orchestrator for Blender
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT
"""
Top-level import pipeline that ties together pdfcadcore extraction,
optional recognition, and Blender geometry/text building.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import bpy

from .dependency_manager import check_pymupdf, ensure_lib_path
from .packed_assets import PackedAssetError, pack_and_verify_bytes, verify_packed_sha256
from .pdfcadcore import (
    ImportConfig, extract_page, iter_pages, recognition, reset_ids,
    classify_page_content, tag_hatch_primitives, cleanup_primitives,
)
from .bl_geometry_builder import build_page
from .bl_text_builder import (
    FinalEntityExpectationAuthority,
    build_all_text,
    cleanup_delivery_outcome,
    final_entity_expectations_from_evidence,
    raster_mesh_fidelity_state,
)
from .pdfcadcore.primitive_extractor import (
    _page_rotation_transform,
    _transform_pdf_point,
)
from .pdfcadcore.text_delivery_report import build_text_representation_delivery
from .text_delivery import (
    AttemptOutcome,
    REPRESENTATIONS,
    ZERO_INK_DELIVERY_MANIFEST_SCHEMA,
    ZeroInkReconciliationAuthority,
    _impossibility_proof_failures,
    fallback_ladder,
    freeze_zero_ink_source_manifest,
    normalize_representation,
    open_zero_ink_reconciliation_authority,
    source_character_is_zero_ink,
    zero_ink_character_proof_failures,
    zero_ink_delivery_proof_failures,
)

_MM_PER_PT = 25.4 / 72.0
_MM_TO_M = 0.001
_AUTO_RECOGNITION_PRIMITIVE_LIMIT = 20_000
_AUTO_RECOGNITION_TEXT_LIMIT = 3_000
_AUTO_RECOGNITION_PAGE_AREA_MM2_LIMIT = 12_000_000.0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "off", "no"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _skip_semantic_recognition_for_speed(page_data) -> Optional[str]:
    """Return a reason when generic recognition should be skipped for a heavy page."""
    if _env_bool("BC_BL_FORCE_AUTO_RECOGNITION", False):
        return None

    primitive_limit = max(1, _env_int(
        "BC_BL_AUTO_RECOGNITION_MAX_PRIMITIVES",
        _AUTO_RECOGNITION_PRIMITIVE_LIMIT,
    ))
    text_limit = max(1, _env_int(
        "BC_BL_AUTO_RECOGNITION_MAX_TEXT",
        _AUTO_RECOGNITION_TEXT_LIMIT,
    ))
    area_limit = max(1.0, _env_float(
        "BC_BL_AUTO_RECOGNITION_MAX_AREA_MM2",
        _AUTO_RECOGNITION_PAGE_AREA_MM2_LIMIT,
    ))

    primitive_count = len(getattr(page_data, "primitives", []) or [])
    text_count = len(getattr(page_data, "text_items", []) or [])
    try:
        page_area = float(getattr(page_data, "width", 0.0) or 0.0) * float(getattr(page_data, "height", 0.0) or 0.0)
    except (TypeError, ValueError):
        page_area = 0.0

    if primitive_count > primitive_limit:
        return f"{primitive_count} primitives > {primitive_limit}"
    if text_count > text_limit:
        return f"{text_count} text spans > {text_limit}"
    if page_area > area_limit and (primitive_count + text_count) > (primitive_limit // 2):
        return f"{page_area:.0f} mm^2 page with {primitive_count + text_count} entities"
    return None


def _default_import_report_path(filepath: str) -> str:
    base = os.path.splitext(os.path.basename(filepath))[0]
    return os.path.join(tempfile.gettempdir(), f"{base}_import_report.json")


def _importer_version() -> str:
    try:
        from . import bl_info

        version = bl_info.get("version", "")
        if isinstance(version, (tuple, list)):
            return ".".join(str(part) for part in version)
        return str(version or "")
    except (ImportError, AttributeError, TypeError):
        return ""


def _blender_host_version() -> str:
    try:
        version = getattr(getattr(bpy, "app", None), "version", "")
        if isinstance(version, (tuple, list)):
            return ".".join(str(part) for part in version)
        return str(version or "")
    except (AttributeError, TypeError):
        return ""


def _pymupdf_version() -> str:
    try:
        from .pdfcadcore.fitz_loader import import_fitz
        from .dependency_manager import get_lib_dir

        fitz = import_fitz(prefer_lib_dir=str(get_lib_dir()))
        return str(getattr(fitz, "__version__", "") or "")
    except (ImportError, RuntimeError, OSError):
        return ""


def _merge_scale_into_stats(stats: Dict, page_data) -> None:
    """Accumulate resolved_scale telemetry for import_report cross-check."""

    rs = getattr(page_data, "resolved_scale", None)
    if not rs:
        return
    try:
        confidence = float(getattr(rs, "confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    factor = getattr(rs, "factor", None)
    if confidence <= 0 and not factor:
        return

    payload = {
        "factor": factor,
        "notation": getattr(rs, "notation", None),
        "source": getattr(rs, "source", None),
        "confidence": confidence,
        "fallback_reason": getattr(rs, "fallback_reason", None),
    }
    current = stats.get("resolved_scale")
    if not current or confidence > float(current.get("confidence", 0) or 0):
        stats["resolved_scale"] = payload

    hints = stats.setdefault(
        "scale_hints",
        {
            "title_block_detected": False,
            "dimension_count": 0,
            "alternate_scale_factors": [],
        },
    )
    if factor and confidence > 0:
        try:
            alt = float(factor)
            alts = list(hints.get("alternate_scale_factors") or [])
            if alt not in alts:
                alts.append(alt)
                hints["alternate_scale_factors"] = sorted(alts)
        except (TypeError, ValueError):
            pass
    if str(getattr(rs, "source", "") or "") == "titleblock":
        hints["title_block_detected"] = True


def _text_fallback_from_provenance(provenance_opts: Any) -> Optional[Dict[str, Any]]:
    """Return an aggregate fallback while preserving item detail separately."""
    if provenance_opts is None:
        return None
    delivery = _text_delivery_from_provenance(provenance_opts)
    groups: Dict[tuple[str, str, str], int] = {}
    for record in delivery["items"]:
        if record.get("status") != "delivered" or not record.get("fallback_used"):
            continue
        requested = str(record.get("requested_representation") or "").strip().lower()
        delivered = str(record.get("final_representation") or "").strip().lower()
        if not requested or not delivered or requested == delivered:
            continue
        reason = "item_specific_impossibility"
        for attempt in list(record.get("attempts") or []):
            if not isinstance(attempt, dict):
                continue
            if attempt.get("status") == "impossible":
                reason = str(attempt.get("reason") or reason)
                break
        key = (requested, delivered, reason)
        groups[key] = int(groups.get(key, 0) or 0) + 1
    if groups:
        requested_values = sorted({key[0] for key in groups})
        delivered_values = sorted({key[1] for key in groups})
        reason_values = sorted({key[2] for key in groups})
        return {
            "requested": requested_values[0] if len(requested_values) == 1 else "mixed_requested",
            "delivered": delivered_values[0] if len(delivered_values) == 1 else "mixed_item_specific",
            "reason": (
                reason_values[0]
                if len(reason_values) == 1
                else (
                    "multiple_item_specific_reasons; see "
                    "extra.text_delivery_attempts"
                )
            ),
            "count": sum(groups.values()),
        }
    if delivery["items"]:
        return None
    try:
        records = list(getattr(provenance_opts, "_text_mode_fallbacks", []) or [])
    except (AttributeError, TypeError):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        requested = str(record.get("requested") or "").strip().lower()
        delivered = str(record.get("delivered") or "").strip().lower()
        reason = str(record.get("reason") or "").strip()
        if not requested or not delivered or requested == delivered:
            continue
        try:
            count = max(0, int(record.get("count", 0) or 0))
        except (TypeError, ValueError):
            count = 0
        return {
            "requested": requested,
            "delivered": delivered,
            "reason": reason or "unspecified",
            "count": count,
        }
    return None


def _text_delivery_from_provenance(provenance_opts: Any) -> Dict[str, Any]:
    """Build the complete, item-scoped text delivery report payload."""
    try:
        raw_records = list(getattr(provenance_opts, "_text_delivery_records", []) or [])
    except (AttributeError, TypeError):
        raw_records = []
    records = [dict(record) for record in raw_records if isinstance(record, dict)]
    requested_counts: Dict[str, int] = {}
    final_counts: Dict[str, int] = {}
    delivered = 0
    fallback = 0
    failed_ids = []
    for record in records:
        requested = str(record.get("requested_representation") or "").strip().lower()
        final = str(record.get("final_representation") or "").strip().lower()
        status = str(record.get("status") or "failed").strip().lower()
        if requested:
            requested_counts[requested] = int(requested_counts.get(requested, 0) or 0) + 1
        if final:
            final_counts[final] = int(final_counts.get(final, 0) or 0) + 1
        if status == "delivered" and final:
            delivered += 1
            if bool(record.get("fallback_used")) and final != requested:
                fallback += 1
        else:
            failed_ids.append(str(record.get("item_id") or "<unknown>"))
    return {
        "schema": "bcs.text_delivery/1.0",
        "summary": {
            "source_items": len(records),
            "delivered_items": delivered,
            "fallback_items": fallback,
            "failed_items": len(failed_ids),
            "requested_counts": dict(sorted(requested_counts.items())),
            "final_counts": dict(sorted(final_counts.items())),
            "failed_item_ids": failed_ids,
        },
        "items": records,
    }


def _report_text_type(value: Any) -> str:
    """Normalize one Blender representation name for the shared report ledger."""

    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {"text3d": "3d_text", "3dtext": "3d_text"}.get(raw, raw)


def _report_exact_text_type(value: Any) -> str:
    """Accept only canonical representation evidence emitted by the producer."""

    return value if isinstance(value, str) and value in REPRESENTATIONS else ""


def _report_string_ids(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return [""]
    return [item if isinstance(item, str) else "" for item in value]


def _report_host_identity(namespace: str, raw_identity: Any) -> str:
    """Qualify a valid Blender identity without repairing malformed evidence."""

    if not isinstance(raw_identity, str):
        return ""
    if not raw_identity or raw_identity != raw_identity.strip():
        return raw_identity
    return f"blender:{namespace}:{raw_identity}"


def _report_merge_identity_evidence(*sources: List[str]) -> List[str]:
    """Coalesce cross-channel identity overlap without hiding source duplicates."""

    merged: List[str] = []
    for source in sources:
        source_seen = set()
        for identity in source:
            if identity in source_seen or identity not in merged:
                merged.append(identity)
            source_seen.add(identity)
    return merged


def _report_object_identity_pairs(
    value: Any,
    *,
    namespace_prefix: str = "",
) -> List[tuple[str, str]]:
    return [
        (
            raw_identity,
            _report_host_identity(f"{namespace_prefix}object", raw_identity),
        )
        for raw_identity in _report_string_ids(value)
    ]


def _report_owned_artifact_identity_pairs(
    value: Any,
    *,
    namespace_prefix: str = "",
) -> List[tuple[str, str]]:
    """Return raw-to-canonical mappings for attempt-owned Blender artifacts."""

    if not isinstance(value, (list, tuple)):
        return [("", "")]
    identities: List[tuple[str, str]] = []
    for artifact in value:
        if not isinstance(artifact, dict) or artifact.get("ownership") != (
            "created_by_this_item_attempt"
        ):
            identities.append(("", ""))
            continue
        artifact_ids: List[tuple[str, str]] = []
        for field, namespace in (
            ("object_id", "object"),
            ("material_id", "material"),
            ("image_id", "image"),
            ("file_path", "file"),
        ):
            raw_identity = artifact.get(field)
            if raw_identity in (None, ""):
                continue
            artifact_ids.append(
                (
                    raw_identity if isinstance(raw_identity, str) else "",
                    _report_host_identity(
                        f"{namespace_prefix}{namespace}",
                        raw_identity,
                    ),
                )
            )
        raw_datablock_id = artifact.get("datablock_id")
        if raw_datablock_id not in (None, ""):
            raw_kind = artifact.get("datablock_kind")
            datablock_kind = (
                raw_kind.lower()
                if isinstance(raw_kind, str)
                and raw_kind
                and raw_kind == raw_kind.strip()
                and raw_kind.upper()
                in {"CURVE", "MESH", "MATERIAL", "IMAGE", "FONT"}
                else ""
            )
            artifact_ids.append(
                (
                    raw_datablock_id
                    if isinstance(raw_datablock_id, str)
                    else "",
                    _report_host_identity(
                        f"{namespace_prefix}datablock:{datablock_kind}",
                        raw_datablock_id,
                    )
                    if datablock_kind
                    else "",
                )
            )
        identities.extend(artifact_ids or [("", "")])
    return identities


def _report_removed_entity_ids(
    cleanup: Any,
    ownership_pairs: List[tuple[str, str]],
    *,
    namespace_prefix: str = "",
) -> List[str]:
    if not isinstance(cleanup, dict):
        return []
    available: Dict[str, List[str]] = {}
    for raw_identity, canonical_identity in ownership_pairs:
        choices = available.setdefault(raw_identity, [])
        if canonical_identity not in choices:
            choices.append(canonical_identity)
    removed: List[str] = []
    for raw_removed in _report_string_ids(cleanup.get("removed")):
        matches = available.get(raw_removed) or []
        removed.append(
            matches.pop(0)
            if matches
            else _report_host_identity(
                f"{namespace_prefix}unbound_removed",
                raw_removed,
            )
        )
    return removed


def _report_finite_coordinates(value: Any, length: int) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    coordinates: List[float] = []
    for raw_coordinate in value:
        if not isinstance(raw_coordinate, (int, float)) or isinstance(
            raw_coordinate,
            bool,
        ):
            return None
        coordinate = float(raw_coordinate)
        if not math.isfinite(coordinate):
            return None
        coordinates.append(coordinate)
    return coordinates


def _report_final_state_verified(
    proof: Any,
    attempted_type: str,
    raw_record: Dict[str, Any],
    raw_attempt: Dict[str, Any],
) -> bool:
    """Bind report certification to Blender's complete post-stack proof."""

    if not isinstance(proof, dict) or attempted_type not in REPRESENTATIONS:
        return False
    if proof.get("status") != "verified" or proof.get("representation") != attempted_type:
        return False
    if not isinstance(proof.get("failures"), list) or proof["failures"]:
        return False
    raw_page = raw_record.get("page")
    raw_span = raw_record.get("source_span_id")
    if (
        not isinstance(raw_page, int)
        or isinstance(raw_page, bool)
        or raw_page <= 0
        or not isinstance(raw_span, int)
        or isinstance(raw_span, bool)
        or raw_span <= 0
        or proof.get("page_number") != raw_page
    ):
        return False
    stack_offset = proof.get("stack_offset_m")
    if (
        not isinstance(stack_offset, (int, float))
        or isinstance(stack_offset, bool)
        or not math.isfinite(float(stack_offset))
        or proof.get("canonical_parent_verified") is not True
        or proof.get("provenance_parent_handle_verified") is not True
    ):
        return False
    stack_offset_value = float(stack_offset)

    raw_entity_ids = raw_record.get("entity_ids")
    if not isinstance(raw_entity_ids, list) or any(
        not isinstance(entity_id, str)
        or not entity_id
        or entity_id != entity_id.strip()
        for entity_id in raw_entity_ids
    ):
        return False
    if len(raw_entity_ids) != len(set(raw_entity_ids)):
        return False
    entity_proofs = proof.get("entities")
    if not isinstance(entity_proofs, list):
        return False
    if [
        entity_proof.get("entity_id") if isinstance(entity_proof, dict) else None
        for entity_proof in entity_proofs
    ] != raw_entity_ids:
        return False

    source_id = raw_record.get("item_id")
    if (
        not isinstance(source_id, str)
        or not source_id
        or source_id != source_id.strip()
    ):
        return False
    if not raw_entity_ids:
        logical_id = raw_record.get("logical_delivery_id")
        source_manifest_sha256 = raw_record.get("source_manifest_sha256")
        return bool(
            raw_record.get("zero_ink_delivery") is True
            and proof.get("logical_zero_ink_delivery") is True
            and logical_id == f"{source_id}:zero-ink:{attempted_type}"
            and proof.get("logical_delivery_id") == logical_id
            and isinstance(source_manifest_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256)
            and proof.get("source_manifest_sha256") == source_manifest_sha256
        )

    attempt_evidence = raw_attempt.get("evidence")
    if not isinstance(attempt_evidence, dict):
        return False
    try:
        unstacked_locations = _delivery_expected_locations(
            attempt_evidence,
            raw_entity_ids,
        )
        entity_expectations, _expectation_entries, expectation_failures = (
            _delivery_entity_expectations(
                attempt_evidence,
                raw_entity_ids,
                attempted_type,
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if (
        expectation_failures
        or set(unstacked_locations) != set(raw_entity_ids)
        or set(entity_expectations) != set(raw_entity_ids)
    ):
        return False

    expected_object_type = {
        "labels": "FONT",
        "text": "FONT",
        "3d_text": "FONT",
        "glyphs": "CURVE",
        "geometry": "MESH",
        "raster": "MESH",
    }[attempted_type]
    for entity_proof in entity_proofs:
        if not isinstance(entity_proof, dict):
            return False
        if entity_proof.get("actual_object_type") != expected_object_type:
            return False
        actual_location = _report_finite_coordinates(
            entity_proof.get("actual_location_m"),
            3,
        )
        if actual_location is None:
            return False
        expected_location = _report_finite_coordinates(
            entity_proof.get("expected_location_m"),
            2,
        )
        unstacked_location = _report_finite_coordinates(
            unstacked_locations.get(entity_proof["entity_id"]),
            2,
        )
        if unstacked_location is None:
            return False
        recomputed_location = [
            unstacked_location[0],
            unstacked_location[1] + stack_offset_value,
        ]
        if not all(math.isfinite(coordinate) for coordinate in recomputed_location):
            return False
        if expected_location is None or any(
            abs(proven - expected) > 1e-7
            for proven, expected in zip(
                expected_location,
                recomputed_location,
                strict=True,
            )
        ) or any(
            abs(actual - expected) > 1e-7
            for actual, expected in zip(
                actual_location[:2],
                expected_location,
                strict=True,
            )
        ):
            return False
        expectation_kind = entity_proof.get("expectation_kind")
        sealed_expectation = entity_expectations.get(entity_proof["entity_id"])
        if (
            not isinstance(sealed_expectation, dict)
            or expectation_kind != sealed_expectation.get("kind")
            or expectation_kind not in {"character", "span", "raster"}
        ):
            return False
        if attempted_type == "raster":
            if expectation_kind != "raster":
                return False
        elif expectation_kind == "raster":
            return False
        if (
            entity_proof.get("object_handle_verified") is not True
            or entity_proof.get("source_item_verified") is not True
            or entity_proof.get("representation_fields_verified") is not True
            or entity_proof.get("affine_verified") is not True
            or entity_proof.get("physical_ink_continuity_verified") is not True
        ):
            return False
        expected_character_state = True if expectation_kind == "character" else None
        if entity_proof.get("character_identity_verified") is not expected_character_state:
            return False
        if expectation_kind == "raster":
            if entity_proof.get("text_material_binding_verified") is not None or any(
                entity_proof.get(field) is not True
                for field in (
                    "raster_geometry_verified",
                    "raster_uv_verified",
                    "raster_material_binding_verified",
                )
            ):
                return False
        elif entity_proof.get("text_material_binding_verified") is not True:
            return False
        ink_count = entity_proof.get("live_ink_element_count")
        if (
            not isinstance(ink_count, int)
            or isinstance(ink_count, bool)
            or ink_count <= 0
        ):
            return False
        measurement = entity_proof.get("live_ink_measurement")
        if measurement == "explicit_test_host_points":
            if getattr(bpy, "_bc_pdf_vector_importer_test_host", False) is not True:
                return False
        elif measurement != "evaluated_mesh_vertices":
            return False
        if expectation_kind == "character":
            expected_bounds = _report_finite_coordinates(
                entity_proof.get("expected_world_ink_bounds_m"),
                4,
            )
            actual_bounds = _report_finite_coordinates(
                entity_proof.get("actual_world_ink_bounds_m"),
                4,
            )
            if (
                expected_bounds is None
                or actual_bounds is None
                or any(
                    abs(actual - expected) > 6e-5
                    for actual, expected in zip(
                        actual_bounds,
                        expected_bounds,
                        strict=True,
                    )
                )
            ):
                return False

    zero_count = raw_record.get("zero_ink_character_count")
    if isinstance(zero_count, int) and not isinstance(zero_count, bool) and zero_count > 0:
        if (
            proof.get("logical_zero_ink_children") != zero_count
            or proof.get("logical_zero_ink_children_verified") is not True
            or proof.get("source_manifest_sha256")
            != raw_record.get("source_manifest_sha256")
        ):
            return False
    return True


def _report_attempt_sequence_verified(
    requested_type: str,
    attempted_types: List[str],
) -> bool:
    """Require Blender's compressed attempts to follow its finite fallback ladder."""

    if not requested_type or not attempted_types:
        return False
    try:
        expected_ladder = fallback_ladder(requested_type)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False
    compressed_attempts: List[str] = []
    for raw_attempted_type in attempted_types:
        attempted_type = _report_text_type(raw_attempted_type)
        if not attempted_type:
            return False
        if not compressed_attempts or compressed_attempts[-1] != attempted_type:
            compressed_attempts.append(attempted_type)
    return tuple(compressed_attempts) == expected_ladder[: len(compressed_attempts)]


def _report_impossibility_failures(
    raw_record: Dict[str, Any],
    raw_attempt: Any,
    attempted_type: str,
) -> List[str]:
    """Re-run Blender's item-bound impossibility validator at report ingestion."""

    if not isinstance(raw_attempt, dict):
        return ["attempt_record_invalid"]
    if attempted_type not in REPRESENTATIONS:
        return ["attempted_representation_invalid"]
    source_id = raw_record.get("item_id")
    page_number = raw_record.get("page")
    source_span_id = raw_record.get("source_span_id")
    if (
        not isinstance(source_id, str)
        or not source_id
        or source_id != source_id.strip()
    ):
        return ["item_identity_invalid"]
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or page_number <= 0
    ):
        return ["page_identity_invalid"]
    if (
        not isinstance(source_span_id, int)
        or isinstance(source_span_id, bool)
        or source_span_id <= 0
    ):
        return ["source_span_identity_invalid"]
    evidence = raw_attempt.get("evidence")
    reason = raw_attempt.get("reason")
    if not isinstance(evidence, dict):
        return ["impossibility_evidence_invalid"]
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
    ):
        return ["impossibility_reason_invalid"]
    try:
        failures = _impossibility_proof_failures(
            attempted_representation=attempted_type,
            item_id=source_id,
            page_number=page_number,
            source_span_id=source_span_id,
            outcome=AttemptOutcome.impossible(reason, evidence=evidence),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["impossibility_validator_unreadable"]
    return list(dict.fromkeys(str(failure) for failure in failures if str(failure)))


def _canonical_text_delivery_attempts(
    delivery_records: Any,
) -> List[Dict[str, Any]]:
    """Flatten Blender records into the host-neutral canonical attempt ledger."""

    ledger: List[Dict[str, Any]] = []
    for raw_record in list(delivery_records or []):
        if not isinstance(raw_record, dict):
            continue
        raw_source_id = raw_record.get("item_id")
        source_id = (
            raw_source_id
            if isinstance(raw_source_id, str)
            and raw_source_id
            and raw_source_id == raw_source_id.strip()
            else ""
        )
        requested = _report_exact_text_type(
            raw_record.get("requested_representation")
        )
        record_final = _report_exact_text_type(raw_record.get("final_representation"))
        record_status = str(raw_record.get("status") or "failed").strip().lower()
        raw_attempts = list(raw_record.get("attempts") or [])
        if not raw_attempts:
            raw_attempts = [
                {
                    "attempted_representation": requested,
                    "status": "failed",
                    "reason": "missing_item_attempt_ledger",
                    "evidence": {"record_status": record_status},
                    "entity_ids": [],
                    "owned_artifacts": [],
                    "superseded": True,
                    "cleanup": {"status": "failed", "removed": []},
                }
            ]
        attempted_types = [
            _report_exact_text_type(
                raw_attempt.get("attempted_representation")
                if "attempted_representation" in raw_attempt
                else raw_attempt.get("attempted_type")
            )
            if isinstance(raw_attempt, dict)
            else ""
            for raw_attempt in raw_attempts
        ]
        impossibility_failures_by_attempt = [
            _report_impossibility_failures(
                raw_record,
                raw_attempt,
                attempted_types[index],
            )
            if isinstance(raw_attempt, dict)
            and str(raw_attempt.get("status") or "").strip().lower()
            == "impossible"
            else []
            for index, raw_attempt in enumerate(raw_attempts)
        ]
        all_impossibility_proofs_verified = all(
            not (
                isinstance(raw_attempt, dict)
                and str(raw_attempt.get("status") or "").strip().lower()
                == "impossible"
            )
            or not impossibility_failures_by_attempt[index]
            for index, raw_attempt in enumerate(raw_attempts)
        )
        attempt_sequence_verified = _report_attempt_sequence_verified(
            requested,
            attempted_types,
        ) and all_impossibility_proofs_verified
        record_metadata = {
            str(key): value
            for key, value in raw_record.items()
            if str(key) not in {"attempts", "final_state_verification"}
            and not str(key).startswith("_")
        }
        for local_index, raw_attempt in enumerate(raw_attempts):
            attempt = raw_attempt if isinstance(raw_attempt, dict) else {}
            terminal = local_index == len(raw_attempts) - 1
            host_status = str(attempt.get("status") or "failed").strip().lower()
            attempted_type = _report_exact_text_type(
                attempt.get("attempted_representation")
                if "attempted_representation" in attempt
                else attempt.get("attempted_type")
            )
            terminal_verified = bool(
                terminal
                and host_status == "delivered"
                and record_status == "delivered"
            )
            impossibility_failures = impossibility_failures_by_attempt[local_index]
            impossibility_proof_verified = bool(
                host_status == "impossible" and not impossibility_failures
            )
            outcome = (
                "proven_impossible"
                if impossibility_proof_verified
                else "verified"
                if terminal_verified
                else "failed"
            )
            cleanup = attempt.get("cleanup")
            cleanup_status = (
                str(cleanup.get("status") or "").strip().lower()
                if isinstance(cleanup, dict)
                else ""
            )
            cleanup_complete = cleanup_status in (
                {"complete", "not_required"} if terminal else {"complete"}
            )
            identity_namespace_prefix = (
                "" if terminal else f"attempt:{source_id}:{local_index}:"
            )
            attempt_entity_pairs = _report_object_identity_pairs(
                attempt.get("entity_ids"),
                namespace_prefix=identity_namespace_prefix,
            )
            record_entity_pairs = _report_object_identity_pairs(
                raw_record.get("entity_ids")
            )
            owned_artifact_pairs = _report_owned_artifact_identity_pairs(
                attempt.get("owned_artifacts"),
                namespace_prefix=identity_namespace_prefix,
            )
            attempt_entity_ids = [identity for _, identity in attempt_entity_pairs]
            record_entity_ids = [identity for _, identity in record_entity_pairs]
            owned_artifact_ids = [identity for _, identity in owned_artifact_pairs]
            created_entity_ids = _report_merge_identity_evidence(
                attempt_entity_ids,
                owned_artifact_ids,
            )
            removed_entity_ids = _report_removed_entity_ids(
                cleanup,
                [*attempt_entity_pairs, *owned_artifact_pairs],
                namespace_prefix=identity_namespace_prefix,
            )
            delivery_entity_ids = (
                record_entity_ids or attempt_entity_ids
                if terminal_verified
                else []
            )
            if (
                terminal_verified
                and not delivery_entity_ids
                and raw_record.get("zero_ink_delivery") is True
            ):
                raw_logical_id = raw_record.get("logical_delivery_id")
                logical_id = _report_host_identity(
                    "logical",
                    raw_logical_id,
                )
                if logical_id:
                    delivery_entity_ids = [logical_id]
                    created_entity_ids = _report_merge_identity_evidence(
                        created_entity_ids,
                        [logical_id],
                    )
            support_entity_ids = (
                [
                    entity_id
                    for entity_id in created_entity_ids
                    if entity_id not in set(delivery_entity_ids)
                ]
                if terminal_verified
                else []
            )
            retained_entity_ids = [*delivery_entity_ids, *support_entity_ids]
            reused_entity_ids = [
                entity_id
                for entity_id in retained_entity_ids
                if entity_id not in set(created_entity_ids)
            ]
            final_state_proof = attempt.get("final_state_verification")
            if not isinstance(final_state_proof, dict):
                final_state_proof = raw_record.get("final_state_verification")
            final_state_verified = _report_final_state_verified(
                final_state_proof,
                attempted_type,
                raw_record,
                attempt,
            ) if terminal else False
            logical_zero_ink_verified = bool(
                raw_record.get("zero_ink_delivery") is True
                and not record_entity_ids
                and not attempt_entity_ids
                and delivery_entity_ids
                and delivery_entity_ids
                == [
                    _report_host_identity(
                        "logical",
                        raw_record.get("logical_delivery_id"),
                    )
                ]
            )
            physical_record_verified = bool(
                record_entity_ids
                and record_entity_ids == attempt_entity_ids
                and delivery_entity_ids == record_entity_ids
            )
            record_verified = bool(
                terminal_verified
                and attempt_sequence_verified
                and record_final == attempted_type
                and final_state_verified
                and (physical_record_verified or logical_zero_ink_verified)
            )
            ownership_verified = bool(
                record_verified
                and not set(removed_entity_ids).intersection(retained_entity_ids)
                and set(retained_entity_ids).issubset(
                    set(created_entity_ids).union(reused_entity_ids)
                )
                and not reused_entity_ids
            )
            entry = {
                str(key): value
                for key, value in attempt.items()
                if not str(key).startswith("_")
            }
            entry.update(
                {
                    "source_item_id": source_id,
                    "requested_type": requested,
                    "attempted_type": attempted_type,
                    "final_type": record_final if terminal_verified else None,
                    "outcome": outcome,
                    "cleanup_complete": cleanup_complete,
                    "created_entity_ids": created_entity_ids,
                    "removed_entity_ids": removed_entity_ids,
                    "delivery_entity_ids": delivery_entity_ids,
                    "support_entity_ids": support_entity_ids,
                    "referenced_entity_ids": [],
                    "reused_entity_ids": reused_entity_ids,
                    "strategy": str(attempt.get("strategy") or attempted_type),
                    "record_verified": record_verified,
                    "type_verified": bool(
                        terminal_verified
                        and attempt_sequence_verified
                        and record_final == attempted_type
                        and final_state_verified
                    ),
                    "visual_verified": bool(
                        terminal_verified
                        and attempt_sequence_verified
                        and final_state_verified
                    ),
                    "ownership_verified": ownership_verified,
                    "attempt_sequence_verified": attempt_sequence_verified,
                    "impossibility_proof_verified": impossibility_proof_verified,
                    "impossibility_proof_failures": impossibility_failures,
                    "host_outcome": host_status,
                    "evidence": (
                        dict(attempt.get("evidence") or {})
                        if isinstance(attempt.get("evidence"), dict)
                        else {}
                    ),
                }
            )
            if terminal:
                entry["host_record"] = dict(record_metadata)
                if (
                    "final_state_verification" not in entry
                    and isinstance(raw_record.get("final_state_verification"), dict)
                ):
                    entry["final_state_verification"] = dict(
                        raw_record["final_state_verification"]
                    )
            ledger.append(entry)
    return ledger


def _text_delivery_obligation_source_ids(
    stats: Dict[str, Any],
    expected_count: int,
) -> Dict[str, Any]:
    """Read the extraction inventory without deriving identities from outcomes."""

    raw_source_ids = stats.get("text_source_item_ids")
    source_item_ids = list(raw_source_ids) if isinstance(raw_source_ids, list) else []
    target_count = max(0, int(expected_count))
    exact_ids = bool(
        all(
            isinstance(source_id, str)
            and bool(source_id)
            and source_id == source_id.strip()
            for source_id in source_item_ids
        )
        and len(source_item_ids) == len(set(source_item_ids))
    )
    inventory_valid = bool(
        isinstance(raw_source_ids, list)
        and len(source_item_ids) == target_count
        and exact_ids
    )
    return {
        "source_item_ids": source_item_ids,
        "valid": inventory_valid,
        "expected_count": target_count,
        "actual_count": len(source_item_ids),
    }


def write_import_report(
    filepath: str,
    config: Dict,
    stats: Dict,
    *,
    import_mode: str = "auto",
    raster_pages: int = 0,
    output_path: Optional[str] = None,
    provenance_opts: Any = None,
) -> str:
    """Emit bcs.import_report/1.1 JSON for one import run."""
    from .pdfcadcore.import_report import build_actual_text_entity_types, build_import_report

    path = (
        output_path
        or config.get("import_report_path")
        or _default_import_report_path(filepath)
    )
    elapsed = float(stats.get("elapsed", 0.0) or 0.0)
    text_source_spans = int(stats.get("text_source_spans", stats.get("text_items", 0)) or 0)
    text_mode = _report_text_type(config.get("text_mode") or "3d_text")
    import_text_enabled = bool(config.get("import_text", True)) and text_mode != "none"
    raw_text_delivery = _text_delivery_from_provenance(
        provenance_opts if import_text_enabled else None
    )
    text_delivery_records = raw_text_delivery["items"]
    text_source_inventory = _text_delivery_obligation_source_ids(
        stats,
        text_source_spans,
    )
    obligation_source_ids = (
        list(text_source_inventory["source_item_ids"])
        if import_text_enabled
        else []
    )
    text_delivery_required = bool(obligation_source_ids)
    text_delivery_attempts = _canonical_text_delivery_attempts(
        text_delivery_records
    )
    expected_text_source_ids = (
        obligation_source_ids
        if import_text_enabled
        else []
    )
    text_representation_delivery = build_text_representation_delivery(
        text_delivery_attempts,
        requested_type=text_mode,
        required=text_delivery_required,
        expected_source_item_ids=expected_text_source_ids,
    )
    text_delivery_summary = raw_text_delivery["summary"]
    text_delivery = {
        "schema": raw_text_delivery["schema"],
        "summary": text_delivery_summary,
    }
    text_fallback = (
        _text_fallback_from_provenance(provenance_opts)
        if import_text_enabled
        else None
    )
    raster_delivery_failures = []
    for record in list(stats.get("raster_delivery_failures") or []):
        if not isinstance(record, dict):
            continue
        try:
            page = int(record.get("page", 0) or 0)
        except (TypeError, ValueError):
            page = 0
        stage = str(record.get("stage") or "").strip()
        reason = str(record.get("reason") or "").strip()
        if page > 0 and stage and reason:
            raster_delivery_failures.append({
                "page": page,
                "stage": stage,
                "reason": reason,
            })
    geometry_delivery_issues = [
        dict(record)
        for record in list(stats.get("geometry_delivery_issues") or [])
        if isinstance(record, dict)
    ]
    geometry_delivery_failures = [
        record
        for record in geometry_delivery_issues
        if str(record.get("status") or "").strip().lower() != "verified"
    ]
    if (
        raster_delivery_failures
        and isinstance(text_fallback, dict)
        and str(text_fallback.get("delivered") or "").strip().lower() == "raster"
    ):
        text_fallback = None
    raster_is_fallback = raster_pages > 0 and import_mode != "raster"
    geometry_approximations = [
        record
        for record in geometry_delivery_issues
        if str(record.get("status") or "").strip().lower() == "verified"
    ]
    text_fallback_attempted = any(
        bool(record.get("fallback_attempted"))
        or len(tuple(record.get("attempts") or ())) > 1
        for record in text_delivery_records
    )
    fallback_used = (
        raster_is_fallback
        or bool(geometry_approximations)
        or int(text_delivery_summary["fallback_items"]) > 0
        or text_fallback is not None
    )
    fallback_attempted = (
        fallback_used
        or text_fallback_attempted
        or bool(raster_delivery_failures and import_mode != "raster")
    )
    if raster_is_fallback:
        fallback_reason = f"raster_fallback_{raster_pages}_page{'s' if raster_pages != 1 else ''}"
    elif int(text_delivery_summary["fallback_items"]) > 0:
        count = int(text_delivery_summary["fallback_items"])
        fallback_reason = f"text_fallback_{count}_item{'s' if count != 1 else ''}"
    elif geometry_approximations:
        count = len(geometry_approximations)
        fallback_reason = f"geometry_approximation_{count}_primitive{'s' if count != 1 else ''}"
    else:
        fallback_reason = None
    from .pdfcadcore.fitz_loader import sample_process_mb

    phases = dict(stats.get("performance_phases") or {})
    if elapsed > 0 and "total_ms" not in phases:
        phases["total_ms"] = elapsed * 1000.0
    text_glyph_estimate = int(stats.get("text_glyph_estimate", 0) or 0)
    bootstrap_text_items = list(stats.get("parts_bootstrap_text_items") or [])
    try:
        delivered_text_counts = getattr(provenance_opts, "_text_delivered_entity_counts", None)
    except AttributeError:
        delivered_text_counts = None
    if not import_text_enabled:
        delivered_text_counts = {}
    elif not isinstance(delivered_text_counts, dict):
        delivered_text_counts = None
    font_rendered = None
    if delivered_text_counts:
        try:
            font_rendered = any(
                int(delivered_text_counts.get(bucket, 0) or 0) > 0
                for bucket in ("native_label", "native_text", "native_3d_text")
            )
        except (TypeError, ValueError):
            font_rendered = None

    delivery_source_items = int(text_delivery_summary["source_items"])
    delivery_delivered_items = int(text_delivery_summary["delivered_items"])
    delivery_failed_items = int(text_delivery_summary["failed_items"])
    text_source_inventory_valid = bool(
        not import_text_enabled or text_source_inventory["valid"]
    )
    text_delivery_verified = bool(
        not import_text_enabled
        or (
            text_source_inventory_valid
            and text_representation_delivery["verified"] is True
        )
    )
    terminal_failure = {}
    if not text_delivery_verified:
        terminal_failure["text_delivery"] = {
            "required_source_items": text_source_spans,
            "source_inventory_valid": text_source_inventory_valid,
            "source_inventory_items": int(text_source_inventory["actual_count"]),
            "recorded_source_items": delivery_source_items,
            "delivered_items": delivery_delivered_items,
            "failed_items": delivery_failed_items,
            "failed_item_ids": list(text_delivery_summary["failed_item_ids"]),
        }
    if raster_delivery_failures:
        terminal_failure["raster_delivery"] = list(raster_delivery_failures)
    if geometry_delivery_failures:
        terminal_failure["geometry_delivery"] = list(geometry_delivery_failures)
    if stats.get("temp_cleanup_error"):
        terminal_failure["temp_cleanup"] = str(stats.get("temp_cleanup_error"))
    extra = {
        "curves": int(stats.get("curves", 0) or 0),
        "meshes": int(stats.get("meshes", 0) or 0),
        "images": int(stats.get("images", 0) or 0),
        "model_3d_intent": stats.get("model_3d_intent"),
        "model_3d": stats.get("model_3d"),
        "resolved_scale": stats.get("resolved_scale"),
        "scale_hints": stats.get("scale_hints"),
        "fallback_attempted": bool(fallback_attempted),
        "result_status": "incomplete" if terminal_failure else "success",
        "text_mode": text_mode,
    }
    if terminal_failure:
        extra["terminal_failure"] = terminal_failure
    if int(text_delivery_summary["source_items"]) > 0:
        extra["text_delivery"] = text_delivery
    extra["text_delivery_obligations"] = {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": text_delivery_required,
        "requested_type": text_mode,
        "source_item_ids": obligation_source_ids,
    }
    extra["text_delivery_attempts"] = text_delivery_attempts
    extra["text_representation_delivery"] = text_representation_delivery
    if raster_delivery_failures:
        extra["raster_delivery_failures"] = raster_delivery_failures
        extra["raster_delivery_failure_count"] = len(raster_delivery_failures)
    if geometry_delivery_issues:
        extra["geometry_delivery_issues"] = geometry_delivery_issues
        extra["geometry_delivery_issue_count"] = len(geometry_delivery_issues)
    if stats.get("temp_cleanup_error"):
        extra["temp_cleanup_error"] = str(stats.get("temp_cleanup_error"))
    if int(stats.get("recognition_skipped_pages", 0) or 0) > 0:
        extra["recognition_skipped_pages"] = int(stats.get("recognition_skipped_pages", 0) or 0)
    # TEXTMODE-1 item 12: unknown text_mode strings are normalized to the
    # 3d_text default loudly — the report notes what the input actually was.
    try:
        normalized_from = getattr(
            provenance_opts, "_text_mode_normalization_warnings", None
        )
    except AttributeError:
        normalized_from = None
    if normalized_from:
        extra["text_mode_normalized_from"] = sorted(
            str(value) for value in normalized_from
        )
    extra["actual_text_entity_types"] = build_actual_text_entity_types(
        host_app="blender",
        text_mode=text_mode if import_text_enabled else None,
        count=int(stats.get("text_items", 0) or 0) if import_text_enabled else 0,
        font_rendered=font_rendered if import_text_enabled else False,
        delivered_counts=delivered_text_counts,
    )

    report = build_import_report(
        host_app="blender",
        host_version=_blender_host_version(),
        runtime_lang="python",
        runtime_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        importer_version=_importer_version(),
        pdf_path=filepath,
        mode=import_mode,
        pages=int(stats.get("pages_imported", stats.get("pages", 0)) or 0),
        primitive_count=int(stats.get("primitives", 0) or 0),
        text_count=int(stats.get("text_items", 0) or 0),
        image_count=int(stats.get("images", 0) or 0),
        layer_count=int(stats.get("collections", 0) or 0),
        elapsed_ms=elapsed * 1000.0,
        performance_phases=phases or None,
        peak_mb=sample_process_mb(),
        warnings=(
            max(
                int(text_delivery_summary["failed_items"]),
                1 if not text_delivery_verified else 0,
            )
            + len(raster_delivery_failures)
            + len(geometry_delivery_issues)
            + (1 if stats.get("temp_cleanup_error") else 0)
        ),
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        pdf_engine_version=_pymupdf_version(),
        import_text=import_text_enabled,
        text_mode=text_mode,
        text_source_spans=text_source_spans,
        text_glyph_estimate=text_glyph_estimate,
        text_fallback=text_fallback,
        extra=extra,
    )
    if raster_delivery_failures:
        diagnostics = report.extra.get("diagnostics")
        if isinstance(diagnostics, dict):
            signals = list(diagnostics.get("signals") or [])
            if "raster_delivery_failed" not in signals:
                signals.append("raster_delivery_failed")
            diagnostics["signals"] = signals
            actions = list(diagnostics.get("recommended_actions") or [])
            action = (
                "The terminal raster image could not be created in Blender; inspect "
                "extra.raster_delivery_failures before trusting the import."
            )
            if action not in actions:
                actions.append(action)
            diagnostics["recommended_actions"] = actions
    diagnostics = report.extra.get("diagnostics")
    if isinstance(diagnostics, dict) and int(text_delivery_summary["source_items"]) > 0:
        signals = list(diagnostics.get("signals") or [])
        actions = list(diagnostics.get("recommended_actions") or [])
        if int(text_delivery_summary["fallback_items"]) > 0:
            if "text_representation_fallback_used" not in signals:
                signals.append("text_representation_fallback_used")
            action = (
                "Review extra.text_delivery_attempts and the compact terminal joins in "
                "extra.text_representation_delivery.items for each requested "
                "representation, impossibility proof, cleanup result, and final "
                "entity identity."
            )
            if action not in actions:
                actions.append(action)
        if int(text_delivery_summary["failed_items"]) > 0:
            if "text_delivery_failed" not in signals:
                signals.append("text_delivery_failed")
            action = (
                "One or more text items were not delivered; inspect "
                "extra.text_delivery.summary.failed_item_ids before trusting the import."
            )
            if action not in actions:
                actions.append(action)
        diagnostics["signals"] = signals
        diagnostics["recommended_actions"] = actions

    provenance_objects = list(getattr(provenance_opts, "_source_provenance_objects", []) or [])
    if provenance_objects:
        from .pdfcadcore.source_provenance import (
            SCHEMA,
            ensure_import_session_id,
            write_source_provenance_sidecar,
        )

        session_id = ensure_import_session_id(provenance_opts)
        sidecar_path = str(Path(path).with_name("source_provenance.json"))
        build_stamp = str((report.report_meta or {}).get("build_stamp") or "")
        write_source_provenance_sidecar(
            output_path=sidecar_path,
            import_session_id=session_id,
            pdf_path=filepath,
            objects=provenance_objects,
            host_app="blender",
            importer_version=_importer_version(),
            build_stamp=build_stamp,
            page_count=int(stats.get("pages_imported", stats.get("pages", 0)) or 0) or None,
        )
        report.extra["source_provenance_path"] = Path(sidecar_path).name
        report.extra["source_provenance"] = {
            "schema": SCHEMA,
            "import_session_id": session_id,
            "object_count": len(provenance_objects),
        }

    from .pdfcadcore.parts_bootstrap import extract_bootstrap_rows, write_parts_bootstrap_sidecar

    bootstrap_path = str(Path(path).with_name("parts_bootstrap.json"))
    bootstrap_rows = extract_bootstrap_rows(bootstrap_text_items)
    build_stamp = str((report.report_meta or {}).get("build_stamp") or "")
    import_build_stamp = {
        "host": "blender",
        "semver": _importer_version(),
    }
    if build_stamp:
        import_build_stamp["build_stamp"] = build_stamp
    write_parts_bootstrap_sidecar(
        bootstrap_path,
        filepath,
        page_count=int(stats.get("pages_imported", stats.get("pages", 0)) or 0),
        rows=bootstrap_rows,
        import_build_stamp=import_build_stamp,
    )
    report.extra["parts_bootstrap"] = {
        "schema": "bcs.parts_bootstrap/1.0",
        "sidecar_path": Path(bootstrap_path).name,
        "row_count": len(bootstrap_rows),
        "note": "BOM row extraction from drawing text" if bootstrap_rows else "no BOM rows detected",
    }

    report.write_json(path)
    try:
        import_build_stamp["report_sha256"] = hashlib.sha256(
            Path(path).read_bytes()
        ).hexdigest()
        write_parts_bootstrap_sidecar(
            bootstrap_path,
            filepath,
            page_count=int(stats.get("pages_imported", stats.get("pages", 0)) or 0),
            rows=bootstrap_rows,
            import_build_stamp=import_build_stamp,
        )
    except OSError:
        pass
    return path


def _iter_collection_tree(root_collection: bpy.types.Collection):
    stack = [root_collection]
    seen = set()
    while stack:
        col = stack.pop()
        if col is None:
            continue
        key = id(col)
        if key in seen:
            continue
        seen.add(key)
        yield col
        try:
            stack.extend(list(col.children))
        except Exception:
            pass


def _find_layer_collection(layer_col, target_collection):
    if layer_col is None:
        return None
    try:
        if layer_col.collection == target_collection:
            return layer_col
    except Exception:
        pass
    for child in getattr(layer_col, "children", []):
        found = _find_layer_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _unhide_collection_tree(root_collection: bpy.types.Collection) -> None:
    scene = bpy.context.scene
    for col in _iter_collection_tree(root_collection):
        try:
            col.hide_viewport = False
        except Exception:
            pass
        try:
            col.hide_render = False
        except Exception:
            pass

        for view_layer in scene.view_layers:
            try:
                layer_col = _find_layer_collection(view_layer.layer_collection, col)
                if layer_col is None:
                    continue
                layer_col.exclude = False
                layer_col.hide_viewport = False
                layer_col.holdout = False
                layer_col.indirect_only = False
            except Exception:
                continue


def _world_bounds_for_objects(objects):
    try:
        from mathutils import Vector
    except Exception:
        return None, None

    min_v = None
    max_v = None
    for obj in objects:
        if obj is None:
            continue
        if getattr(obj, "type", "") in {"CAMERA", "LIGHT"}:
            continue
        try:
            corners = obj.bound_box
            mw = obj.matrix_world
        except Exception:
            continue
        if not corners:
            continue
        try:
            world_pts = [mw @ Vector((c[0], c[1], c[2])) for c in corners]
        except Exception:
            continue
        for p in world_pts:
            if min_v is None:
                min_v = Vector((p.x, p.y, p.z))
                max_v = Vector((p.x, p.y, p.z))
            else:
                min_v.x = min(min_v.x, p.x)
                min_v.y = min(min_v.y, p.y)
                min_v.z = min(min_v.z, p.z)
                max_v.x = max(max_v.x, p.x)
                max_v.y = max(max_v.y, p.y)
                max_v.z = max(max_v.z, p.z)
    return min_v, max_v


def _close(a: float, b: float, tol: float = 1.0e-4) -> bool:
    return abs(float(a) - float(b)) <= tol


def _is_default_startup_cube(obj) -> bool:
    try:
        if obj is None or getattr(obj, "type", "") != "MESH":
            return False
        if getattr(obj, "name", "") != "Cube":
            return False
        if getattr(obj, "parent", None) is not None:
            return False
        loc = obj.location
        rot = obj.rotation_euler
        scl = obj.scale
        dims = obj.dimensions
        if not (_close(loc.x, 0.0) and _close(loc.y, 0.0) and _close(loc.z, 0.0)):
            return False
        if not (_close(rot.x, 0.0) and _close(rot.y, 0.0) and _close(rot.z, 0.0)):
            return False
        if not (_close(scl.x, 1.0) and _close(scl.y, 1.0) and _close(scl.z, 1.0)):
            return False
        if not (_close(dims.x, 2.0, 1.0e-3) and _close(dims.y, 2.0, 1.0e-3) and _close(dims.z, 2.0, 1.0e-3)):
            return False
        return True
    except Exception:
        return False


def _auto_hide_default_cube(scene) -> int:
    if scene is None:
        return 0
    hidden = 0
    try:
        for obj in scene.objects:
            if not _is_default_startup_cube(obj):
                continue
            try:
                obj.hide_set(True)
            except Exception:
                pass
            try:
                obj.hide_viewport = True
            except Exception:
                pass
            hidden += 1
    except Exception:
        return hidden
    return hidden


def _text_item_profile(text_items) -> Dict[str, int]:
    total = 0
    longish = 0
    alpha = 0
    for item in text_items or []:
        raw = getattr(item, "text", "")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            continue
        total += 1
        if any(ch.isalpha() for ch in text):
            alpha += 1
        # Narrative/doc-style text runs are usually long and/or multi-word.
        if len(text) >= 18 or text.count(" ") >= 2:
            longish += 1
    return {"total": total, "longish": longish, "alpha": alpha}


def _looks_like_text_cloud_page(primitives_count: int, text_items) -> bool:
    profile = _text_item_profile(text_items)
    total = profile["total"]
    longish = profile["longish"]
    alpha = profile["alpha"]
    if total < 180 or longish < 40:
        return False

    long_ratio = longish / float(max(total, 1))
    text_to_vector_ratio = total / float(max(primitives_count, 1))
    alpha_ratio = alpha / float(max(total, 1))

    # Typical CAD drawings have lots of short tokens (fractions, IDs).
    # Narrative map/plan pages tend to have many longer, multi-word runs.
    if long_ratio >= 0.28 and alpha_ratio >= 0.55 and text_to_vector_ratio >= 2.5:
        return True

    # Heavy pages with extreme primitive counts can hang; prefer raster when
    # they are also text-heavy.
    if primitives_count >= 12000 and total >= 300 and long_ratio >= 0.20:
        return True

    return False


def _primitive_bbox_area_ratio(prim, page_area_mm2: float) -> float:
    if page_area_mm2 <= 1e-9:
        return 0.0
    try:
        if getattr(prim, "bbox", None):
            x0, y0, x1, y1 = prim.bbox
            return max(0.0, (abs(float(x1) - float(x0)) * abs(float(y1) - float(y0))) / page_area_mm2)
    except Exception:
        pass
    try:
        pts = list(getattr(prim, "points", []) or [])
        if len(pts) >= 3:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            return max(0.0, ((max(xs) - min(xs)) * (max(ys) - min(ys))) / page_area_mm2)
    except Exception:
        pass
    return 0.0


def _looks_like_page_frame_only(page_data) -> bool:
    prims = list(getattr(page_data, "primitives", []) or [])
    if not prims or len(prims) > 12:
        return False
    text_count = len(list(getattr(page_data, "text_items", []) or []))
    if text_count > 12:
        return False
    page_area = max(float(getattr(page_data, "width", 0.0) or 0.0) * float(getattr(page_data, "height", 0.0) or 0.0), 1.0)
    big_frames = 0
    for prim in prims:
        ratio = _primitive_bbox_area_ratio(prim, page_area)
        if ratio >= 0.88:
            big_frames += 1
    return big_frames >= 1


def _focus_view_on_import(
    root_collection: bpy.types.Collection,
    keep_selected: bool = False,
    prefer_material_preview: bool = False,
) -> bool:
    """
    Select imported objects and frame them in all visible VIEW_3D areas.
    Returns True when at least one 3D view was focused.
    """
    if root_collection is None:
        return False

    _unhide_collection_tree(root_collection)

    objects = []
    try:
        objects = [obj for obj in root_collection.all_objects if obj is not None]
    except Exception:
        # Fallback traversal for older Blender collection APIs.
        stack = [root_collection]
        seen_cols = set()
        while stack:
            col = stack.pop()
            if col is None or id(col) in seen_cols:
                continue
            seen_cols.add(id(col))
            try:
                objects.extend([obj for obj in col.objects if obj is not None])
            except Exception:
                pass
            try:
                stack.extend(list(col.children))
            except Exception:
                pass

    if not objects:
        return False

    # Ensure imported objects are visible before focusing.
    visible_objects = []
    for obj in objects:
        try:
            obj.hide_set(False)
        except Exception:
            pass
        try:
            obj.hide_viewport = False
        except Exception:
            pass
        try:
            obj.hide_render = False
        except Exception:
            pass
        visible_objects.append(obj)

    min_v, max_v = _world_bounds_for_objects(visible_objects)

    view_layer = bpy.context.view_layer

    # Select all imported objects for framing.
    try:
        for obj in view_layer.objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
    except Exception:
        pass

    selected = []
    for obj in visible_objects:
        try:
            obj.select_set(True)
            selected.append(obj)
        except Exception:
            continue

    if not selected:
        return False

    try:
        view_layer.objects.active = selected[0]
    except Exception:
        pass

    focused = False
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(
                    window=window,
                    screen=screen,
                    area=area,
                    region=region,
                    scene=bpy.context.scene,
                    view_layer=view_layer,
                ):
                    # If viewport is in Local View isolation, imported objects can
                    # exist in outliner but remain invisible. Exit isolation first.
                    try:
                        space = area.spaces.active
                        # Viewport-local collection isolation can hide imported
                        # collections even though they appear in the outliner.
                        if hasattr(space, "use_local_collections"):
                            space.use_local_collections = False
                        if getattr(space, "local_view", None) is not None:
                            bpy.ops.view3d.localview(frame_selected=False)
                        # Expand clip range so large drawings cannot disappear.
                        if min_v is not None and max_v is not None:
                            span_x = abs(max_v.x - min_v.x)
                            span_y = abs(max_v.y - min_v.y)
                            span_z = abs(max_v.z - min_v.z)
                            radius = max(span_x, span_y, span_z, 0.25)
                            space.clip_start = max(1.0e-5, min(float(space.clip_start), radius / 10000.0))
                            space.clip_end = max(float(space.clip_end), radius * 200.0, 1000.0)
                    except Exception:
                        pass

                    try:
                        bpy.ops.view3d.view_axis(type="TOP", align_active=False)
                    except Exception:
                        pass
                    # Prefer orthographic for plan-like PDF drawings.
                    try:
                        rv3d = area.spaces.active.region_3d
                        if rv3d is not None:
                            rv3d.view_perspective = "ORTHO"
                            if min_v is not None and max_v is not None:
                                center = (min_v + max_v) * 0.5
                                span_x = abs(max_v.x - min_v.x)
                                span_y = abs(max_v.y - min_v.y)
                                span_z = abs(max_v.z - min_v.z)
                                radius = max(span_x, span_y, span_z, 0.25)
                                rv3d.view_location = center
                                rv3d.view_distance = max(radius * 1.35, 0.4)
                        if prefer_material_preview:
                            try:
                                space.shading.type = "MATERIAL"
                                if hasattr(space.shading, "color_type"):
                                    space.shading.color_type = "TEXTURE"
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # Re-frame after switching orientation.
                    try:
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                    except Exception:
                        pass
                focused = True
            except Exception:
                continue

    # Fallback framing pass: try view_all in each 3D area when selected framing
    # didn't succeed (file-browser context can be finicky on some setups).
    if not focused:
        for window in wm.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type != "VIEW_3D":
                    continue
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(
                        window=window,
                        screen=screen,
                        area=area,
                        region=region,
                        scene=bpy.context.scene,
                        view_layer=view_layer,
                    ):
                        try:
                            space = area.spaces.active
                            if hasattr(space, "use_local_collections"):
                                space.use_local_collections = False
                            if getattr(space, "local_view", None) is not None:
                                bpy.ops.view3d.localview(frame_selected=False)
                        except Exception:
                            pass
                        bpy.ops.view3d.view_axis(type="TOP", align_active=False)
                        try:
                            rv3d = area.spaces.active.region_3d
                            if rv3d is not None:
                                rv3d.view_perspective = "ORTHO"
                                if min_v is not None and max_v is not None:
                                    center = (min_v + max_v) * 0.5
                                    span_x = abs(max_v.x - min_v.x)
                                    span_y = abs(max_v.y - min_v.y)
                                    span_z = abs(max_v.z - min_v.z)
                                    radius = max(span_x, span_y, span_z, 0.25)
                                    rv3d.view_location = center
                                    rv3d.view_distance = max(radius * 1.35, 0.4)
                            if prefer_material_preview:
                                try:
                                    space.shading.type = "MATERIAL"
                                    if hasattr(space.shading, "color_type"):
                                        space.shading.color_type = "TEXTURE"
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            bpy.ops.view3d.view_all(center=False)
                        except Exception:
                            pass
                    focused = True
                except Exception:
                    continue

    if not keep_selected:
        # Imported objects are selected only for framing; clear selection after.
        for obj in objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
        try:
            view_layer.objects.active = None
        except Exception:
            pass

    return focused


def _extract_image_placements(doc, page, page_num: int, import_cfg, image_dir: str) -> list[dict]:
    """Extract embedded image XObjects and map them into page coordinates (mm)."""
    placements: list[dict] = []
    if not image_dir:
        return placements

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    page_rect = page.rect
    page_height = float(page_rect.height)
    page_x0 = float(getattr(page_rect, "x0", 0.0) or 0.0)
    page_y0 = float(getattr(page_rect, "y0", 0.0) or 0.0)
    page_rotation = _page_rotation_transform(
        page_rect,
        getattr(page, "rotation_matrix", fitz.Matrix(1, 0, 0, 1, 0, 0)),
    )
    seen_xrefs: set[int] = set()

    for img_info in page.get_images(full=True):
        xref = int(img_info[0])
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)

        try:
            pix = fitz.Pixmap(doc, xref)
            color_space_n = None
            try:
                color_space_n = int(getattr(getattr(pix, "colorspace", None), "n", 0))
            except (TypeError, ValueError):
                color_space_n = None

            needs_rgb = pix.alpha or pix.n != 3 or (color_space_n is not None and color_space_n != 3)
            if needs_rgb:
                pix = fitz.Pixmap(fitz.csRGB, pix)

            image_path = os.path.join(image_dir, f"page_{page_num:03d}_xref_{xref}.png")
            pix.save(image_path)
        except (RuntimeError, OSError, ValueError, TypeError):
            continue

        try:
            image_rects = page.get_image_rects(xref, transform=True)
        except TypeError:
            image_rects = [
                (
                    rect,
                    fitz.Matrix(
                        float(rect.width),
                        0.0,
                        0.0,
                        float(rect.height),
                        float(rect.x0),
                        float(rect.y0),
                    ),
                )
                for rect in page.get_image_rects(xref)
            ]
        for _rect, image_transform in image_rects:
            quad_mm = []
            for unit_x, unit_y in ((0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)):
                point = fitz.Point(unit_x, unit_y) * image_transform
                display_x, display_y = _transform_pdf_point(
                    float(point.x),
                    float(point.y),
                    page_rotation,
                )
                x_pt = display_x - page_x0
                y_pt = display_y - page_y0
                if import_cfg.flip_y:
                    y_pt = page_height - y_pt
                quad_mm.append(
                    (
                        x_pt * _MM_PER_PT * import_cfg.user_scale,
                        y_pt * _MM_PER_PT * import_cfg.user_scale,
                    )
                )
            xs = [point[0] for point in quad_mm]
            ys = [point[1] for point in quad_mm]
            left = min(xs)
            right = max(xs)
            bottom = min(ys)
            top = max(ys)

            placements.append(
                {
                    "path": image_path,
                    "x_mm": left,
                    "y_mm": bottom,
                    "width_mm": right - left,
                    "height_mm": top - bottom,
                    "quad_mm": quad_mm,
                    "xref": xref,
                    "page_number": page_num,
                }
            )

    return placements


def _render_page_raster(
    page,
    page_num: int,
    import_cfg,
    image_dir: str,
    *,
    excluded_text_bboxes=(),
) -> Optional[dict]:
    """Render a page background, removing text delivered as separate entities."""
    if not image_dir:
        return None

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    dpi = int(max(36, getattr(import_cfg, "raster_dpi", 300) or 300))
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    render_doc = None
    render_page = page
    exclusions = tuple(excluded_text_bboxes or ())
    try:
        if exclusions:
            source_doc = getattr(page, "parent", None)
            source_page_number = int(getattr(page, "number", page_num - 1))
            if source_doc is None:
                raise RuntimeError("source document unavailable for text-free page composition")
            render_doc = fitz.open()
            render_doc.insert_pdf(
                source_doc,
                from_page=source_page_number,
                to_page=source_page_number,
            )
            render_page = render_doc[0]
            page_rect = render_page.rect
            for raw_bbox in exclusions:
                if raw_bbox is None or len(raw_bbox) != 4:
                    raise ValueError("delivered text has no finite four-value source bbox")
                values = tuple(float(value) for value in raw_bbox)
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("delivered text source bbox is non-finite")
                rect = fitz.Rect(*values) & page_rect
                if rect.is_empty or rect.is_infinite:
                    raise ValueError("delivered text source bbox is outside the source page")
                render_page.add_redact_annot(rect, fill=None, cross_out=False)
            render_page.apply_redactions(images=0, graphics=0, text=0)

        pix = render_page.get_pixmap(matrix=matrix, alpha=False)
        suffix = "_background" if exclusions else ""
        image_path = os.path.join(
            image_dir,
            f"page_{page_num:03d}_raster_{dpi}dpi{suffix}.png",
        )
        pix.save(image_path)
    except (AttributeError, RuntimeError, OSError, ValueError, TypeError):
        return None
    finally:
        if render_doc is not None:
            render_doc.close()

    width_mm = float(page.rect.width) * _MM_PER_PT * import_cfg.user_scale
    height_mm = float(page.rect.height) * _MM_PER_PT * import_cfg.user_scale
    return {
        "path": image_path,
        "x_mm": 0.0,
        "y_mm": 0.0,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "xref": -1,
        "page_number": page_num,
        "excluded_text_bbox_count": len(exclusions),
        "composition": (
            "page_background_without_delivered_text"
            if exclusions
            else "complete_page_raster"
        ),
    }


def _delivered_text_bboxes(text_items, delivery_records, page_number: int):
    delivered_ids = {
        int(record.get("source_span_id"))
        for record in tuple(delivery_records or ())
        if int(record.get("page", 0) or 0) == int(page_number)
        and record.get("status") == "delivered"
        and record.get("final_representation")
        and record.get("entity_ids")
    }
    result = []
    for item in tuple(text_items or ()):
        try:
            item_id = int(item.id)
        except (AttributeError, TypeError, ValueError):
            continue
        bbox = getattr(item, "source_bbox_pdf", None)
        if item_id in delivered_ids and bbox is not None:
            result.append(tuple(float(value) for value in bbox))
    return result


def _freeze_created_image_plane_ownership(obj) -> Dict[str, Any]:
    """Freeze exact raster-plane references before later metadata can mutate."""

    try:
        object_name = str(getattr(obj, "name", "") or "")
        mesh = getattr(obj, "data", None)
        mesh_name = str(getattr(mesh, "name", "") or "") if mesh is not None else ""
        material_name = str(obj.get("pdf_image_material", "") or "")
        material_owned = bool(obj.get("pdf_image_material_owned", False))
        image_name = str(obj.get("pdf_image_datablock", "") or "")
        image_owned = bool(obj.get("pdf_image_datablock_owned", False))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        object_name = ""
        mesh = None
        mesh_name = ""
        material_name = ""
        material_owned = False
        image_name = ""
        image_owned = False
    material = None
    image = None
    if material_owned and material_name:
        try:
            material = next(
                (
                    candidate
                    for candidate in tuple(getattr(mesh, "materials", ()) or ())
                    if str(getattr(candidate, "name", "") or "") == material_name
                ),
                None,
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            material = None
        if material is None:
            try:
                material = bpy.data.materials.get(material_name)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                material = None
    if image_owned and image_name and material is not None:
        try:
            image = next(
                (
                    candidate
                    for node in tuple(material.node_tree.nodes)
                    for candidate in (getattr(node, "image", None),)
                    if candidate is not None
                    and str(getattr(candidate, "name", "") or "") == image_name
                ),
                None,
            )
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            image = None
    if image_owned and image_name and image is None:
        try:
            image = bpy.data.images.get(image_name)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            image = None
    return {
        "schema": "bc_bl_owned_image_plane_v1",
        "object": obj,
        "object_name": object_name,
        "mesh": mesh,
        "mesh_name": mesh_name,
        "material": material,
        "material_name": material_name,
        "image": image,
        "image_name": image_name,
    }


def _remove_created_image_plane(ownership, collection) -> Dict[str, Any]:
    """Remove only exact raster-plane references frozen at construction time."""

    if not isinstance(ownership, dict) or ownership.get("schema") != (
        "bc_bl_owned_image_plane_v1"
    ):
        return {
            "status": "failed",
            "removed": [],
            "detail": "frozen image-plane ownership is missing or invalid",
        }
    obj = ownership.get("object")
    mesh = ownership.get("mesh")
    material = ownership.get("material")
    image = ownership.get("image")
    object_name = str(ownership.get("object_name") or "")
    mesh_name = str(ownership.get("mesh_name") or "")
    removed: List[str] = []
    try:
        collection.objects.unlink(obj)
    except Exception:
        pass
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        removed.append(object_name)
    except Exception as exc:
        return {
            "status": "failed",
            "removed": removed,
            "exception_type": type(exc).__name__,
            "detail": str(exc),
        }
    if mesh is not None:
        try:
            bpy.data.meshes.remove(mesh)
            removed.append(mesh_name)
        except Exception as exc:
            return {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            }
    for registry_name, block, name in (
        ("materials", material, str(ownership.get("material_name") or "")),
        ("images", image, str(ownership.get("image_name") or "")),
    ):
        if block is None:
            continue
        registry = getattr(bpy.data, registry_name, None)
        remove = getattr(registry, "remove", None)
        try:
            if int(getattr(block, "users", 0) or 0) > 0:
                raise RuntimeError(
                    f"owned {registry_name} datablock still has users"
                )
            if not callable(remove):
                raise RuntimeError(f"no {registry_name} datablock remover")
            remove(block)
            removed.append(name)
        except Exception as exc:
            return {
                "status": "failed",
                "removed": removed,
                "exception_type": type(exc).__name__,
                "detail": str(exc),
            }
    return {"status": "complete", "removed": removed}


def _remove_owned_raster_file(path: str) -> Dict[str, Any]:
    """Remove only the item clip written by the current raster attempt."""
    if not path or not os.path.exists(path):
        return {"status": "complete", "removed": []}
    try:
        os.remove(path)
    except OSError as exc:
        return {
            "status": "failed",
            "removed": [],
            "exception_type": type(exc).__name__,
            "detail": str(exc),
        }
    return {"status": "complete", "removed": [str(path)]}


def _raise_for_incomplete_raster_cleanup(
    *results: Dict[str, Any],
    owned_objects=(),
    owned_datablocks=(),
    owned_files=(),
) -> None:
    incomplete = [result for result in results if result.get("status") != "complete"]
    if incomplete:
        raise _owned_raster_error(
            f"terminal raster attempt cleanup failed: {incomplete!r}",
            owned_objects=owned_objects,
            owned_datablocks=owned_datablocks,
            owned_files=owned_files,
        )


def _owned_raster_error(
    message: str,
    *,
    owned_objects=(),
    owned_datablocks=(),
    owned_files=(),
) -> RuntimeError:
    """Return an exception that preserves exact partial-construction ownership."""

    error = RuntimeError(message)
    error.owned_objects = tuple(value for value in owned_objects if value is not None)
    error.owned_datablocks = tuple(
        value for value in owned_datablocks if value is not None
    )
    error.owned_files = tuple(owned_files)
    error.owned_artifacts = ()
    return error


def _render_text_item_raster(
    page,
    text_item,
    collection: bpy.types.Collection,
    *,
    page_num: int,
    item_id: str,
    import_cfg,
    image_dir: str,
    z_offset_m: float = 0.0,
) -> Optional[bpy.types.Object]:
    """Render and verify one text span as the terminal item-scoped fallback."""
    source_bbox = getattr(text_item, "source_bbox_pdf", None)
    target_bbox = getattr(text_item, "bbox", None)
    if not image_dir or not source_bbox or not target_bbox:
        return None
    try:
        sx0, sy0, sx1, sy1 = (float(value) for value in source_bbox[:4])
        tx0, ty0, tx1, ty1 = (float(value) for value in target_bbox[:4])
    except (IndexError, TypeError, ValueError):
        return None
    values = (sx0, sy0, sx1, sy1, tx0, ty0, tx1, ty1)
    if not all(math.isfinite(value) for value in values):
        return None
    sx0, sx1 = sorted((sx0, sx1))
    sy0, sy1 = sorted((sy0, sy1))
    tx0, tx1 = sorted((tx0, tx1))
    ty0, ty1 = sorted((ty0, ty1))
    if sx1 - sx0 <= 1.0e-9 or sy1 - sy0 <= 1.0e-9:
        return None
    if tx1 - tx0 <= 1.0e-9 or ty1 - ty0 <= 1.0e-9:
        return None

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore

    dpi = int(max(36, getattr(import_cfg, "raster_dpi", 300) or 300))
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    clip = fitz.Rect(sx0, sy0, sx1, sy1)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item_id)).strip("_")
    if not safe_id:
        return None
    image_path = os.path.join(image_dir, f"{safe_id}_{dpi}dpi.png")
    image_owner_root = str(Path(image_dir).resolve())
    try:
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=True)
        if int(getattr(pix, "width", 0) or 0) <= 0 or int(getattr(pix, "height", 0) or 0) <= 0:
            return None
        samples = bytes(getattr(pix, "samples", b"") or b"")
        if not samples or not any(samples):
            return None
        pix.save(image_path)
        if not os.path.isfile(image_path) or os.path.getsize(image_path) <= 0:
            return None
    except (RuntimeError, OSError, ValueError, TypeError):
        cleanup = _remove_owned_raster_file(image_path)
        _raise_for_incomplete_raster_cleanup(
            cleanup,
            owned_files=((image_path, image_owner_root),),
        )
        return None

    source_id = int(getattr(text_item, "id", 0) or 0)
    placement = {
        "path": image_path,
        "x_mm": tx0,
        "y_mm": ty0,
        "width_mm": tx1 - tx0,
        "height_mm": ty1 - ty0,
        "xref": -1_000_000 - source_id,
        "page_number": int(page_num),
        "source_bbox_pdf": [sx0, sy0, sx1, sy1],
        "source_item_id": str(item_id),
    }
    try:
        obj = _create_image_plane(placement, collection, z_offset_m=z_offset_m)
    except Exception as exc:
        if tuple(getattr(exc, "owned_objects", ()) or ()) or tuple(
            getattr(exc, "owned_datablocks", ()) or ()
        ):
            exc.owned_files = tuple(getattr(exc, "owned_files", ()) or ()) + (
                (image_path, image_owner_root),
            )
            raise
        cleanup = _remove_owned_raster_file(image_path)
        _raise_for_incomplete_raster_cleanup(
            cleanup,
            owned_files=((image_path, image_owner_root),),
        )
        return None
    if obj is None:
        cleanup = _remove_owned_raster_file(image_path)
        _raise_for_incomplete_raster_cleanup(
            cleanup,
            owned_files=((image_path, image_owner_root),),
        )
        return None
    ownership = _freeze_created_image_plane_ownership(obj)
    try:
        obj["pdf_raster_source_item_id"] = str(item_id)
        obj["pdf_raster_source_bbox_pdf"] = list(placement["source_bbox_pdf"])
        obj["pdf_raster_dpi"] = dpi
        obj["pdf_image_owner_root"] = image_owner_root
    except Exception:
        plane_cleanup = _remove_created_image_plane(ownership, collection)
        file_cleanup = (
            _remove_owned_raster_file(image_path)
            if plane_cleanup.get("status") == "complete"
            else {"status": "not_attempted", "removed": []}
        )
        _raise_for_incomplete_raster_cleanup(
            plane_cleanup,
            file_cleanup,
            owned_objects=(ownership.get("object"),),
            owned_datablocks=(
                ownership.get("mesh"),
                ownership.get("material"),
                ownership.get("image"),
            ),
            owned_files=((image_path, image_owner_root),),
        )
        return None
    return obj


def _record_raster_delivery_failure(
    failures: List[Dict[str, Any]],
    *,
    page_num: int,
    stage: str,
    reason: str,
) -> None:
    """Keep terminal-raster delivery failures visible to the caller/report."""
    record = {
        "page": int(page_num),
        "stage": str(stage),
        "reason": str(reason),
    }
    if record not in failures:
        failures.append(record)


def _image_plane_geometry(placement: dict):
    """Return plane origin, local vertices, winding, and stable image UVs."""

    quad_mm = placement.get("quad_mm")
    if isinstance(quad_mm, (list, tuple)) and len(quad_mm) == 4:
        quad = [
            (float(point[0]) * _MM_TO_M, float(point[1]) * _MM_TO_M)
            for point in quad_mm
        ]
    else:
        x = float(placement.get("x_mm", 0.0)) * _MM_TO_M
        y = float(placement.get("y_mm", 0.0)) * _MM_TO_M
        width = max(float(placement.get("width_mm", 0.0)) * _MM_TO_M, 1e-9)
        height = max(float(placement.get("height_mm", 0.0)) * _MM_TO_M, 1e-9)
        quad = [
            (x, y),
            (x + width, y),
            (x + width, y + height),
            (x, y + height),
        ]
    origin_x, origin_y = quad[0]
    vertices = [
        (point_x - origin_x, point_y - origin_y, 0.0)
        for point_x, point_y in quad
    ]
    signed_area_twice = sum(
        quad[index][0] * quad[(index + 1) % 4][1]
        - quad[index][1] * quad[(index + 1) % 4][0]
        for index in range(4)
    )
    face = (0, 1, 2, 3) if signed_area_twice >= 0.0 else (0, 3, 2, 1)
    uv_by_vertex = {
        0: (0.0, 0.0),
        1: (1.0, 0.0),
        2: (1.0, 1.0),
        3: (0.0, 1.0),
    }
    return (origin_x, origin_y), vertices, face, uv_by_vertex


def _create_image_plane(
    placement: dict,
    collection: bpy.types.Collection,
    z_offset_m: float = 0.0,
) -> Optional[bpy.types.Object]:
    """Create a textured mesh plane for an extracted/rasterized PDF image."""
    path = placement.get("path")
    if not path or not os.path.isfile(path):
        return None

    try:
        image_bytes = Path(path).read_bytes()
    except OSError:
        return None
    if not image_bytes:
        return None
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()

    page_num = int(placement.get("page_number", 0))
    xref = int(placement.get("xref", -1))

    mesh = None
    obj = None
    material = None
    image = None
    material_created = False
    image_created = False
    try:
        (x, y), verts, face, uv_by_vert = _image_plane_geometry(placement)
        mesh = bpy.data.meshes.new(f"PDF_ImgMesh_{page_num}_{xref}")
        mesh.from_pydata(verts, [], [face])
        mesh.update()
        try:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            if uv_layer is None:
                raise RuntimeError("UVMap creation returned no layer")
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    v_idx = mesh.loops[loop_idx].vertex_index
                    uv_layer.data[loop_idx].uv = uv_by_vert.get(v_idx, (0.0, 0.0))
        except Exception as exc:
            raise RuntimeError("raster UV construction failed") from exc

        obj = bpy.data.objects.new(f"PDF_Image_{page_num}_{xref}", mesh)
        obj.location = (x, y, z_offset_m)
        collection.objects.link(obj)

        mat_name = f"PDF_Image_Mat_{page_num}_{xref}"
        material = bpy.data.materials.new(name=mat_name)
        material_created = True
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()

        tex = nodes.new(type="ShaderNodeTexImage")
        bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
        out = nodes.new(type="ShaderNodeOutputMaterial")

        image = bpy.data.images.load(path, check_existing=False)
        image_created = True
        packed_image_sha256 = pack_and_verify_bytes(image, image_bytes)
        if packed_image_sha256 != image_sha256:
            raise RuntimeError("packed image digest changed after verification")
        tex.image = image
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

        material.blend_method = "HASHED"
        if mesh.materials:
            mesh.materials[0] = material
        else:
            mesh.materials.append(material)

        obj["pdf_image_path"] = path
        obj["pdf_xref"] = xref
        obj["pdf_image_material"] = str(getattr(material, "name", "") or "")
        obj["pdf_image_datablock"] = str(getattr(image, "name", "") or "")
        obj["pdf_image_material_owned"] = bool(material_created)
        obj["pdf_image_datablock_owned"] = bool(image_created)
        obj["pdf_image_packed"] = True
        obj["pdf_image_sha256"] = image_sha256
        return obj
    except Exception as exc:
        cleanup_errors = []
        if obj is not None:
            try:
                collection.objects.unlink(obj)
            except Exception:
                pass
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
                obj = None
            except Exception as cleanup_exc:
                cleanup_errors.append(f"object:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if mesh is not None:
            try:
                bpy.data.meshes.remove(mesh)
                mesh = None
            except Exception as cleanup_exc:
                cleanup_errors.append(f"mesh:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if material_created and material is not None:
            try:
                bpy.data.materials.remove(material)
                material = None
            except Exception as cleanup_exc:
                cleanup_errors.append(f"material:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if image_created and image is not None:
            try:
                bpy.data.images.remove(image)
                image = None
            except Exception as cleanup_exc:
                cleanup_errors.append(f"image:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if cleanup_errors:
            raise _owned_raster_error(
                "image plane construction and owned-artifact cleanup failed: "
                f"creation={type(exc).__name__}:{exc}; cleanup={cleanup_errors!r}",
                owned_objects=(obj,),
                owned_datablocks=(mesh, material, image),
            ) from exc
        return None


# ── Mode mapping (BCS-ARCH-001) ───────────────────────────────────────

def _config_from_mode(mode_name: str) -> ImportConfig:
    """Map a BCS-ARCH-001 mode name to an ImportConfig instance.

    Valid modes: auto (default), vector, raster, hybrid.
    """
    key = (mode_name or "auto").strip().lower()
    if key == "auto":
        return ImportConfig.auto()
    if key == "vector":
        return ImportConfig.vector()
    if key == "raster":
        return ImportConfig.raster()
    if key == "hybrid":
        return ImportConfig.hybrid()
    raise ValueError(
        f"Unknown import mode: {mode_name!r}. "
        "Valid modes: auto, vector, raster, hybrid (BCS-ARCH-001)."
    )


def _apply_overrides(config: ImportConfig, ui_config: dict) -> ImportConfig:
    """Apply operator UI overrides onto an ImportConfig.

    BCS-ARCH-001 text rendering is orthogonal to mode. ``text_mode`` is
    one of ``labels | text | 3d_text | glyphs | geometry | raster``; the separate
    ``import_text`` toggle controls whether text is imported at all.
    """
    if "import_text" in ui_config:
        config.import_text = bool(ui_config["import_text"])
    if "text_mode" in ui_config:
        text_mode = str(ui_config["text_mode"] or "3d_text").strip().lower()
        config.text_mode = text_mode
        config.strict_text_fidelity = True
    if "strict_text_fidelity" in ui_config:
        if not bool(ui_config["strict_text_fidelity"]):
            raise ValueError("strict_text_fidelity cannot be disabled")
        config.strict_text_fidelity = True
    if "ignore_images" in ui_config:
        config.ignore_images = bool(ui_config["ignore_images"])
    if "detect_arcs" in ui_config:
        config.detect_arcs = ui_config["detect_arcs"]
    if "make_faces" in ui_config:
        config.make_faces = ui_config["make_faces"]
    if "group_by_color" in ui_config:
        config.group_by_color = ui_config["group_by_color"]
    if "map_dashes" in ui_config:
        config.map_dashes = ui_config["map_dashes"]
    if "model3d_mode" in ui_config:
        mode = str(ui_config["model3d_mode"] or "off").strip().lower()
        config.model3d_mode = mode if mode in {"off", "auto", "extrude"} else "off"
    if "model3d_depth_mm" in ui_config:
        try:
            config.model3d_depth_mm = max(0.01, float(ui_config["model3d_depth_mm"]))
        except (TypeError, ValueError):
            config.model3d_depth_mm = 3.175
    return config


# ── Page range parsing ───────────────────────────────────────────────

def _parse_pages(page_str: str, total_pages: int) -> List[int]:
    """
    Parse a page range string into a list of 0-based page indices.

    Supports: 'all', '1', '1,3-5', '2-4'
    Input is 1-based (user-facing). Output is 0-based (internal).
    """
    page_str = (page_str or "all").strip().lower()

    if page_str in ("all", "", "*"):
        return list(range(total_pages))

    pages = set()
    for part in page_str.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            lo = max(1, int(m.group(1)))
            hi = min(total_pages, int(m.group(2)))
            for p in range(lo, hi + 1):
                pages.add(p - 1)
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < total_pages:
                pages.add(idx)

    return sorted(pages) if pages else list(range(total_pages))


def _normalize_page_arrangement(raw: str | None) -> str:
    key = (raw or "spread").strip().lower()
    if key in {"spread", "compact", "touch", "overlay"}:
        return key
    return "spread"


def _normalize_page_gap_ratio(raw) -> float:
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        ratio = 0.20
    return max(0.0, min(1.0, ratio))


def _page_stack_step(page_height_m: float, arrangement: str, gap_ratio: float) -> float:
    h = max(0.001, float(page_height_m or 0.0))
    if arrangement == "overlay":
        return 0.0
    if arrangement == "touch":
        return h
    if arrangement == "compact":
        return h * (1.0 + gap_ratio)
    return h * 1.2


def _stack_page_objects(objects, stack_offset_m: float) -> int:
    """Move each page hierarchy once, leaving child-local transforms intact."""
    page_objects = []
    seen = set()
    for obj in tuple(objects or ()):
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        page_objects.append(obj)
    page_object_ids = {id(obj) for obj in page_objects}
    moved = 0
    for obj in page_objects:
        try:
            parent = obj.parent
        except (AttributeError, ReferenceError):
            parent = None
        if parent is not None and id(parent) in page_object_ids:
            continue
        try:
            location = obj.location
            try:
                location.y += float(stack_offset_m)
            except AttributeError:
                location[1] = float(location[1]) + float(stack_offset_m)
            moved += 1
        except (AttributeError, IndexError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue
    return moved


def _object_world_location(obj):
    """Return evaluated world translation, falling back for host-test doubles."""
    try:
        translation = obj.matrix_world.translation
        return [float(translation[index]) for index in range(3)]
    except (AttributeError, IndexError, ReferenceError, RuntimeError, TypeError, ValueError):
        return [float(obj.location[index]) for index in range(3)]


def _delivery_expected_locations(attempt_evidence, entity_ids):
    expected = {}
    for character in tuple(attempt_evidence.get("character_entities") or ()):
        verification = dict(character.get("verification") or {})
        location = verification.get("actual_location_m")
        if not isinstance(location, (list, tuple)) or len(location) < 2:
            continue
        for entity_id in tuple(character.get("entity_ids") or ()):
            expected[str(entity_id)] = (float(location[0]), float(location[1]))
    top_location = attempt_evidence.get("actual_location_m")
    if isinstance(top_location, (list, tuple)) and len(top_location) >= 2 and entity_ids:
        expected.setdefault(
            str(entity_ids[0]),
            (float(top_location[0]), float(top_location[1])),
        )
    return expected


def _expectation_mapping(entries, entity_ids):
    """Require one and only one expectation for every final physical handle."""

    entity_ids = [str(value) for value in tuple(entity_ids or ())]
    grouped = {entity_id: [] for entity_id in entity_ids}
    failures = []
    for entry in tuple(entries or ()):
        if not isinstance(entry, dict):
            failures.append("final_entity_expectation_entry_invalid")
            continue
        entity_id = entry.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            failures.append("final_entity_expectation_handle_invalid")
            continue
        if entity_id not in grouped:
            failures.append(f"final_entity_expectation_unexpected:{entity_id}")
            continue
        grouped[entity_id].append(entry)
    mapping = {}
    for entity_id in entity_ids:
        candidates = grouped[entity_id]
        if not candidates:
            failures.append(f"final_entity_expectation_missing:{entity_id}")
        elif len(candidates) != 1:
            failures.append(f"final_entity_expectation_ambiguous:{entity_id}")
        else:
            mapping[entity_id] = candidates[0]
    return mapping, failures


def _delivery_entity_expectations(attempt_evidence, entity_ids, representation):
    entries = final_entity_expectations_from_evidence(
        attempt_evidence,
        entity_ids,
        representation,
    )
    mapping, failures = _expectation_mapping(entries, entity_ids)
    return mapping, entries, failures


def _reject_nonfinite_json_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def _sealed_entity_expectations(
    provenance_opts,
    record,
    *,
    item_id: str,
    page_number: int,
    source_span_id: int,
    requested_representation: str,
    representation: str,
    entity_ids,
):
    """Open immutable delivery-time expectations and validate every binding."""

    failures = []
    authorities = getattr(
        provenance_opts,
        "_final_entity_expectation_authorities",
        None,
    )
    authority = authorities.get(item_id) if isinstance(authorities, dict) else None
    if not isinstance(authority, FinalEntityExpectationAuthority):
        return {}, [], ["final_entity_expectation_authority_missing"]
    payload = str(authority.manifest_json or "")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != authority.manifest_sha256:
        failures.append("final_entity_expectation_authority_digest_mismatch")
    if str(authority.item_id or "") != item_id:
        failures.append("final_entity_expectation_authority_item_mismatch")
    if record.get("final_entity_expectation_sha256") != authority.manifest_sha256:
        failures.append("final_entity_expectation_record_digest_mismatch")
    try:
        manifest = json.loads(
            payload,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, [], [*failures, "final_entity_expectation_authority_invalid"]
    if not isinstance(manifest, dict):
        return {}, [], [*failures, "final_entity_expectation_authority_invalid"]
    if manifest.get("schema") != "blender_final_entity_expectations_v1":
        failures.append("final_entity_expectation_schema_mismatch")
    if manifest.get("importer_id") != "bc_pdf_vector_importer.blender":
        failures.append("final_entity_expectation_importer_mismatch")
    if str(manifest.get("item_id") or "") != item_id:
        failures.append("final_entity_expectation_item_mismatch")
    if _strict_manifest_int(manifest.get("page_number")) != int(page_number):
        failures.append("final_entity_expectation_page_mismatch")
    if _strict_manifest_int(manifest.get("source_span_id")) != int(source_span_id):
        failures.append("final_entity_expectation_span_mismatch")
    if manifest.get("requested_representation") != requested_representation:
        failures.append("final_entity_expectation_requested_mode_mismatch")
    if manifest.get("delivered_representation") != representation:
        failures.append("final_entity_expectation_delivered_mode_mismatch")
    sealed_ids = manifest.get("entity_ids")
    expected_ids = [str(value) for value in tuple(entity_ids or ())]
    if sealed_ids != expected_ids:
        failures.append("final_entity_expectation_entity_ids_mismatch")
    entries = manifest.get("expectations")
    if not isinstance(entries, list):
        entries = []
        failures.append("final_entity_expectation_entries_invalid")
    mapping, mapping_failures = _expectation_mapping(entries, expected_ids)
    failures.extend(mapping_failures)
    return mapping, entries, failures


def _finite_matrix_values(value):
    try:
        values = [float(part) for part in value]
    except (TypeError, ValueError):
        return None
    if len(values) != 16 or not all(math.isfinite(part) for part in values):
        return None
    return values


def _evaluated_matrix_values(obj):
    if getattr(bpy, "_bc_pdf_vector_importer_test_host", False) is True:
        test_values = getattr(obj, "_pdf_test_evaluated_matrix_values", None)
        values = _finite_matrix_values(test_values)
        if values is not None:
            return values
    try:
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        values = [float(part) for row in evaluated.matrix_world for part in row]
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None
    return values if len(values) == 16 and all(math.isfinite(v) for v in values) else None


def _bounds_from_world_points(points):
    try:
        finite_points = [
            (float(point[0]), float(point[1]))
            for point in tuple(points or ())
        ]
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    if not finite_points or not all(
        math.isfinite(value) for point in finite_points for value in point
    ):
        return None
    xs = [point[0] for point in finite_points]
    ys = [point[1] for point in finite_points]
    return (min(xs), min(ys), max(xs), max(ys))


def _live_world_ink_measurement(obj):
    """Measure evaluated physical ink; production paths never assume success."""

    if getattr(bpy, "_bc_pdf_vector_importer_test_host", False) is True:
        test_points = getattr(obj, "_pdf_test_world_ink_points", None)
        if isinstance(test_points, (list, tuple)):
            if not test_points:
                return 0, None, "explicit_test_host_points"
            bounds = _bounds_from_world_points(test_points)
            if bounds is None:
                return None, None, "explicit_test_host_points_invalid"
            return len(test_points), bounds, "explicit_test_host_points"

    evaluated = None
    mesh = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        to_mesh = getattr(evaluated, "to_mesh", None)
        if not callable(to_mesh):
            return None, None, "evaluated_to_mesh_unavailable"
        try:
            mesh = to_mesh(
                preserve_all_data_layers=False,
                depsgraph=depsgraph,
            )
        except TypeError:
            mesh = to_mesh()
        vertices = tuple(getattr(mesh, "vertices", ()) or ())
        if not vertices:
            return 0, None, "evaluated_mesh_vertices"
        matrix = evaluated.matrix_world
        points = [matrix @ vertex.co for vertex in vertices]
        bounds = _bounds_from_world_points(points)
        if bounds is None:
            return None, None, "evaluated_mesh_bounds_invalid"
        return len(vertices), bounds, "evaluated_mesh_vertices"
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None, None, "evaluated_mesh_measurement_failed"
    finally:
        try:
            clear = getattr(evaluated, "to_mesh_clear", None)
            if mesh is not None and callable(clear):
                clear()
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def _object_property(obj, name, default=None):
    try:
        return obj.get(name, default)
    except (AttributeError, ReferenceError, TypeError):
        return default


def _finite_pair(value):
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    return result if len(result) == 2 and all(math.isfinite(v) for v in result) else None


def _finite_bounds(value):
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if (
        len(result) != 4
        or not all(math.isfinite(v) for v in result)
        or result[0] > result[2]
        or result[1] > result[3]
    ):
        return None
    return result


def _finite_coordinate_rows(value, width: int):
    if not isinstance(value, list):
        return None
    rows = []
    for raw_row in value:
        if not isinstance(raw_row, list):
            return None
        try:
            row = [float(part) for part in raw_row]
        except (TypeError, ValueError):
            return None
        if len(row) != width or not all(math.isfinite(part) for part in row):
            return None
        rows.append(row)
    return rows


def _coordinate_rows_match(expected, actual, width: int) -> bool:
    expected_rows = _finite_coordinate_rows(expected, width)
    actual_rows = _finite_coordinate_rows(actual, width)
    return bool(
        expected_rows is not None
        and actual_rows is not None
        and len(expected_rows) == len(actual_rows)
        and all(
            abs(left - right) <= 1e-12
            for expected_row, actual_row in zip(  # noqa: B905
                expected_rows,
                actual_rows,
            )
            for left, right in zip(expected_row, actual_row)  # noqa: B905
        )
    )


def _raster_mesh_state_matches(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False, False
    geometry_matches = bool(
        _coordinate_rows_match(
            expected.get("vertices_local"),
            actual.get("vertices_local"),
            3,
        )
        and expected.get("loops_vertex_indices")
        == actual.get("loops_vertex_indices")
        and expected.get("polygons") == actual.get("polygons")
    )
    uv_matches = bool(
        expected.get("uv_layer_name") == "UVMap"
        and actual.get("uv_layer_name") == "UVMap"
        and _coordinate_rows_match(
            expected.get("uv_coordinates"),
            actual.get("uv_coordinates"),
            2,
        )
    )
    return geometry_matches, uv_matches


def _same_host_identity(left, right) -> bool:
    if left is right:
        return True
    try:
        if left == right:
            return True
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        pass
    try:
        left_pointer = int(left.as_pointer())
        right_pointer = int(right.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
    return left_pointer != 0 and left_pointer == right_pointer


def _host_node_socket(node, collection_name: str, socket_name: str):
    try:
        sockets = getattr(node, collection_name)
        getter = getattr(sockets, "get", None)
        socket = getter(socket_name) if callable(getter) else sockets[socket_name]
    except (
        AttributeError,
        IndexError,
        KeyError,
        ReferenceError,
        RuntimeError,
        TypeError,
    ):
        return None
    return socket


def _exact_host_node_link(
    links,
    *,
    from_node,
    from_socket,
    to_node,
    to_socket,
) -> bool:
    if from_socket is None or to_socket is None:
        return False
    try:
        return any(
            _same_host_identity(getattr(link, "from_node", None), from_node)
            and _same_host_identity(getattr(link, "to_node", None), to_node)
            and _same_host_identity(getattr(link, "from_socket", None), from_socket)
            and _same_host_identity(getattr(link, "to_socket", None), to_socket)
            for link in links
        )
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def _finite_rgba(value):
    try:
        rgba = tuple(float(part) for part in value)
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        return None
    if len(rgba) != 4 or not all(math.isfinite(part) for part in rgba):
        return None
    return rgba


def _text_material_binding_verified(
    obj,
    *,
    material_name: str,
    expected_rgba,
) -> bool:
    """Prove exact assignment, color, alpha, and active Surface shader routing."""

    expected = _finite_rgba(expected_rgba)
    if not material_name or expected is None:
        return False
    try:
        material = bpy.data.materials.get(material_name)
        assigned_materials = tuple(obj.data.materials)
        actual = _finite_rgba(material.diffuse_color)
        if (
            material is None
            or not bool(material.use_nodes)
            or actual is None
            or not any(
                _same_host_identity(candidate, material)
                for candidate in assigned_materials
            )
            or any(
                abs(left - right) > 1e-6
                for left, right in zip(actual, expected)  # noqa: B905
            )
        ):
            return False
        nodes = tuple(material.node_tree.nodes)
        links = tuple(material.node_tree.links)
    except (
        AttributeError,
        IndexError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False
    shaders = [
        node
        for node in nodes
        if str(getattr(node, "type", "") or "") == "BSDF_PRINCIPLED"
    ]
    outputs = [
        node
        for node in nodes
        if str(getattr(node, "type", "") or "") == "OUTPUT_MATERIAL"
    ]
    try:
        for shader in shaders:
            base_color = _finite_rgba(
                _host_node_socket(shader, "inputs", "Base Color").default_value
            )
            shader_alpha = float(
                _host_node_socket(shader, "inputs", "Alpha").default_value
            )
            shader_bsdf = _host_node_socket(shader, "outputs", "BSDF")
            if (
                base_color is None
                or not math.isfinite(shader_alpha)
                or any(
                    abs(left - right) > 1e-6
                    for left, right in zip(base_color, expected)  # noqa: B905
                )
                or abs(shader_alpha - expected[3]) > 1e-6
            ):
                continue
            for output in outputs:
                if not bool(getattr(output, "is_active_output", True)):
                    continue
                output_surface = _host_node_socket(output, "inputs", "Surface")
                if _exact_host_node_link(
                    links,
                    from_node=shader,
                    from_socket=shader_bsdf,
                    to_node=output,
                    to_socket=output_surface,
                ):
                    return True
    except (
        AttributeError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False
    return False


def _raster_material_binding_verified(
    obj,
    *,
    material_name: str,
    image_name: str,
) -> bool:
    """Prove the final mesh renders the expected packed image through one chain."""

    try:
        material = bpy.data.materials.get(material_name)
        image = bpy.data.images.get(image_name)
        assigned_materials = list(obj.data.materials)
        polygons = tuple(obj.data.polygons)
        if (
            material is None
            or image is None
            or not bool(material.use_nodes)
            or not any(
                _same_host_identity(candidate, material)
                for candidate in assigned_materials
            )
            or not polygons
        ):
            return False
        for polygon in polygons:
            material_index = int(polygon.material_index)
            if (
                material_index < 0
                or material_index >= len(assigned_materials)
                or not _same_host_identity(
                    assigned_materials[material_index],
                    material,
                )
            ):
                return False
        nodes = tuple(material.node_tree.nodes)
        links = tuple(material.node_tree.links)
    except (
        AttributeError,
        IndexError,
        ReferenceError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False
    bound_textures = [
        node
        for node in nodes
        if str(getattr(node, "type", "") or "") == "TEX_IMAGE"
        and _same_host_identity(getattr(node, "image", None), image)
    ]
    shaders = [
        node
        for node in nodes
        if str(getattr(node, "type", "") or "") == "BSDF_PRINCIPLED"
    ]
    outputs = [
        node
        for node in nodes
        if str(getattr(node, "type", "") or "") == "OUTPUT_MATERIAL"
    ]
    for texture in bound_textures:
        texture_color = _host_node_socket(texture, "outputs", "Color")
        texture_alpha = _host_node_socket(texture, "outputs", "Alpha")
        for shader in shaders:
            shader_base_color = _host_node_socket(shader, "inputs", "Base Color")
            shader_alpha = _host_node_socket(shader, "inputs", "Alpha")
            shader_bsdf = _host_node_socket(shader, "outputs", "BSDF")
            if not (
                _exact_host_node_link(
                    links,
                    from_node=texture,
                    from_socket=texture_color,
                    to_node=shader,
                    to_socket=shader_base_color,
                )
                and _exact_host_node_link(
                    links,
                    from_node=texture,
                    from_socket=texture_alpha,
                    to_node=shader,
                    to_socket=shader_alpha,
                )
            ):
                continue
            for output in outputs:
                if not bool(getattr(output, "is_active_output", True)):
                    continue
                output_surface = _host_node_socket(output, "inputs", "Surface")
                if _exact_host_node_link(
                    links,
                    from_node=shader,
                    from_socket=shader_bsdf,
                    to_node=output,
                    to_socket=output_surface,
                ):
                    return True
    return False


def _strict_manifest_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _live_entity_continuity_failures(
    obj,
    *,
    entity_id: str,
    item_id: str,
    source_span_id: int,
    requested_representation: str,
    representation: str,
    expectation,
    stack_offset_m: float,
):
    """Fail-closed final-object verification against one sealed expectation."""

    failures = []
    proof = {
        "object_handle_verified": False,
        "source_item_verified": False,
        "character_identity_verified": False,
        "representation_fields_verified": False,
        "text_material_binding_verified": None,
        "affine_verified": False,
        "physical_ink_continuity_verified": False,
    }
    proof["object_handle_verified"] = (
        str(getattr(obj, "name", "") or "") == entity_id
    )
    if not proof["object_handle_verified"]:
        failures.append(f"final_entity_handle_mismatch:{entity_id}")
    source_item_matches = (
        str(_object_property(obj, "pdf_source_item_id", "") or "") == item_id
    )
    source_span_matches = (
        _strict_manifest_int(_object_property(obj, "pdf_source_span_id"))
        == int(source_span_id)
    )
    proof["source_item_verified"] = source_item_matches and source_span_matches
    if not source_item_matches:
        failures.append(f"final_entity_source_item_mismatch:{entity_id}")
    if not source_span_matches:
        failures.append(f"final_entity_source_span_mismatch:{entity_id}")

    expected = dict(expectation or {})
    if not expected:
        failures.append(f"final_entity_expectation_missing:{entity_id}")
        return proof, failures
    kind = str(expected.get("kind") or "")
    proof["expectation_kind"] = kind
    if kind not in {"character", "span", "raster"}:
        failures.append(f"final_entity_expectation_kind_invalid:{entity_id}")
        return proof, failures

    actual_type = str(getattr(obj, "type", "") or "")
    expected_type = {
        "labels": "FONT",
        "text": "FONT",
        "3d_text": "FONT",
        "glyphs": "CURVE",
        "geometry": "MESH",
        "raster": "MESH",
    }.get(representation)
    representation_ok = bool(
        expected_type is not None
        and actual_type == expected_type
        and str(_object_property(obj, "pdf_text_mode", "") or "")
        == representation
        and str(_object_property(obj, "pdf_text_requested_mode", "") or "")
        == requested_representation
    )
    if actual_type != expected_type:
        failures.append(f"final_entity_type_mismatch:{entity_id}")
    if (
        str(_object_property(obj, "pdf_text_mode", "") or "") != representation
        or str(_object_property(obj, "pdf_text_requested_mode", "") or "")
        != requested_representation
    ):
        failures.append(f"final_entity_representation_mismatch:{entity_id}")

    if kind == "character":
        character_ok = True
        expected_index = _strict_manifest_int(expected.get("character_index"))
        if expected_index is None:
            failures.append(f"final_entity_character_expectation_invalid:{entity_id}")
            character_ok = False
        elif _strict_manifest_int(
            _object_property(obj, "pdf_source_char_index")
        ) != expected_index:
            failures.append(f"final_entity_character_index_mismatch:{entity_id}")
            character_ok = False
        for expected_key, property_name, failure_name in (
            ("glyph_id", "pdf_source_glyph_id", "final_entity_character_glyph_mismatch"),
            (
                "physical_glyph_id",
                "pdf_physical_glyph_id",
                "final_entity_physical_glyph_mismatch",
            ),
        ):
            if expected_key not in expected:
                failures.append(f"final_entity_character_expectation_invalid:{entity_id}")
                character_ok = False
                continue
            raw_value = expected.get(expected_key)
            expected_value = -1 if raw_value is None else _strict_manifest_int(raw_value)
            if (
                expected_value is None
                or _strict_manifest_int(_object_property(obj, property_name))
                != expected_value
            ):
                failures.append(f"{failure_name}:{entity_id}")
                character_ok = False
        proof["character_identity_verified"] = character_ok
    else:
        proof["character_identity_verified"] = None

    if kind in {"character", "span"}:
        expected_text = expected.get("text")
        if not isinstance(expected_text, str) or not expected_text:
            failures.append(f"final_entity_text_expectation_invalid:{entity_id}")
            representation_ok = False
        else:
            actual_text = (
                str(getattr(getattr(obj, "data", None), "body", ""))
                if actual_type == "FONT"
                else str(_object_property(obj, "pdf_text_source", "") or "")
            )
            if actual_text != expected_text:
                failures.append(f"final_entity_body_mismatch:{entity_id}")
                representation_ok = False
        expected_font_sha = str(
            expected.get("packed_font_sha256")
            or expected.get("source_ink_font_sha256")
            or ""
        )
        if re.fullmatch(r"[0-9a-f]{64}", expected_font_sha) is None:
            failures.append(f"final_entity_font_expectation_invalid:{entity_id}")
            representation_ok = False
        else:
            font_ok = (
                str(_object_property(obj, "pdf_exact_font_sha256", "") or "")
                == expected_font_sha
            )
            if actual_type == "FONT":
                try:
                    font_ok = bool(
                        font_ok
                        and verify_packed_sha256(obj.data.font, expected_font_sha)
                        == expected_font_sha
                    )
                except (AttributeError, PackedAssetError, ReferenceError, TypeError):
                    font_ok = False
            if not font_ok:
                failures.append(f"final_entity_font_digest_mismatch:{entity_id}")
                representation_ok = False
        if actual_type == "FONT":
            try:
                actual_extrusion = float(obj.data.extrude)
                expected_extrusion = float(expected.get("extrusion_m"))
            except (AttributeError, ReferenceError, TypeError, ValueError):
                actual_extrusion = expected_extrusion = math.nan
            if (
                not math.isfinite(actual_extrusion)
                or not math.isfinite(expected_extrusion)
                or abs(actual_extrusion - expected_extrusion) > 1e-12
            ):
                failures.append(f"final_entity_extrusion_mismatch:{entity_id}")
                representation_ok = False
        material_binding_verified = _text_material_binding_verified(
            obj,
            material_name=str(expected.get("material_name") or ""),
            expected_rgba=expected.get("expected_rgba"),
        )
        proof["text_material_binding_verified"] = material_binding_verified
        if not material_binding_verified:
            failures.append(
                f"final_entity_text_material_binding_mismatch:{entity_id}"
            )
            representation_ok = False

    topology_field = {
        "CURVE": ("spline_count", "splines"),
        "MESH": ("vertex_count", "vertices"),
    }.get(actual_type)
    if topology_field is not None:
        expected_count = _strict_manifest_int(expected.get(topology_field[0]))
        try:
            topology_count = len(tuple(getattr(obj.data, topology_field[1])))
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            topology_count = None
        if expected_count is None or topology_count != expected_count:
            failures.append(f"final_entity_topology_mismatch:{entity_id}")
            representation_ok = False

    if kind == "character":
        expected_matrix = _finite_matrix_values(expected.get("intended_affine_matrix"))
        affine_ok = expected_matrix is not None
        if expected_matrix is None:
            failures.append(f"final_entity_affine_expectation_invalid:{entity_id}")
        else:
            metadata_matrix = _finite_matrix_values(
                _object_property(obj, "pdf_affine_matrix", ())
            )
            metadata_tolerance = max(
                1e-9,
                max(abs(value) for value in expected_matrix) * 2e-7,
            )
            if metadata_matrix is None or any(
                abs(actual - canonical) > metadata_tolerance
                for actual, canonical in zip(  # noqa: B905
                    metadata_matrix,
                    expected_matrix,
                )
            ):
                failures.append(f"final_entity_affine_metadata_mismatch:{entity_id}")
                affine_ok = False
            evaluated_matrix = _evaluated_matrix_values(obj)
            if evaluated_matrix is None:
                failures.append(f"final_entity_affine_unverifiable:{entity_id}")
                affine_ok = False
            else:
                stacked_matrix = list(expected_matrix)
                stacked_matrix[7] += float(stack_offset_m)
                tolerance = max(
                    1e-6,
                    max(abs(value) for value in stacked_matrix) * 2e-7,
                )
                if any(
                    abs(actual - canonical) > tolerance
                    for actual, canonical in zip(  # noqa: B905
                        evaluated_matrix,
                        stacked_matrix,
                    )
                ):
                    failures.append(f"final_entity_affine_mismatch:{entity_id}")
                    affine_ok = False
        proof["affine_verified"] = affine_ok
    else:
        expected_matrix = _finite_matrix_values(
            expected.get("evaluated_world_affine_matrix")
        )
        affine_ok = expected_matrix is not None
        if expected_matrix is None:
            failures.append(f"final_entity_affine_expectation_invalid:{entity_id}")
        else:
            evaluated_matrix = _evaluated_matrix_values(obj)
            if evaluated_matrix is None:
                failures.append(f"final_entity_affine_unverifiable:{entity_id}")
                affine_ok = False
            else:
                stacked_matrix = list(expected_matrix)
                stacked_matrix[7] += float(stack_offset_m)
                tolerance = max(
                    1e-6,
                    max(abs(value) for value in stacked_matrix) * 2e-7,
                )
                if any(
                    abs(actual - canonical) > tolerance
                    for actual, canonical in zip(  # noqa: B905
                        evaluated_matrix,
                        stacked_matrix,
                    )
                ):
                    failures.append(f"final_entity_affine_mismatch:{entity_id}")
                    affine_ok = False
        proof["affine_verified"] = affine_ok

    ink_count, actual_bounds, measurement = _live_world_ink_measurement(obj)
    proof["live_ink_element_count"] = ink_count
    proof["live_ink_measurement"] = measurement
    ink_ok = ink_count is not None
    if ink_count is None:
        failures.append(f"final_entity_physical_ink_unverifiable:{entity_id}")
    if kind == "character":
        expected_zero_ink = expected.get("zero_ink_identity")
        metadata_zero_ink = _object_property(
            obj,
            "pdf_metric_zero_ink_identity",
            None,
        )
        if not isinstance(expected_zero_ink, bool):
            failures.append(f"final_entity_ink_expectation_invalid:{entity_id}")
            ink_ok = False
        elif (
            not isinstance(metadata_zero_ink, bool)
            or metadata_zero_ink is not expected_zero_ink
            or (
                ink_count is not None
                and ((ink_count == 0) is not expected_zero_ink)
            )
        ):
            failures.append(f"final_entity_physical_ink_mismatch:{entity_id}")
            ink_ok = False
        expected_bounds = _finite_bounds(expected.get("expected_world_ink_bounds_m"))
        if expected_zero_ink is True:
            if expected.get("expected_world_ink_bounds_m") is not None:
                failures.append(f"final_entity_ink_expectation_invalid:{entity_id}")
                ink_ok = False
            if actual_bounds is not None:
                failures.append(f"final_entity_physical_ink_mismatch:{entity_id}")
                ink_ok = False
        elif expected_bounds is None:
            failures.append(f"final_entity_ink_bounds_expectation_invalid:{entity_id}")
            ink_ok = False
        elif actual_bounds is None:
            failures.append(f"final_entity_ink_bounds_unverifiable:{entity_id}")
            ink_ok = False
        else:
            stacked_bounds = list(expected_bounds)
            stacked_bounds[1] += float(stack_offset_m)
            stacked_bounds[3] += float(stack_offset_m)
            proof["expected_world_ink_bounds_m"] = stacked_bounds
            proof["actual_world_ink_bounds_m"] = list(actual_bounds)
            if any(
                abs(actual - canonical) > 6e-5
                for actual, canonical in zip(  # noqa: B905
                    actual_bounds,
                    stacked_bounds,
                )
            ):
                failures.append(f"final_entity_ink_bounds_mismatch:{entity_id}")
                ink_ok = False
    elif kind == "raster":
        expected_bbox = _finite_bounds(expected.get("source_bbox_pdf"))
        expected_sha = str(expected.get("packed_image_sha256") or "")
        image_name = str(expected.get("image_datablock") or "")
        material_name = str(expected.get("material_name") or "")
        live_mesh_state = raster_mesh_fidelity_state(obj)
        geometry_matches, uv_matches = _raster_mesh_state_matches(
            expected.get("mesh_state"),
            live_mesh_state,
        )
        material_binding_verified = _raster_material_binding_verified(
            obj,
            material_name=material_name,
            image_name=image_name,
        )
        proof["raster_geometry_verified"] = geometry_matches
        proof["raster_uv_verified"] = uv_matches
        proof["raster_material_binding_verified"] = material_binding_verified
        if not geometry_matches:
            failures.append(f"final_entity_raster_geometry_mismatch:{entity_id}")
        if not uv_matches:
            failures.append(f"final_entity_raster_uv_mismatch:{entity_id}")
        if not material_binding_verified:
            failures.append(
                f"final_entity_raster_material_binding_mismatch:{entity_id}"
            )
        raster_ok = bool(
            expected_bbox is not None
            and _finite_bounds(
                _object_property(obj, "pdf_raster_source_bbox_pdf", ())
            )
            == expected_bbox
            and re.fullmatch(r"[0-9a-f]{64}", expected_sha)
            and str(_object_property(obj, "pdf_image_sha256", "") or "")
            == expected_sha
            and str(_object_property(obj, "pdf_image_datablock", "") or "")
            == image_name
            and ink_count is not None
            and ink_count > 0
            and geometry_matches
            and uv_matches
            and material_binding_verified
        )
        try:
            image = bpy.data.images.get(image_name)
            raster_ok = bool(
                raster_ok
                and verify_packed_sha256(image, expected_sha) == expected_sha
            )
        except (
            AttributeError,
            PackedAssetError,
            ReferenceError,
            RuntimeError,
            TypeError,
        ):
            raster_ok = False
        if not raster_ok:
            failures.append(f"final_entity_raster_continuity_mismatch:{entity_id}")
        ink_ok = bool(ink_ok and raster_ok)
    elif ink_count is not None and ink_count <= 0:
        failures.append(f"final_entity_physical_ink_mismatch:{entity_id}")
        ink_ok = False

    if kind in {"span", "raster"}:
        expected_dimensions = _finite_pair(expected.get("expected_dimensions_m"))
        try:
            actual_dimensions = (
                abs(float(obj.dimensions[0])),
                abs(float(obj.dimensions[1])),
            )
        except (
            AttributeError,
            IndexError,
            ReferenceError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            actual_dimensions = None
        if expected_dimensions is None:
            failures.append(f"final_entity_dimensions_expectation_invalid:{entity_id}")
            representation_ok = False
        elif actual_dimensions is None or any(
            abs(actual - canonical) > max(1e-7, abs(canonical) * 1e-3)
            for actual, canonical in zip(  # noqa: B905
                actual_dimensions or (),
                expected_dimensions,
            )
        ):
            failures.append(f"final_entity_dimensions_mismatch:{entity_id}")
            representation_ok = False

    proof["representation_fields_verified"] = representation_ok
    proof["physical_ink_continuity_verified"] = ink_ok
    return proof, list(dict.fromkeys(failures))


def _evidence_has_positioned_zero_ink(evidence) -> bool:
    """Return whether delivery evidence objectively includes positioned zero ink."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("proof_kind") == "positioned_zero_ink_delivery_v1":
        return True
    zero_count = _strict_manifest_int(evidence.get("zero_ink_character_count"))
    if zero_count is not None and zero_count > 0:
        return True
    characters = evidence.get("character_entities")
    return bool(
        isinstance(characters, (list, tuple))
        and any(
            isinstance(character, dict)
            and isinstance(character.get("verification"), dict)
            and character["verification"].get("zero_ink_identity") is True
            for character in characters
        )
    )


def _canonical_zero_ink_delivery_manifest(
    provenance_opts,
    *,
    authority,
    item_id: str,
    page_number: int,
):
    """Return sealed delivery/source truth; mutable maps are tamper detectors only."""
    failures = []
    if not isinstance(authority, ZeroInkReconciliationAuthority):
        return None, None, False, ["zero_ink_reconciliation_authority_missing"]
    try:
        source_manifest, manifest = open_zero_ink_reconciliation_authority(
            authority
        )
    except (TypeError, ValueError, OverflowError):
        return None, None, False, ["zero_ink_reconciliation_authority_invalid"]

    source_detector_valid = False
    source_manifests = getattr(
        provenance_opts,
        "_zero_ink_source_manifests",
        None,
    )
    mutable_source = (
        source_manifests.get(item_id)
        if isinstance(source_manifests, dict)
        else None
    )
    if not isinstance(mutable_source, dict):
        failures.append("zero_ink_source_manifest_missing")
    else:
        try:
            mutable_source_snapshot, mutable_source_digest = (
                freeze_zero_ink_source_manifest(mutable_source)
            )
        except (TypeError, ValueError, OverflowError):
            failures.append("zero_ink_source_manifest_invalid")
        else:
            if (
                mutable_source_snapshot != source_manifest
                or mutable_source_digest != authority.source_manifest_sha256
            ):
                failures.append("zero_ink_source_manifest_digest_mismatch")
                failures.append(
                    "zero_ink_character_source_manifest_digest_mismatch"
                )
            else:
                source_detector_valid = True

    delivery_detector_valid = False
    delivery_manifests = getattr(
        provenance_opts,
        "_zero_ink_delivery_manifests",
        None,
    )
    entry = (
        delivery_manifests.get(item_id)
        if isinstance(delivery_manifests, dict)
        else None
    )
    if not isinstance(entry, dict):
        failures.append("zero_ink_delivery_manifest_missing")
    else:
        try:
            mutable_delivery, mutable_delivery_digest = (
                freeze_zero_ink_source_manifest(entry.get("manifest"))
            )
        except (TypeError, ValueError, OverflowError):
            failures.append("zero_ink_delivery_manifest_invalid")
        else:
            if (
                mutable_delivery != manifest
                or mutable_delivery_digest != entry.get("sha256")
                or mutable_delivery_digest
                != authority.delivery_manifest_sha256
            ):
                failures.append("zero_ink_delivery_manifest_digest_mismatch")
            else:
                delivery_detector_valid = True
    if manifest.get("schema") != ZERO_INK_DELIVERY_MANIFEST_SCHEMA:
        failures.append("zero_ink_delivery_manifest_schema_unverified")
    if manifest.get("importer_id") != "bc_pdf_vector_importer.blender":
        failures.append("zero_ink_delivery_manifest_importer_identity_unbound")
    if str(manifest.get("item_id") or "") != str(item_id):
        failures.append("zero_ink_delivery_manifest_item_identity_unbound")
    if _strict_manifest_int(manifest.get("page_number")) != int(page_number):
        failures.append("zero_ink_delivery_manifest_page_identity_unbound")

    source_characters = (
        source_manifest.get("characters")
        if isinstance(source_manifest, dict)
        else None
    )
    if not isinstance(source_characters, list):
        source_characters = []
        failures.append("zero_ink_source_manifest_invalid")
    source_span_id = (
        _strict_manifest_int(source_manifest.get("source_span_id"))
        if isinstance(source_manifest, dict)
        else None
    )
    if (
        source_span_id is None
        or _strict_manifest_int(manifest.get("source_span_id")) != source_span_id
    ):
        failures.append("zero_ink_delivery_manifest_span_identity_unbound")
    source_requested = (
        source_manifest.get("requested_representation")
        if isinstance(source_manifest, dict)
        else None
    )
    if manifest.get("requested_representation") != source_requested:
        failures.append("zero_ink_delivery_manifest_requested_mode_unbound")
    delivered_representation = manifest.get("delivered_representation")
    if delivered_representation not in {"text", "3d_text", "glyphs", "geometry"}:
        failures.append("zero_ink_delivery_manifest_delivered_mode_invalid")

    entity_ids = manifest.get("entity_ids")
    if not isinstance(entity_ids, list):
        entity_ids = []
        failures.append("zero_ink_delivery_manifest_entity_identity_invalid")
    elif (
        any(not isinstance(value, str) or not value for value in entity_ids)
        or len(set(entity_ids)) != len(entity_ids)
    ):
        failures.append("zero_ink_delivery_manifest_entity_identity_invalid")
    if _strict_manifest_int(manifest.get("physical_entity_count")) != len(entity_ids):
        failures.append("zero_ink_delivery_manifest_physical_count_unbound")

    zero_count = sum(
        1
        for character in source_characters
        if isinstance(character, dict)
        and isinstance(character.get("text"), str)
        and source_character_is_zero_ink(character)
    )
    all_source_characters_zero_ink = bool(source_characters) and zero_count == len(
        source_characters
    )
    # Native Text/3D Text intentionally retain editable FONT entities even when
    # every character has no rendered ink. Converted CURVE/MESH delivery has no
    # truthful physical entity for an all-zero item and remains logical-only.
    logical_zero_ink = bool(
        all_source_characters_zero_ink
        and delivered_representation in {"glyphs", "geometry"}
    )
    if _strict_manifest_int(manifest.get("source_character_count")) != len(
        source_characters
    ):
        failures.append("zero_ink_delivery_manifest_source_count_unbound")
    if _strict_manifest_int(manifest.get("zero_ink_character_count")) != zero_count:
        failures.append("zero_ink_delivery_manifest_zero_count_unbound")
    if manifest.get("logical_zero_ink_delivery") is not logical_zero_ink:
        failures.append("zero_ink_delivery_manifest_logical_mode_unbound")
    if logical_zero_ink and entity_ids:
        failures.append("zero_ink_delivery_manifest_logical_entity_identity_present")
    if not logical_zero_ink and not entity_ids:
        failures.append("zero_ink_delivery_manifest_physical_entity_identity_missing")
    expected_contribution = 0 if logical_zero_ink else 1
    if (
        _strict_manifest_int(manifest.get("delivered_count_contribution"))
        != expected_contribution
    ):
        failures.append("zero_ink_delivery_manifest_count_contribution_unbound")

    try:
        _, source_digest = freeze_zero_ink_source_manifest(source_manifest)
    except (TypeError, ValueError, OverflowError):
        source_digest = None
    if (
        source_digest is None
        or manifest.get("source_manifest_sha256") != source_digest
    ):
        failures.append("zero_ink_delivery_manifest_source_crosslink_mismatch")
    return (
        manifest,
        source_manifest,
        source_detector_valid and delivery_detector_valid,
        list(dict.fromkeys(failures)),
    )


def _reverify_text_delivery_after_stack(
    delivery_records,
    *,
    page_number: int,
    stack_offset_m: float,
    provenance_opts=None,
):
    """Bind proof to final host state after every page-placement mutation."""
    failures = []
    expected_types = {
        "labels": "FONT",
        "text": "FONT",
        "3d_text": "FONT",
        "glyphs": "CURVE",
        "geometry": "MESH",
        "raster": "MESH",
    }
    try:
        bpy.context.view_layer.update()
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    registry_getter_failure = None
    try:
        registry = getattr(getattr(bpy, "data", None), "objects", None)
        getter = getattr(registry, "get", None)
    except Exception as exc:
        getter = None
        registry_getter_failure = type(exc).__name__
    for record in tuple(delivery_records or ()):
        if (
            int(record.get("page", 0) or 0) != int(page_number)
            or record.get("status") != "delivered"
        ):
            continue
        raw_record_entity_ids = record.get("entity_ids")
        record_entity_ids = [
            value
            for value in tuple(raw_record_entity_ids or ())
            if isinstance(value, str) and value
        ]
        record_entity_ids_exact = bool(
            isinstance(raw_record_entity_ids, (list, tuple))
            and len(record_entity_ids) == len(raw_record_entity_ids)
        )
        attempts = tuple(record.get("attempts") or ())
        delivered_attempt = next(
            (attempt for attempt in reversed(attempts) if attempt.get("status") == "delivered"),
            {},
        )
        prior_evidence = dict(delivered_attempt.get("evidence") or {})
        record_representation = str(record.get("final_representation") or "")
        record_requested_representation = str(
            record.get("requested_representation") or ""
        )
        entity_proofs = []
        record_failures = []
        item_id = str(record.get("item_id") or "")
        source_manifests = getattr(
            provenance_opts,
            "_zero_ink_source_manifests",
            None,
        )
        reconciliation_authorities = getattr(
            provenance_opts,
            "_zero_ink_reconciliation_authorities",
            (),
        )
        authority_matches = [
            authority
            for authority in (
                reconciliation_authorities
                if isinstance(reconciliation_authorities, tuple)
                else ()
            )
            if isinstance(authority, ZeroInkReconciliationAuthority)
            and authority.item_id == item_id
        ]
        authority = authority_matches[0] if len(authority_matches) == 1 else None
        if len(authority_matches) > 1:
            record_failures.append("zero_ink_reconciliation_authority_ambiguous")
        runtime_outcomes = getattr(
            provenance_opts,
            "_text_delivery_outcomes",
            None,
        )
        runtime_outcome = (
            runtime_outcomes.get(item_id)
            if isinstance(runtime_outcomes, dict)
            else None
        )
        runtime_evidence = (
            dict(runtime_outcome.evidence or {})
            if isinstance(runtime_outcome, AttemptOutcome)
            else {}
        )
        runtime_delivery_representation = str(
            runtime_evidence.get("delivered_representation") or ""
        )
        runtime_delivery_count_contribution = None
        if (
            isinstance(runtime_outcome, AttemptOutcome)
            and runtime_delivery_representation in expected_types
        ):
            raw_runtime_ids = tuple(runtime_outcome.entity_ids or ())
            runtime_ids = tuple(
                value
                for value in raw_runtime_ids
                if isinstance(value, str) and value
            )
            if len(runtime_ids) == len(raw_runtime_ids):
                if runtime_outcome.entity is not None and runtime_ids:
                    runtime_delivery_count_contribution = 1
                elif (
                    runtime_outcome.entity is None
                    and not runtime_ids
                    and runtime_evidence.get("proof_kind")
                    == "positioned_zero_ink_delivery_v1"
                ):
                    runtime_delivery_count_contribution = 0
        delivery_manifests = getattr(
            provenance_opts,
            "_zero_ink_delivery_manifests",
            None,
        )
        canonical_state_present = bool(
            authority is not None
            or (
                isinstance(source_manifests, dict)
                and item_id in source_manifests
            )
            or (
                isinstance(delivery_manifests, dict)
                and item_id in delivery_manifests
            )
            or record.get("zero_ink_delivery") is True
            or (
                isinstance(record.get("zero_ink_character_count"), int)
                and not isinstance(record.get("zero_ink_character_count"), bool)
                and record.get("zero_ink_character_count") > 0
            )
            or bool(str(record.get("zero_ink_delivery_manifest_sha256") or ""))
            or bool(str(record.get("source_manifest_sha256") or ""))
            or _evidence_has_positioned_zero_ink(prior_evidence)
            or _evidence_has_positioned_zero_ink(runtime_evidence)
        )
        canonical_delivery = None
        expected_manifest = None
        canonical_detector_maps_bound = False
        if canonical_state_present:
            (
                canonical_delivery,
                expected_manifest,
                canonical_detector_maps_bound,
                canonical_failures,
            ) = (
                _canonical_zero_ink_delivery_manifest(
                    provenance_opts,
                    authority=authority,
                    item_id=item_id,
                    page_number=int(page_number),
                )
            )
            record_failures.extend(canonical_failures)

        canonical_count_contribution = None
        canonical_zero_count = None
        if isinstance(canonical_delivery, dict):
            entity_ids = list(canonical_delivery.get("entity_ids") or ())
            representation = str(
                canonical_delivery.get("delivered_representation") or ""
            )
            requested_representation = str(
                canonical_delivery.get("requested_representation") or ""
            )
            proof_page_number = _strict_manifest_int(
                canonical_delivery.get("page_number")
            )
            proof_source_span_id = _strict_manifest_int(
                canonical_delivery.get("source_span_id")
            )
            if proof_page_number is None:
                proof_page_number = int(page_number)
            if proof_source_span_id is None:
                proof_source_span_id = int(record.get("source_span_id", 0) or 0)
            canonical_characters = (
                expected_manifest.get("characters")
                if isinstance(expected_manifest, dict)
                else []
            )
            if not isinstance(canonical_characters, list):
                canonical_characters = []
            canonical_zero_count = sum(
                1
                for character in canonical_characters
                if isinstance(character, dict)
                and isinstance(character.get("text"), str)
                and source_character_is_zero_ink(character)
            )
            canonical_logical_zero_ink = bool(canonical_characters) and (
                canonical_zero_count == len(canonical_characters)
            )
            canonical_count_contribution = (
                0
                if canonical_logical_zero_ink
                and canonical_delivery.get("logical_zero_ink_delivery") is True
                else 1
            )
            if (
                canonical_detector_maps_bound
                and isinstance(runtime_outcome, AttemptOutcome)
                and runtime_delivery_representation != representation
            ):
                record_failures.append(
                    "zero_ink_runtime_delivery_representation_unbound"
                )
            if not record_entity_ids_exact or record_entity_ids != entity_ids:
                record_failures.append("zero_ink_record_entity_identity_unbound")
            if record_representation != representation:
                record_failures.append("zero_ink_record_representation_unbound")
            if record_requested_representation != requested_representation:
                record_failures.append(
                    "zero_ink_record_requested_representation_unbound"
                )
            if _strict_manifest_int(record.get("physical_entity_count")) != len(
                entity_ids
            ):
                record_failures.append("zero_ink_record_physical_count_unbound")
            if _strict_manifest_int(record.get("zero_ink_character_count")) != (
                canonical_zero_count
            ):
                record_failures.append("zero_ink_record_character_count_unbound")
            if record.get("source_manifest_sha256") != canonical_delivery.get(
                "source_manifest_sha256"
            ):
                record_failures.append("zero_ink_record_manifest_identity_unbound")
            if record.get("zero_ink_delivery_manifest_sha256") != (
                authority.delivery_manifest_sha256
                if isinstance(authority, ZeroInkReconciliationAuthority)
                else None
            ):
                record_failures.append(
                    "zero_ink_record_delivery_manifest_identity_unbound"
                )
            if (
                _strict_manifest_int(record.get("delivered_count_contribution"))
                != canonical_count_contribution
            ):
                record_failures.append(
                    "zero_ink_record_count_contribution_unbound"
                )
            if delivered_attempt.get("attempted_representation") != representation:
                record_failures.append("zero_ink_attempt_representation_unbound")
            raw_attempt_entity_ids = delivered_attempt.get("entity_ids")
            attempt_entity_ids = [
                value
                for value in tuple(raw_attempt_entity_ids or ())
                if isinstance(value, str) and value
            ]
            if (
                not isinstance(raw_attempt_entity_ids, (list, tuple))
                or len(attempt_entity_ids) != len(raw_attempt_entity_ids)
                or attempt_entity_ids != entity_ids
            ):
                record_failures.append("zero_ink_attempt_entity_identity_unbound")
        else:
            entity_ids = record_entity_ids
            representation = record_representation
            requested_representation = record_requested_representation
            proof_page_number = int(record.get("page", 0) or 0)
            proof_source_span_id = int(record.get("source_span_id", 0) or 0)

        final_objects = {}
        for entity_id in entity_ids:
            if registry_getter_failure is not None:
                final_objects[entity_id] = None
                record_failures.append(
                    "final_entity_registry_lookup_unreadable:"
                    f"{entity_id}:{registry_getter_failure}"
                )
                continue
            try:
                final_objects[entity_id] = (
                    getter(entity_id) if callable(getter) else None
                )
            except Exception as exc:
                final_objects[entity_id] = None
                record_failures.append(
                    "final_entity_registry_lookup_unreadable:"
                    f"{entity_id}:{type(exc).__name__}"
                )

        entity_expectations, mutable_expectation_entries, expectation_failures = (
            _delivery_entity_expectations(
                prior_evidence,
                entity_ids,
                representation,
            )
        )
        expected_locations = _delivery_expected_locations(prior_evidence, entity_ids)
        expected_type = expected_types.get(representation)
        if isinstance(canonical_delivery, dict):
            logical_zero_ink_claimed = bool(
                canonical_delivery.get("logical_zero_ink_delivery")
            )
        else:
            logical_zero_ink_claimed = not entity_ids and (
                record.get("zero_ink_delivery") is True
                or prior_evidence.get("zero_ink_delivery") is True
                or prior_evidence.get("proof_kind")
                == "positioned_zero_ink_delivery_v1"
            )
        strict_continuity_enabled = provenance_opts is not None
        canonical_parent_verified = None
        provenance_parent_handle_verified = None
        if strict_continuity_enabled:
            if entity_ids:
                record_failures.extend(expectation_failures)
                (
                    sealed_expectations,
                    sealed_expectation_entries,
                    sealed_expectation_failures,
                ) = _sealed_entity_expectations(
                    provenance_opts,
                    record,
                    item_id=item_id,
                    page_number=proof_page_number,
                    source_span_id=proof_source_span_id,
                    requested_representation=requested_representation,
                    representation=representation,
                    entity_ids=entity_ids,
                )
                record_failures.extend(sealed_expectation_failures)
                if mutable_expectation_entries != sealed_expectation_entries:
                    record_failures.append(
                        "final_entity_expectation_detector_mismatch"
                    )
                for entry in mutable_expectation_entries:
                    if (
                        isinstance(entry, dict)
                        and entry.get("kind") == "character"
                        and _finite_matrix_values(
                            entry.get("intended_affine_matrix")
                        )
                        is None
                    ):
                        record_failures.append(
                            "final_entity_affine_expectation_invalid:"
                            + str(entry.get("entity_id") or "<unbound>")
                        )
                if sealed_expectations:
                    entity_expectations = sealed_expectations
            canonical_parent_verified = True
            provenance_parent_handle_verified = True
            canonical_parent = None
            if entity_ids:
                canonical_parent = final_objects.get(entity_ids[0])
                if canonical_parent is None:
                    canonical_parent_verified = False
                    record_failures.append(
                        f"missing_final_entity:{entity_ids[0]}"
                    )
                if (
                    not isinstance(runtime_outcome, AttemptOutcome)
                    or runtime_outcome.entity is not canonical_parent
                ):
                    canonical_parent_verified = False
                    record_failures.append("final_entity_runtime_parent_mismatch")
            elif logical_zero_ink_claimed:
                if (
                    not isinstance(runtime_outcome, AttemptOutcome)
                    or runtime_outcome.entity is not None
                    or tuple(runtime_outcome.entity_ids or ())
                ):
                    canonical_parent_verified = False
                    record_failures.append("final_entity_runtime_parent_mismatch")

            expected_parent_handle = (
                str(record.get("logical_delivery_id") or "")
                if logical_zero_ink_claimed
                else (entity_ids[0] if entity_ids else "")
            )
            provenance_objects = getattr(
                provenance_opts,
                "_source_provenance_objects",
                None,
            )
            provenance_matches = []
            if isinstance(provenance_objects, list):
                provenance_matches = [
                    candidate
                    for candidate in provenance_objects
                    if _strict_manifest_int(getattr(candidate, "page", None))
                    == int(proof_page_number)
                    and _strict_manifest_int(getattr(candidate, "span_id", None))
                    == int(proof_source_span_id)
                    and str(getattr(candidate, "source_kind", "") or "")
                    == "text_span"
                ]
            if (
                len(provenance_matches) != 1
                or str(getattr(provenance_matches[0], "parent_handle", "") or "")
                != expected_parent_handle
            ):
                provenance_parent_handle_verified = False
                record_failures.append("source_provenance_parent_handle_mismatch")
        evidence_zero_count = prior_evidence.get("zero_ink_character_count")
        raw_manifest_characters = (
            expected_manifest.get("characters")
            if isinstance(expected_manifest, dict)
            else ()
        )
        manifest_characters = (
            raw_manifest_characters
            if isinstance(raw_manifest_characters, (list, tuple))
            else ()
        )
        expected_zero_count = sum(
            1
            for character in manifest_characters
            if isinstance(character, dict)
            and isinstance(character.get("text"), str)
            and source_character_is_zero_ink(character)
        )
        record_zero_count = record.get("zero_ink_character_count")
        evidence_characters = prior_evidence.get("character_entities")
        evidence_zero_child = bool(
            isinstance(evidence_characters, (list, tuple))
            and any(
                isinstance(character, dict)
                and (
                    isinstance(character.get("verification"), dict)
                    and character["verification"].get("zero_ink_identity") is True
                    or (
                        isinstance(character.get("text"), str)
                        and source_character_is_zero_ink(character)
                    )
                )
                for character in evidence_characters
            )
        )
        if isinstance(canonical_delivery, dict):
            nested_zero_ink_claimed = bool(
                canonical_zero_count is not None and canonical_zero_count > 0
            )
        else:
            nested_zero_ink_claimed = bool(
                representation in {"glyphs", "geometry"}
                and (
                    expected_zero_count > 0
                    or evidence_zero_child
                    or (
                        isinstance(evidence_zero_count, int)
                        and not isinstance(evidence_zero_count, bool)
                        and evidence_zero_count > 0
                    )
                    or (
                        isinstance(record_zero_count, int)
                        and not isinstance(record_zero_count, bool)
                        and record_zero_count > 0
                    )
                )
            )
        logical_zero_ink_verified = False
        nested_zero_ink_verified = False
        if logical_zero_ink_claimed:
            if not isinstance(runtime_outcome, AttemptOutcome):
                record_failures.append("zero_ink_runtime_outcome_missing")
            else:
                if (
                    runtime_outcome.entity is not None
                    or tuple(runtime_outcome.entity_ids or ())
                ):
                    record_failures.append(
                        "zero_ink_runtime_outcome_has_physical_entity"
                    )
                if (
                    runtime_outcome.owned_artifacts
                    or runtime_outcome.owned_objects
                    or runtime_outcome.owned_datablocks
                ):
                    record_failures.append(
                        "zero_ink_runtime_outcome_retains_owned_artifacts"
                    )
            record_failures.extend(
                zero_ink_delivery_proof_failures(
                    attempted_representation=representation,
                    requested_representation=requested_representation,
                    item_id=item_id,
                    page_number=proof_page_number,
                    source_span_id=proof_source_span_id,
                    outcome=AttemptOutcome.delivered(
                        None,
                        entity_ids=(),
                        evidence=prior_evidence,
                    ),
                    expected_zero_ink_manifest=expected_manifest,
                )
            )
            expected_logical_id = f"{item_id}:zero-ink:{representation}"
            if record.get("zero_ink_delivery") is not True:
                record_failures.append("zero_ink_record_claim_missing")
            if record.get("logical_delivery_id") != expected_logical_id:
                record_failures.append("zero_ink_record_identity_unbound")
            if record.get("physical_entity_count") != 0:
                record_failures.append("zero_ink_record_physical_count_not_zero")
            if requested_representation != representation:
                record_failures.append("zero_ink_record_representation_mismatch")
            if tuple(delivered_attempt.get("entity_ids") or ()):
                record_failures.append("zero_ink_attempt_has_physical_entity_identity")
            if delivered_attempt.get("attempted_representation") != representation:
                record_failures.append("zero_ink_attempt_representation_unbound")
            if record.get("source_manifest_sha256") != prior_evidence.get(
                "source_manifest_sha256"
            ):
                record_failures.append("zero_ink_record_manifest_identity_unbound")
            record_failures = list(dict.fromkeys(record_failures))
            logical_zero_ink_verified = not record_failures
            nested_zero_ink_verified = logical_zero_ink_verified
        elif nested_zero_ink_claimed:
            if not isinstance(runtime_outcome, AttemptOutcome):
                record_failures.append("zero_ink_runtime_outcome_missing")
                proof_outcome = AttemptOutcome.delivered(
                    None,
                    entity_ids=tuple(entity_ids),
                    evidence=prior_evidence,
                )
            else:
                raw_runtime_ids = tuple(runtime_outcome.entity_ids or ())
                runtime_ids = [
                    value
                    for value in raw_runtime_ids
                    if isinstance(value, str) and value
                ]
                if (
                    len(runtime_ids) != len(raw_runtime_ids)
                    or runtime_ids != entity_ids
                ):
                    record_failures.append(
                        "zero_ink_runtime_outcome_entity_identity_unbound"
                    )
                proof_outcome = AttemptOutcome.delivered(
                    runtime_outcome.entity,
                    entity_ids=tuple(runtime_ids),
                    evidence=prior_evidence,
                )
            record_failures.extend(
                zero_ink_character_proof_failures(
                    attempted_representation=representation,
                    requested_representation=requested_representation,
                    item_id=item_id,
                    page_number=proof_page_number,
                    source_span_id=proof_source_span_id,
                    outcome=proof_outcome,
                    expected_zero_ink_manifest=expected_manifest,
                )
            )
            if record.get("zero_ink_character_count") != evidence_zero_count:
                record_failures.append("zero_ink_record_character_count_unbound")
            if record.get("source_manifest_sha256") != prior_evidence.get(
                "source_manifest_sha256"
            ):
                record_failures.append("zero_ink_record_manifest_identity_unbound")
            if [
                str(value)
                for value in tuple(delivered_attempt.get("entity_ids") or ())
                if str(value)
            ] != entity_ids:
                record_failures.append("zero_ink_attempt_entity_identity_unbound")
            record_failures = list(dict.fromkeys(record_failures))
            nested_zero_ink_verified = not record_failures
        for entity_id in entity_ids:
            obj = final_objects.get(entity_id)
            if obj is None:
                record_failures.append(f"missing_final_entity:{entity_id}")
                continue
            try:
                actual_type = str(getattr(obj, "type", "") or "")
                location = _object_world_location(obj)
            except (AttributeError, IndexError, ReferenceError, TypeError, ValueError):
                record_failures.append(f"unreadable_final_entity:{entity_id}")
                continue
            proof = {
                "entity_id": entity_id,
                "actual_object_type": actual_type,
                "actual_location_m": location,
            }
            if expected_type and actual_type != expected_type:
                record_failures.append(f"final_entity_type_mismatch:{entity_id}")
            if not all(math.isfinite(value) for value in location):
                record_failures.append(f"nonfinite_final_entity_location:{entity_id}")
            prior_location = expected_locations.get(entity_id)
            if prior_location is not None:
                expected_location = [
                    prior_location[0],
                    prior_location[1] + float(stack_offset_m),
                ]
                proof["expected_location_m"] = expected_location
                if len(location) < 2 or any(
                    abs(actual - expected) > 1e-7
                    for actual, expected in zip(  # noqa: B905
                        location[:2], expected_location
                    )
                ):
                    record_failures.append(f"final_entity_location_mismatch:{entity_id}")
            if strict_continuity_enabled:
                try:
                    continuity_proof, continuity_failures = (
                        _live_entity_continuity_failures(
                            obj,
                            entity_id=entity_id,
                            item_id=item_id,
                            source_span_id=proof_source_span_id,
                            requested_representation=requested_representation,
                            representation=representation,
                            expectation=entity_expectations.get(entity_id),
                            stack_offset_m=stack_offset_m,
                        )
                    )
                except Exception as exc:
                    continuity_proof = {
                        "object_handle_verified": False,
                        "source_item_verified": False,
                        "character_identity_verified": False,
                        "representation_fields_verified": False,
                        "text_material_binding_verified": False,
                        "affine_verified": False,
                        "physical_ink_continuity_verified": False,
                    }
                    continuity_failures = [
                        "final_entity_continuity_unreadable:"
                        f"{entity_id}:{type(exc).__name__}"
                    ]
                proof.update(continuity_proof)
                record_failures.extend(continuity_failures)
            entity_proofs.append(proof)
        if not entity_ids and not logical_zero_ink_claimed:
            record_failures.append("final_entity_identity_missing")
        final_proof = {
            "status": "failed" if record_failures else "verified",
            "page_number": int(page_number),
            "stack_offset_m": float(stack_offset_m),
            "representation": representation,
            "canonical_parent_verified": canonical_parent_verified,
            "provenance_parent_handle_verified": provenance_parent_handle_verified,
            "entities": entity_proofs,
            "failures": record_failures,
        }
        if logical_zero_ink_claimed:
            final_proof.update({
                "logical_zero_ink_delivery": logical_zero_ink_verified,
                "logical_delivery_id": str(
                    record.get("logical_delivery_id") or ""
                ),
                "source_manifest_sha256": str(
                    record.get("source_manifest_sha256") or ""
                ),
            })
        if nested_zero_ink_claimed:
            final_proof.update({
                "logical_zero_ink_children": int(
                    canonical_zero_count
                    if canonical_zero_count is not None
                    else expected_zero_count
                ),
                "logical_zero_ink_children_verified": nested_zero_ink_verified,
                "source_manifest_sha256": str(
                    record.get("source_manifest_sha256") or ""
                ),
            })
        record["final_state_verification"] = final_proof
        if delivered_attempt:
            delivered_attempt["final_state_verification"] = final_proof
        if record_failures:
            item_id = str(record.get("item_id") or "")
            outcomes = getattr(provenance_opts, "_text_delivery_outcomes", None)
            outcome = outcomes.get(item_id) if isinstance(outcomes, dict) else None
            if outcome is None:
                cleanup = {
                    "status": "failed",
                    "removed": [],
                    "detail": "runtime delivery ownership record missing",
                }
            else:
                try:
                    cleanup = cleanup_delivery_outcome(outcome)
                except Exception as exc:
                    cleanup = {
                        "status": "failed",
                        "removed": [],
                        "exception_type": type(exc).__name__,
                        "detail": str(exc),
                    }
            cleanup_outcomes = getattr(
                provenance_opts,
                "_text_cleanup_outcomes",
                None,
            )
            if outcome is not None and cleanup.get("status") != "complete":
                if not isinstance(cleanup_outcomes, dict):
                    cleanup_outcomes = {}
                    provenance_opts._text_cleanup_outcomes = (  # noqa: B010
                        cleanup_outcomes
                    )
                cleanup_outcomes[item_id] = outcome
            elif isinstance(cleanup_outcomes, dict):
                cleanup_outcomes.pop(item_id, None)
            if isinstance(outcomes, dict):
                # A failed item is never live-delivery authority.  Exact refs
                # needed after incomplete cleanup live only in the remediation
                # ledger above.
                outcomes.pop(item_id, None)

            source_manifests = getattr(
                provenance_opts,
                "_zero_ink_source_manifests",
                None,
            )
            if isinstance(source_manifests, dict):
                source_manifests.pop(item_id, None)
            zero_ink_delivery_manifests = getattr(
                provenance_opts,
                "_zero_ink_delivery_manifests",
                None,
            )
            if isinstance(zero_ink_delivery_manifests, dict):
                zero_ink_delivery_manifests.pop(item_id, None)
            authorities = getattr(
                provenance_opts,
                "_zero_ink_reconciliation_authorities",
                (),
            )
            if provenance_opts is not None and isinstance(authorities, tuple):
                provenance_opts._zero_ink_reconciliation_authorities = (  # noqa: B010
                    tuple(
                        candidate
                        for candidate in authorities
                        if not (
                            isinstance(
                                candidate,
                                ZeroInkReconciliationAuthority,
                            )
                            and candidate.item_id == item_id
                        )
                    )
                )
            final_entity_authorities = getattr(
                provenance_opts,
                "_final_entity_expectation_authorities",
                None,
            )
            if isinstance(final_entity_authorities, dict):
                final_entity_authorities.pop(item_id, None)
            final_proof["cleanup"] = cleanup
            if canonical_state_present:
                if canonical_detector_maps_bound:
                    count_contribution = canonical_count_contribution
                    count_representation = representation
                else:
                    count_contribution = (
                        runtime_delivery_count_contribution
                        if runtime_delivery_count_contribution is not None
                        else 0
                    )
                    count_representation = runtime_delivery_representation
            else:
                count_contribution = record.get("delivered_count_contribution")
                if not (
                    isinstance(count_contribution, int)
                    and not isinstance(count_contribution, bool)
                    and count_contribution >= 0
                ):
                    count_contribution = 0 if logical_zero_ink_claimed else 1
                count_representation = representation
            failure = {
                "item_id": item_id,
                "page": int(page_number),
                "failures": list(record_failures),
                "cleanup": cleanup,
                "delivered_count_contribution": count_contribution,
            }
            failures.append(failure)
            record["status"] = "failed"
            record["reason"] = "post_stack_final_state_verification_failed"
            record["final_representation"] = None
            record["fallback_used"] = False
            if cleanup.get("status") == "complete":
                record["entity_ids"] = []
            if provenance_opts is not None:
                counts = getattr(
                    provenance_opts,
                    "_text_delivered_entity_counts",
                    None,
                )
                bucket = {
                    "labels": "native_label",
                    "text": "native_text",
                    "3d_text": "native_3d_text",
                    "glyphs": "glyph_curve",
                    "geometry": "geometry_mesh",
                    "raster": "raster_patch",
                }.get(count_representation)
                if (
                    isinstance(counts, dict)
                    and bucket
                    and count_contribution > 0
                ):
                    counts[bucket] = max(
                        0,
                        int(counts.get(bucket, 0) or 0) - count_contribution,
                    )
                provenance = getattr(
                    provenance_opts,
                    "_source_provenance_objects",
                    None,
                )
                if isinstance(provenance, list):
                    source_span_id = int(record.get("source_span_id", 0) or 0)
                    provenance[:] = [
                        value
                        for value in provenance
                        if not (
                            int(getattr(value, "page", 0) or 0) == int(page_number)
                            and int(getattr(value, "span_id", -1) or -1)
                            == source_span_id
                        )
                    ]
    return failures


# ── Main import entry point ──────────────────────────────────────────

def import_pdf(
    filepath: str,
    config: Optional[dict] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    context=None,
) -> Dict[str, int]:
    """
    Import a PDF file into Blender. Main entry point.

    Args:
        filepath: Absolute path to the PDF file.
        config: Dict with keys like 'mode', 'pages', 'text_mode',
                'import_text', 'detect_arcs', 'make_faces',
                'group_by_color', 'map_dashes'.
        progress_callback: Optional callable(progress_float, message_str).
        context: Optional bpy.context for Blender window-manager progress bar.
                 Pass None for CLI/headless mode.

    Returns:
        Stats dict with keys: pages, primitives, text_items, collections,
        elapsed, curves, meshes, circles, arcs, pages_imported.

    Raises:
        RuntimeError: If PyMuPDF is not available.
        FileNotFoundError: If the PDF file does not exist.
    """
    if config is None:
        config = {}
    if "strict_text_fidelity" in config and not bool(config["strict_text_fidelity"]):
        raise ValueError("strict_text_fidelity cannot be disabled")

    visual_style = str(config.get("visual_style", "source") or "source").strip().lower()
    if visual_style not in {"source", "blueprint", "high_contrast"}:
        visual_style = "source"
    line_z_offset_m = float(config.get("line_z_offset_mm", 0.10) or 0.10) * _MM_TO_M
    text_z_offset_m = float(config.get("text_z_offset_mm", 0.35) or 0.35) * _MM_TO_M
    image_z_offset_m = float(config.get("image_z_offset_mm", 0.0) or 0.0) * _MM_TO_M
    auto_focus_view = bool(config.get("auto_focus_view", True))
    keep_selection_after_focus = bool(config.get("keep_selection_after_focus", False))
    auto_hide_default_cube = bool(config.get("auto_hide_default_cube", True))

    # Blender window-manager progress bar (safe when context is None)
    wm = None
    if context is not None:
        try:
            wm = context.window_manager
            wm.progress_begin(0, 100)
        except Exception:
            wm = None

    def _wm_progress(pct: float):
        """Update Blender's progress bar (0.0-1.0 -> 0-100)."""
        if wm is not None:
            try:
                wm.progress_update(int(pct * 100))
            except Exception:
                pass

    def _progress(pct: float, msg: str):
        if progress_callback:
            progress_callback(pct, msg)
        _wm_progress(pct)

    t_start = time.perf_counter()
    phase_timings_ms: Dict[str, float] = {}
    doc = None
    image_dir = ""
    image_dir_owned = False

    try:
        # 1. Verify PyMuPDF is available
        t_phase = time.perf_counter()
        _progress(0.0, "Checking dependencies...")
        if not check_pymupdf():
            from .dependency_manager import ensure_pymupdf_runtime

            if not ensure_pymupdf_runtime(auto_install=True):
                raise RuntimeError(
                    "PyMuPDF is not installed for this Blender Python build. "
                    "Open addon preferences (Edit > Preferences > Add-ons > "
                    "PDF Vector Importer) and click 'Install PyMuPDF'."
                )

        ensure_lib_path()
        from .pdfcadcore.fitz_loader import import_fitz
        from .dependency_manager import get_lib_dir

        import_fitz(prefer_lib_dir=str(get_lib_dir()))
        phase_timings_ms["dependencies_ms"] = (time.perf_counter() - t_phase) * 1000.0

        # 2. Verify file exists
        t_phase = time.perf_counter()
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"PDF file not found: {filepath}")

        hidden_startup_cube = 0
        if auto_hide_default_cube:
            hidden_startup_cube = _auto_hide_default_cube(bpy.context.scene)

        # 3. Build ImportConfig from mode + overrides (BCS-ARCH-001)
        import_cfg = _config_from_mode(config.get("mode", "auto"))
        import_cfg = _apply_overrides(import_cfg, config)
        if import_cfg.import_text and str(import_cfg.text_mode or "3d_text") != "none":
            import_cfg.text_mode = normalize_representation(import_cfg.text_mode)
            try:
                from .pdfcadcore.source_provenance import ensure_import_session_id

                ensure_import_session_id(import_cfg)
            except ImportError:
                pass

        # 4. Reset pdfcadcore ID counter
        reset_ids()
        phase_timings_ms["setup_ms"] = (time.perf_counter() - t_phase) * 1000.0

        # 5. Open PDF
        t_phase = time.perf_counter()
        _progress(0.05, "Opening PDF...")
        from .pdfcadcore.fitz_loader import safe_open

        doc = safe_open(filepath)
        total_pages = doc.page_count

        # 6. Determine pages to import
        page_indices = _parse_pages(config.get("pages", "all"), total_pages)
        phase_timings_ms["open_pdf_ms"] = (time.perf_counter() - t_phase) * 1000.0
        if not page_indices:
            doc.close()
            elapsed = time.perf_counter() - t_start
            phase_timings_ms["total_ms"] = elapsed * 1000.0
            return {
                "pages_imported": 0, "pages": 0, "primitives": 0,
                "text_items": 0, "collections": 0, "elapsed": elapsed,
                "curves": 0, "meshes": 0, "circles": 0, "arcs": 0, "images": 0,
                "performance_phases": phase_timings_ms,
            }

        # 7. Create root collection
        basename = os.path.splitext(os.path.basename(filepath))[0]
        root_col = bpy.data.collections.new(f"PDF Import - {basename}")
        bpy.context.scene.collection.children.link(root_col)
        collections_created = 1  # root collection
        text_delivery_enabled = (
            bool(import_cfg.import_text)
            and str(import_cfg.text_mode or "3d_text").strip().lower() != "none"
        )
        image_dir = (
            tempfile.mkdtemp(prefix="bc_bl_pdf_images_")
            if (not import_cfg.ignore_images or text_delivery_enabled)
            else ""
        )
        image_dir_owned = bool(
            image_dir
            and Path(image_dir).name.startswith("bc_bl_pdf_images_")
        )

        # 8. Build config dict for geometry builder
        builder_config = {
            "make_faces": import_cfg.make_faces,
            "ignore_fill_only_shapes": bool(config.get("ignore_fill_only_shapes", True)),
            "group_by_color": import_cfg.group_by_color,
            "map_dashes": import_cfg.map_dashes,
            "visual_style": visual_style,
            "line_z_offset_m": line_z_offset_m,
            "model3d_mode": getattr(import_cfg, "model3d_mode", "off"),
            "model3d_depth_m": float(getattr(import_cfg, "model3d_depth_mm", 3.175) or 3.175) * _MM_TO_M,
        }

        # 9. Process each page
        total_stats = {
            "pages_imported": 0, "primitives": 0, "text_items": 0,
            "curves": 0, "meshes": 0, "circles": 0, "arcs": 0, "images": 0,
            "skipped_fill_only": 0,
            "recognition_skipped_pages": 0,
            "hidden_startup_cube": hidden_startup_cube,
            "text_source_spans": 0,
            "text_source_item_ids": [],
            "text_glyph_estimate": 0,
            "parts_bootstrap_text_items": [],
            "model3d_solids": 0,
            "raster_delivery_failures": [],
            "geometry_delivery_issues": [],
            "text_final_state_failures": [],
        }
        raster_pages_imported = 0
        total_page_count = max(1, len(page_indices))
        model3d_all_text = []

        def _page_progress(page_offset: int, stage: float) -> float:
            stage_clamped = max(0.0, min(1.0, float(stage)))
            base = 0.10 + 0.75 * (page_offset / total_page_count)
            span = 0.75 / total_page_count
            return min(0.95, base + span * stage_clamped)

        def _add_phase_ms(name: str, started: float) -> None:
            phase_timings_ms[name] = float(phase_timings_ms.get(name, 0.0) or 0.0) + (
                (time.perf_counter() - started) * 1000.0
            )

        # Multi-page stacking: shift each page downward by accumulated heights.
        _page_stack_offset_m = 0.0
        _page_arrangement = _normalize_page_arrangement(config.get("page_arrangement"))
        _page_gap_ratio = _normalize_page_gap_ratio(config.get("page_gap_ratio"))

        use_streaming = len(page_indices) > 1
        page_numbers = [idx + 1 for idx in page_indices]
        stream_cancelled = False
        t_pages_phase = time.perf_counter()

        def _iter_pages_for_import():
            """Yield (loop_index, page_idx, page_num, page, page_data) per page."""
            extract_kwargs = dict(
                scale=import_cfg.user_scale,
                flip_y=import_cfg.flip_y,
                detect_arcs=import_cfg.detect_arcs,
                arc_fit_tol_mm=import_cfg.arc_fit_tol_mm,
                min_arc_angle_deg=import_cfg.min_arc_angle_deg,
                arc_min_pts=getattr(import_cfg, "arc_sampling_pts", 5),
            )
            # arc_min_pts is consumed by extract_page / iter_pages via pdfcadcore;
            # arc_sampling_pts in ImportConfig maps to that gate parameter.
            if use_streaming:
                def _on_stream_progress(prog):
                    nonlocal stream_cancelled
                    _progress(
                        _page_progress(prog.page_index - 1, 0.35),
                        f"Extracted page {prog.page_number}/{prog.total_pages}: "
                        f"{prog.primitive_count} primitives ({prog.elapsed_s:.1f}s)",
                    )
                    if prog.over_budget:
                        _progress(
                            _page_progress(prog.page_index - 1, 0.38),
                            f"Page {prog.page_number} exceeded soft budget ({prog.elapsed_s:.1f}s)",
                        )
                    return False if stream_cancelled else None

                for loop_i, (page_num, page_data) in enumerate(
                    iter_pages(
                        doc,
                        pages=page_numbers,
                        progress=_on_stream_progress,
                        **extract_kwargs,
                    )
                ):
                    page_idx = page_num - 1
                    page = doc.load_page(page_idx)
                    yield loop_i, page_idx, page_num, page, page_data
            else:
                page_idx = page_indices[0]
                page_num = page_idx + 1
                page = doc.load_page(page_idx)
                page_data = extract_page(page, page_num, **extract_kwargs)
                yield 0, page_idx, page_num, page, page_data

        for i, _page_idx, page_num, page, page_data in _iter_pages_for_import():
            _progress(_page_progress(i, 0.05), f"Processing page {page_num}/{len(page_indices)}...")

            import_mode = (import_cfg.import_mode or "auto").strip().lower()

            # 9a. Auto-mode classification (before extraction)
            if import_mode == "auto":
                t_phase = time.perf_counter()
                raw_drawings = page.get_drawings()
                text_blocks = page.get_text("blocks") or []
                text_words = page.get_text("words") or []
                mbox = page.mediabox
                page_area = float(mbox.width) * float(mbox.height)
                classification = classify_page_content(
                    raw_drawings,
                    text_blocks_count=len(text_blocks),
                    text_words_count=len(text_words),
                    page_area=page_area,
                )
                if classification["type"] in ("glyph_flood", "fill_art", "raster_candidate"):
                    _progress(
                        _page_progress(i, 0.15),
                        f"Auto-mode: {classification['reason']} — favoring raster import for page {page_num}",
                    )
                    import_mode = "raster"
                _add_phase_ms("classify_ms", t_phase)

            _progress(_page_progress(i, 0.35), f"Parsed page {page_num}: {len(page_data.primitives)} primitives")
            _merge_scale_into_stats(total_stats, page_data)
            page_text_items = list(page_data.text_items or [])
            total_stats["text_source_spans"] += len(page_text_items)
            total_stats["text_source_item_ids"].extend(
                "page:%d:text:%d" % (int(page_num), int(item.id))
                for item in page_text_items
            )
            total_stats["text_glyph_estimate"] += sum(
                len(str(getattr(item, "text", "") or ""))
                for item in page_text_items
            )
            total_stats["parts_bootstrap_text_items"].extend(page_text_items)
            model3d_all_text.extend(page_text_items)

            # 9c. Geometry cleanup (remove micro-segments)
            if import_cfg.cleanup_level != "conservative" or import_cfg.min_seg_len > 0:
                t_phase = time.perf_counter()
                cleanup_stats = cleanup_primitives(
                    page_data.primitives,
                    cleanup_level=import_cfg.cleanup_level,
                )
                _add_phase_ms("cleanup_ms", t_phase)
                if import_cfg.verbose and cleanup_stats.get("removed_micro", 0) > 0:
                    _progress(_page_progress(i, 0.45), f"Cleanup: removed "
                              f"{cleanup_stats['removed_micro']} micro-segments "
                              f"on page {page_num}")

            if (
                import_mode == "auto"
                and import_cfg.raster_fallback
                and _looks_like_text_cloud_page(len(page_data.primitives), page_data.text_items)
            ):
                profile = _text_item_profile(page_data.text_items)
                _progress(
                    _page_progress(i, 0.50),
                    f"Auto-mode: text-heavy page ({profile['total']} text / {len(page_data.primitives)} vectors) — raster for page {page_num}",
                )
                import_mode = "raster"

            # 9d. Hatch detection (post-extraction, on primitives)
            if import_cfg.hatch_mode != "import":
                hatch_ids = tag_hatch_primitives(page_data.primitives)
                if hatch_ids:
                    if import_cfg.hatch_mode == "skip":
                        page_data.primitives = [
                            p for p in page_data.primitives
                            if p.id not in hatch_ids
                        ]
                    elif import_cfg.hatch_mode == "group":
                        for p in page_data.primitives:
                            if p.id in hatch_ids:
                                p.generic_tags.append("hatch_line")

            # 9e. Optional recognition pass
            recognition_skip_reason = _skip_semantic_recognition_for_speed(page_data)
            if recognition_skip_reason:
                total_stats["recognition_skipped_pages"] += 1
                _progress(
                    _page_progress(i, 0.55),
                    f"Skipping semantic recognition on page {page_num}: {recognition_skip_reason}",
                )
            else:
                _progress(_page_progress(i, 0.55), f"Recognition pass on page {page_num}...")
                t_phase = time.perf_counter()
                try:
                    recognition.run(page_data, mode="auto")
                except Exception:
                    # Recognition failure is non-fatal
                    pass
                _add_phase_ms("recognition_ms", t_phase)

            # 9f. Create page collection
            page_col = bpy.data.collections.new(f"PDF_Page_{page_num}")
            root_col.children.link(page_col)
            collections_created += 1

            # 9g. Build geometry
            page_stats = {"curves": 0, "meshes": 0, "circles": 0, "arcs": 0}
            if import_mode != "raster":
                page_builder_config = dict(builder_config)
                try:
                    from .pdfcadcore.model3d_intent import analyze_model3d_intent

                    page_intent = analyze_model3d_intent(page_data.text_items or [], host_supports_3d=True)
                    page_builder_config["model3d_intent_feasible"] = bool(page_intent.feasible)
                except (ImportError, RuntimeError, TypeError, ValueError, AttributeError):
                    page_builder_config["model3d_intent_feasible"] = False
                _progress(_page_progress(i, 0.72), f"Building geometry for page {page_num}...")
                t_phase = time.perf_counter()
                def _geom_progress(frac, _i=i, _pn=page_num):
                    frac = max(0.0, min(1.0, float(frac)))
                    _progress(
                        _page_progress(_i, 0.72 + (0.10 * frac)),
                        f"Building geometry for page {_pn}... ({int(frac * 100)}%)",
                    )
                page_stats = build_page(
                    page_data,
                    page_col,
                    page_builder_config,
                    progress_callback=_geom_progress,
                )
                _add_phase_ms("geometry_ms", t_phase)

            # 9h. Build text objects
            text_count = 0
            if import_cfg.import_text and import_cfg.text_mode != "none":
                _progress(_page_progress(i, 0.82), f"Building text for page {page_num}...")
                t_phase = time.perf_counter()
                def _text_progress(frac, _i=i, _pn=page_num):
                    frac = max(0.0, min(1.0, float(frac)))
                    _progress(
                        _page_progress(_i, 0.82 + (0.09 * frac)),
                        f"Building text for page {_pn}... ({int(frac * 100)}%)",
                    )
                text_count = build_all_text(
                    page_text_items,
                    page_col,
                    page_num,
                    visual_style=visual_style,
                    z_offset_m=text_z_offset_m,
                    strict_text_fidelity=import_cfg.strict_text_fidelity,
                    text_mode=import_cfg.text_mode,
                    progress_callback=_text_progress,
                    provenance_opts=import_cfg,
                    terminal_raster_callback=(
                        lambda text_item, collection, callback_page_number, item_id,
                        _page=page, _cfg=import_cfg, _dir=image_dir, _z=text_z_offset_m:
                        _render_text_item_raster(
                            _page,
                            text_item,
                            collection,
                            page_num=callback_page_number,
                            item_id=item_id,
                            import_cfg=_cfg,
                            image_dir=_dir,
                            z_offset_m=_z,
                        )
                    ),
                )
                _add_phase_ms("text_ms", t_phase)

            # 9i. Build image/raster planes
            image_count = 0
            raster_page_delivered = False
            delivery_records = getattr(import_cfg, "_text_delivery_records", ())
            excluded_text_bboxes = _delivered_text_bboxes(
                page_data.text_items,
                delivery_records,
                page_num,
            )
            if not import_cfg.ignore_images:
                _progress(_page_progress(i, 0.92), f"Building images for page {page_num}...")
                t_phase = time.perf_counter()
                placements = []
                if import_mode == "raster":
                    rendered = _render_page_raster(
                        page,
                        page_num,
                        import_cfg,
                        image_dir,
                        excluded_text_bboxes=excluded_text_bboxes,
                    )
                    if rendered:
                        placements.append(rendered)
                    else:
                        _record_raster_delivery_failure(
                            total_stats["raster_delivery_failures"],
                            page_num=page_num,
                            stage="render",
                            reason="raster_render_failed",
                        )
                        _progress(
                            _page_progress(i, 0.94),
                            f"Raster delivery failed on page {page_num}; see import report.",
                        )
                else:
                    placements = _extract_image_placements(doc, page, page_num, import_cfg, image_dir)
                    if (
                        import_cfg.raster_fallback
                        and not placements
                        and (not page_data.primitives or _looks_like_page_frame_only(page_data))
                    ):
                        _progress(
                            _page_progress(i, 0.93),
                            f"Auto-mode: sparse vector shell on page {page_num} — raster fallback",
                        )
                        rendered = _render_page_raster(
                            page,
                            page_num,
                            import_cfg,
                            image_dir,
                            excluded_text_bboxes=excluded_text_bboxes,
                        )
                        if rendered:
                            placements.append(rendered)
                        else:
                            _record_raster_delivery_failure(
                                total_stats["raster_delivery_failures"],
                                page_num=page_num,
                                stage="render",
                                reason="raster_render_failed",
                            )
                            _progress(
                                _page_progress(i, 0.94),
                                f"Raster delivery failed on page {page_num}; see import report.",
                            )

                for placement in placements:
                    if _create_image_plane(placement, page_col, z_offset_m=image_z_offset_m):
                        image_count += 1
                        if isinstance(placement, dict) and placement.get("xref") == -1:
                            raster_page_delivered = True
                    elif isinstance(placement, dict) and int(placement.get("xref", 0) or 0) == -1:
                        _record_raster_delivery_failure(
                            total_stats["raster_delivery_failures"],
                            page_num=page_num,
                            stage="plane",
                            reason="raster_plane_creation_failed",
                        )
                        _progress(
                            _page_progress(i, 0.94),
                            f"Raster delivery failed on page {page_num}; see import report.",
                        )
                _add_phase_ms("images_ms", t_phase)
            if raster_page_delivered:
                raster_pages_imported += 1

            # 9j. Multi-page stacking: shift this page's collection downward
            if len(page_indices) > 1 and _page_stack_offset_m != 0.0:
                _stack_page_objects(page_col.all_objects, _page_stack_offset_m)
            final_text_failures = _reverify_text_delivery_after_stack(
                getattr(import_cfg, "_text_delivery_records", ()),
                page_number=page_num,
                stack_offset_m=_page_stack_offset_m,
                provenance_opts=import_cfg,
            )
            total_stats["text_final_state_failures"].extend(final_text_failures)
            failed_text_count = sum(
                max(0, int(failure.get("delivered_count_contribution", 1) or 0))
                for failure in final_text_failures
            )
            text_count = max(0, int(text_count) - failed_text_count)
            # Advance offset for the next page (page_data.height is in mm)
            page_height_m = page_data.height * _MM_TO_M
            _page_stack_offset_m -= _page_stack_step(
                page_height_m,
                _page_arrangement,
                _page_gap_ratio,
            )

            # 9k. Accumulate stats
            total_stats["pages_imported"] += 1
            total_stats["primitives"] += len(page_data.primitives)
            total_stats["text_items"] += text_count
            total_stats["curves"] += page_stats.get("curves", 0)
            total_stats["meshes"] += page_stats.get("meshes", 0)
            total_stats["circles"] += page_stats.get("circles", 0)
            total_stats["arcs"] += page_stats.get("arcs", 0)
            total_stats["images"] += image_count
            total_stats["skipped_fill_only"] += page_stats.get("skipped_fill_only", 0)
            total_stats["model3d_solids"] += page_stats.get("model3d_solids", 0)
            total_stats["geometry_delivery_issues"].extend(
                list(page_stats.get("geometry_delivery_issues") or [])
            )
            _progress(
                _page_progress(i, 1.0),
                f"Finished page {page_num}/{len(page_indices)} "
                f"({total_stats['primitives']} primitives, {total_stats['text_items']} text)",
            )

        phase_timings_ms["pages_import_ms"] = (time.perf_counter() - t_pages_phase) * 1000.0
        t_phase = time.perf_counter()
        doc.close()

        elapsed = time.perf_counter() - t_start
        _progress(1.0, "Import complete.")
        try:
            from .pdfcadcore.model3d_intent import analyze_model3d_intent

            model3d_intent = analyze_model3d_intent(model3d_all_text, host_supports_3d=True).to_dict()
        except (ImportError, RuntimeError, TypeError, ValueError, AttributeError):
            model3d_intent = {
                "feasible": False,
                "plates": [],
                "members": [],
                "skipped_reason": "3D intent analysis unavailable",
            }
        model3d_mode = str(getattr(import_cfg, "model3d_mode", "off") or "off").strip().lower()
        model3d_enabled = model3d_mode != "off"
        model3d_solids = int(total_stats.get("model3d_solids", 0) or 0)
        model3d_payload = {
            "supported": True,
            "enabled": model3d_enabled,
            "mode": model3d_mode if model3d_mode in {"off", "auto", "extrude"} else "off",
            "depth_mm": round(float(getattr(import_cfg, "model3d_depth_mm", 3.175) or 3.175), 4),
            "solids_created": model3d_solids,
        }
        if not model3d_enabled:
            model3d_payload["skipped_reason"] = "option_off"
        elif model3d_mode == "auto" and not bool(model3d_intent.get("feasible")):
            model3d_payload["skipped_reason"] = model3d_intent.get("skipped_reason") or "no_3d_intent_evidence"
        elif model3d_solids == 0:
            model3d_payload["skipped_reason"] = "no_extrudable_closed_regions"
        total_stats["model_3d_intent"] = model3d_intent
        total_stats["model_3d"] = model3d_payload

        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        # Merge extended stats into return dict
        total_stats["pages"] = len(page_indices)
        total_stats["collections"] = collections_created
        try:
            total_stats["focused"] = (
                1
                if (
                    auto_focus_view
                    and _focus_view_on_import(
                        root_col,
                        keep_selected=keep_selection_after_focus,
                        prefer_material_preview=(raster_pages_imported > 0),
                    )
                )
                else 0
            )
        except Exception:
            total_stats["focused"] = 0
        phase_timings_ms["finalize_ms"] = (time.perf_counter() - t_phase) * 1000.0
        phase_timings_ms["total_ms"] = elapsed * 1000.0
        total_stats["elapsed"] = elapsed
        total_stats["performance_phases"] = phase_timings_ms
        if image_dir_owned and image_dir and os.path.isdir(image_dir):
            try:
                shutil.rmtree(image_dir)
                image_dir_owned = False
            except OSError as exc:
                total_stats["temp_cleanup_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
        delivery_summary = _text_delivery_from_provenance(import_cfg)["summary"]
        total_stats["text_delivery_source_items"] = int(delivery_summary["source_items"])
        total_stats["text_delivery_delivered_items"] = int(delivery_summary["delivered_items"])
        total_stats["text_delivery_fallback_items"] = int(delivery_summary["fallback_items"])
        total_stats["text_delivery_failed_items"] = int(delivery_summary["failed_items"])
        total_stats["text_delivery_failed_item_ids"] = list(delivery_summary["failed_item_ids"])
        try:
            report_path = write_import_report(
                filepath,
                config,
                total_stats,
                import_mode=(import_cfg.import_mode or "auto").strip().lower(),
                raster_pages=raster_pages_imported,
                provenance_opts=import_cfg,
            )
            total_stats["import_report_path"] = report_path
        except (OSError, RuntimeError, TypeError, ValueError, ImportError) as exc:
            total_stats["import_report_error"] = str(exc)
        return total_stats

    finally:
        if doc is not None and not bool(getattr(doc, "is_closed", False)):
            try:
                doc.close()
            except Exception:
                pass
        if image_dir_owned and image_dir and os.path.isdir(image_dir):
            try:
                shutil.rmtree(image_dir)
            except OSError:
                pass
        if wm is not None:
            try:
                wm.progress_end()
            except Exception:
                pass
