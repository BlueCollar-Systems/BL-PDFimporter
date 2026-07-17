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
from .packed_assets import pack_and_verify_bytes
from .pdfcadcore import (
    ImportConfig, extract_page, iter_pages, recognition, reset_ids,
    classify_page_content, tag_hatch_primitives, cleanup_primitives,
)
from .bl_geometry_builder import build_page
from .bl_text_builder import build_all_text, cleanup_delivery_outcome
from .pdfcadcore.primitive_extractor import (
    _page_rotation_transform,
    _transform_pdf_point,
)
from .text_delivery import (
    AttemptOutcome,
    normalize_representation,
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
                else "multiple_item_specific_reasons; see extra.text_delivery.items"
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
    text_delivery = _text_delivery_from_provenance(provenance_opts)
    text_delivery_summary = text_delivery["summary"]
    text_fallback = _text_fallback_from_provenance(provenance_opts)
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
        for record in text_delivery["items"]
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
    text_source_spans = int(stats.get("text_source_spans", stats.get("text_items", 0)) or 0)
    text_glyph_estimate = int(stats.get("text_glyph_estimate", 0) or 0)
    bootstrap_text_items = list(stats.get("parts_bootstrap_text_items") or [])
    try:
        delivered_text_counts = getattr(provenance_opts, "_text_delivered_entity_counts", None)
    except AttributeError:
        delivered_text_counts = None
    if not isinstance(delivered_text_counts, dict):
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

    text_mode = str(config.get("text_mode") or "3d_text")
    import_text_enabled = bool(config.get("import_text", True)) and text_mode != "none"
    delivery_source_items = int(text_delivery_summary["source_items"])
    delivery_delivered_items = int(text_delivery_summary["delivered_items"])
    delivery_failed_items = int(text_delivery_summary["failed_items"])
    text_delivery_required = bool(import_text_enabled and text_source_spans > 0)
    text_delivery_verified = bool(
        not text_delivery_required
        or (
            delivery_source_items == text_source_spans
            and delivery_delivered_items == delivery_source_items
            and delivery_failed_items == 0
        )
    )
    terminal_failure = {}
    if not text_delivery_verified:
        terminal_failure["text_delivery"] = {
            "required_source_items": text_source_spans,
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
    }
    if terminal_failure:
        extra["terminal_failure"] = terminal_failure
    if int(text_delivery_summary["source_items"]) > 0:
        extra["text_delivery"] = text_delivery
    if text_source_spans > 0 or int(text_delivery_summary["source_items"]) > 0:
        extra["text_representation_delivery"] = {
            "required": text_delivery_required,
            "verified": text_delivery_verified,
            "source_items": delivery_source_items,
            "delivered_items": delivery_delivered_items,
            "failed_items": delivery_failed_items,
        }
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
    if bool(config.get("import_text", True)) and text_mode != "none":
        extra["actual_text_entity_types"] = build_actual_text_entity_types(
            host_app="blender",
            text_mode=text_mode,
            count=int(stats.get("text_items", 0) or 0),
            font_rendered=font_rendered,
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
        import_text=bool(config.get("import_text", True)),
        text_mode=text_mode if import_text_enabled else None,
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
                "Review extra.text_delivery.items for each requested representation, "
                "impossibility proof, cleanup result, and final entity identity."
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


def _remove_created_image_plane(obj, collection) -> Dict[str, Any]:
    """Remove every attempt-owned datablock from one failed raster plane."""
    removed: List[str] = []
    try:
        object_name = str(getattr(obj, "name", "") or "")
        mesh = getattr(obj, "data", None)
        mesh_name = str(getattr(mesh, "name", "") or "") if mesh is not None else ""
        material_name = str(obj.get("pdf_image_material", "") or "")
        material_owned = bool(obj.get("pdf_image_material_owned", False))
        image_name = str(obj.get("pdf_image_datablock", "") or "")
        image_owned = bool(obj.get("pdf_image_datablock_owned", False))
    except ReferenceError:
        return {"status": "complete", "removed": ["<already_removed_image_plane>"]}
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
    for registry_name, name, owned in (
        ("materials", material_name, material_owned),
        ("images", image_name, image_owned),
    ):
        if not owned or not name:
            continue
        registry = getattr(bpy.data, registry_name, None)
        get = getattr(registry, "get", None)
        remove = getattr(registry, "remove", None)
        block = get(name) if callable(get) else None
        if block is None:
            continue
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


def _raise_for_incomplete_raster_cleanup(*results: Dict[str, Any]) -> None:
    incomplete = [result for result in results if result.get("status") != "complete"]
    if incomplete:
        raise RuntimeError(f"terminal raster attempt cleanup failed: {incomplete!r}")


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
        _raise_for_incomplete_raster_cleanup(cleanup)
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
    except Exception:
        cleanup = _remove_owned_raster_file(image_path)
        _raise_for_incomplete_raster_cleanup(cleanup)
        return None
    if obj is None:
        cleanup = _remove_owned_raster_file(image_path)
        _raise_for_incomplete_raster_cleanup(cleanup)
        return None
    try:
        obj["pdf_raster_source_item_id"] = str(item_id)
        obj["pdf_raster_source_bbox_pdf"] = list(placement["source_bbox_pdf"])
        obj["pdf_raster_dpi"] = dpi
    except Exception:
        plane_cleanup = _remove_created_image_plane(obj, collection)
        file_cleanup = (
            _remove_owned_raster_file(image_path)
            if plane_cleanup.get("status") == "complete"
            else {"status": "not_attempted", "removed": []}
        )
        _raise_for_incomplete_raster_cleanup(plane_cleanup, file_cleanup)
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
            except Exception as cleanup_exc:
                cleanup_errors.append(f"object:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if mesh is not None:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"mesh:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if material_created and material is not None:
            try:
                bpy.data.materials.remove(material)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"material:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if image_created and image is not None:
            try:
                bpy.data.images.remove(image)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"image:{type(cleanup_exc).__name__}:{cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(
                "image plane construction and owned-artifact cleanup failed: "
                f"creation={type(exc).__name__}:{exc}; cleanup={cleanup_errors!r}"
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
    registry = getattr(getattr(bpy, "data", None), "objects", None)
    getter = getattr(registry, "get", None)
    for record in tuple(delivery_records or ()):
        if (
            int(record.get("page", 0) or 0) != int(page_number)
            or record.get("status") != "delivered"
        ):
            continue
        entity_ids = [str(value) for value in tuple(record.get("entity_ids") or ())]
        attempts = tuple(record.get("attempts") or ())
        delivered_attempt = next(
            (attempt for attempt in reversed(attempts) if attempt.get("status") == "delivered"),
            {},
        )
        prior_evidence = dict(delivered_attempt.get("evidence") or {})
        expected_locations = _delivery_expected_locations(prior_evidence, entity_ids)
        representation = str(record.get("final_representation") or "")
        requested_representation = str(
            record.get("requested_representation") or ""
        )
        expected_type = expected_types.get(representation)
        entity_proofs = []
        record_failures = []
        logical_zero_ink_claimed = not entity_ids and (
            record.get("zero_ink_delivery") is True
            or prior_evidence.get("zero_ink_delivery") is True
            or prior_evidence.get("proof_kind")
            == "positioned_zero_ink_delivery_v1"
        )
        logical_zero_ink_verified = False
        if logical_zero_ink_claimed:
            item_id = str(record.get("item_id") or "")
            manifests = getattr(
                provenance_opts,
                "_zero_ink_source_manifests",
                None,
            )
            expected_manifest = (
                manifests.get(item_id) if isinstance(manifests, dict) else None
            )
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
                    page_number=int(record.get("page", 0) or 0),
                    source_span_id=int(record.get("source_span_id", 0) or 0),
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
        for entity_id in entity_ids:
            obj = getter(entity_id) if callable(getter) else None
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
            entity_proofs.append(proof)
        if not entity_ids and not logical_zero_ink_claimed:
            record_failures.append("final_entity_identity_missing")
        final_proof = {
            "status": "failed" if record_failures else "verified",
            "page_number": int(page_number),
            "stack_offset_m": float(stack_offset_m),
            "representation": representation,
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
                if cleanup.get("status") == "complete" and isinstance(outcomes, dict):
                    outcomes.pop(item_id, None)
            if cleanup.get("status") == "complete":
                source_manifests = getattr(
                    provenance_opts,
                    "_zero_ink_source_manifests",
                    None,
                )
                if isinstance(source_manifests, dict):
                    source_manifests.pop(item_id, None)
            final_proof["cleanup"] = cleanup
            failure = {
                "item_id": item_id,
                "page": int(page_number),
                "failures": list(record_failures),
                "cleanup": cleanup,
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
                }.get(representation)
                if isinstance(counts, dict) and bucket:
                    counts[bucket] = max(0, int(counts.get(bucket, 0) or 0) - 1)
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
            total_stats["text_source_spans"] += len(page_data.text_items or [])
            total_stats["text_glyph_estimate"] += sum(
                len(str(getattr(item, "text", "") or ""))
                for item in (page_data.text_items or [])
            )
            total_stats["parts_bootstrap_text_items"].extend(page_data.text_items or [])
            model3d_all_text.extend(page_data.text_items or [])

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
                    page_data.text_items,
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
            text_count = max(0, int(text_count) - len(final_text_failures))
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
