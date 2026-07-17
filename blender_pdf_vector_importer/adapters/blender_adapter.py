"""Deprecated API compatibility wrapper for the shipping Blender importer.

This module intentionally owns no extraction or Blender object builders.  Keeping
one host delivery path prevents the historical adapter from bypassing exact-font,
fallback-evidence, packed-asset, rollback, and import-report guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from pdf_vector_importer.text_delivery import normalize_representation


@dataclass(frozen=True)
class BlenderImportOptions:
    pages: Optional[str] = None
    import_text: bool = True
    text_mode: str = "3d_text"
    import_images: bool = True
    group_by_layer: bool = True
    group_by_color: bool = True


@dataclass(frozen=True)
class BlenderImportResult:
    """Compatibility result backed by the shipping engine's verified statistics."""

    stats: Dict[str, Any]

    def summary(self) -> Dict[str, Any]:
        stats = self.stats
        return {
            "pages": int(stats.get("pages_imported", stats.get("pages", 0)) or 0),
            "primitives": int(stats.get("primitives", 0) or 0),
            "text_items": int(stats.get("text_items", 0) or 0),
            "images": int(stats.get("images", 0) or 0),
            "text_delivery_failed_items": int(
                stats.get("text_delivery_failed_items", 0) or 0
            ),
            "text_delivery_fallback_items": int(
                stats.get("text_delivery_fallback_items", 0) or 0
            ),
            "raster_delivery_failures": list(
                stats.get("raster_delivery_failures") or []
            ),
            "import_report_path": str(stats.get("import_report_path") or ""),
        }


def import_into_blender(
    pdf_path: str,
    mode: str = "auto",
    options: Optional[BlenderImportOptions] = None,
    *,
    context=None,
) -> BlenderImportResult:
    """Route the historical API through the only verified host import engine."""
    try:
        import bpy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - Blender runtime only
        raise RuntimeError("Blender Python API (bpy) is required for this adapter.") from exc

    opts = options or BlenderImportOptions()
    if not opts.group_by_layer:
        raise ValueError(
            "Source layer preservation cannot be disabled. The deprecated adapter's "
            "independent ungrouped builder was removed so it cannot bypass verified "
            "delivery."
        )

    requested_text_mode = normalize_representation(opts.text_mode)
    config: Dict[str, Any] = {
        "mode": str(mode or "auto").strip().lower(),
        "import_text": bool(opts.import_text),
        "text_mode": requested_text_mode,
        "ignore_images": not bool(opts.import_images),
        "group_by_color": bool(opts.group_by_color),
        "strict_text_fidelity": True,
    }
    if opts.pages and str(opts.pages).strip():
        config["pages"] = str(opts.pages).strip()

    from pdf_vector_importer import bl_import_engine

    stats = dict(
        bl_import_engine.import_pdf(
            str(pdf_path),
            config=config,
            context=context,
        )
    )
    report_error = str(stats.get("import_report_error") or "").strip()
    if report_error:
        raise RuntimeError(f"Import report could not be written: {report_error}")
    cleanup_error = str(stats.get("temp_cleanup_error") or "").strip()
    if cleanup_error:
        raise RuntimeError(f"Owned temporary files could not be cleaned: {cleanup_error}")
    return BlenderImportResult(stats=stats)


__all__ = ["BlenderImportOptions", "BlenderImportResult", "import_into_blender"]
