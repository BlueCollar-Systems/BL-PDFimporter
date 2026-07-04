"""Mode-driven import orchestration for Blender PDF importer (BCS-ARCH-001)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from pdf_vector_importer import bl_info
from pdf_vector_importer.pdfcadcore.import_bounds import compute_import_bounds
from pdf_vector_importer.pdfcadcore.import_config import ImportConfig
from pdf_vector_importer.pdfcadcore.import_report import build_import_report
from .core.document import DocumentExtraction, ExtractionOptions, extract_document


@dataclass
class ImportRun:
    extraction: DocumentExtraction
    config: ImportConfig
    import_report_path: Optional[str] = None


def _pymupdf_version() -> str:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    return str(getattr(fitz, "__version__", "") or "")


def _importer_version() -> str:
    version = bl_info.get("version", "")
    if isinstance(version, (tuple, list)):
        return ".".join(str(part) for part in version)
    return str(version or "")


def _mode_config(mode: str) -> ImportConfig:
    """Dispatch a BCS-ARCH-001 mode name to the matching ImportConfig.

    The four valid modes are: auto, vector, raster, hybrid.
    No preset names are accepted; any other value raises ValueError.
    """
    key = (mode or "auto").strip().lower()
    if key == "auto":
        return ImportConfig.auto()
    if key == "vector":
        return ImportConfig.vector()
    if key == "raster":
        return ImportConfig.raster()
    if key == "hybrid":
        return ImportConfig.hybrid()
    raise ValueError(
        f"Unknown import mode: {mode!r}. "
        "Valid modes: auto, vector, raster, hybrid (BCS-ARCH-001)."
    )


def run_import(pdf_path: str, mode: str = "auto",
               overrides: Optional[Dict[str, Any]] = None) -> ImportRun:
    cfg = _mode_config(mode)
    for key, value in (overrides or {}).items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    opts = ExtractionOptions(
        pages=cfg.pages,
        scale=cfg.user_scale,
        flip_y=cfg.flip_y,
        import_text=cfg.import_text,
        import_images=not cfg.ignore_images,
        import_mode=cfg.import_mode,
        raster_fallback=cfg.raster_fallback,
        raster_dpi=cfg.raster_dpi,
        detect_arcs=cfg.detect_arcs,
        arc_fit_tol_mm=cfg.arc_fit_tol_mm,
    )

    extraction = extract_document(pdf_path, opts)
    return ImportRun(extraction=extraction, config=cfg)


def write_import_report(
    run: ImportRun,
    output_path: str,
    *,
    elapsed_ms: float = 0.0,
    performance_phases: Optional[Dict[str, float]] = None,
) -> str:
    """Emit bcs.import_report/1.1 JSON for headless Blender import runs."""
    extraction = run.extraction
    pages = extraction.pages
    page_data = [p.page_data for p in pages]
    bounds = None
    if page_data:
        bounds_obj = compute_import_bounds(page_data)
        if bounds_obj is not None:
            bounds = [round(v, 1) for v in bounds_obj.as_tuple()]

    text_items = [
        txt
        for page in pages
        for txt in (page.page_data.text_items or [])
    ]
    phases = dict(performance_phases or {})
    if elapsed_ms > 0 and "total_ms" not in phases:
        phases["total_ms"] = float(elapsed_ms)

    report = build_import_report(
        host_app="blender",
        host_version="headless",
        runtime_lang="python",
        runtime_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        importer_version=_importer_version(),
        pdf_path=extraction.pdf_path,
        mode=run.config.import_mode,
        pages=len(pages),
        primitive_count=extraction.primitive_count,
        text_count=extraction.text_count,
        layer_count=sum(len(page.page_data.layers or []) for page in pages),
        bbox=bounds,
        elapsed_ms=elapsed_ms,
        performance_phases=phases or None,
        fallback_used=any((p.resolved_mode or "") == "raster" for p in pages),
        fallback_reason=next(
            (p.resolved_reason for p in pages if (p.resolved_mode or "") == "raster"),
            None,
        ),
        pdf_engine_version=_pymupdf_version(),
        import_text=bool(run.config.import_text),
        text_mode=str(run.config.text_mode or "3d_text"),
        text_source_spans=len(text_items),
        text_glyph_estimate=sum(len(str(getattr(txt, "text", "") or "")) for txt in text_items),
        extra={"auto_mode": extraction.summary().get("auto_mode")},
    )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    report.write_json(output_path)
    run.import_report_path = output_path
    return output_path


def apply_uniform_scale(extraction: DocumentExtraction, factor: float) -> None:
    if factor <= 0:
        raise ValueError("Scale factor must be positive.")

    for page in extraction.pages:
        data = page.page_data
        data.width *= factor
        data.height *= factor

        for primitive in data.primitives:
            primitive.points = [(x * factor, y * factor) for x, y in (primitive.points or [])]
            if primitive.center:
                primitive.center = (primitive.center[0] * factor, primitive.center[1] * factor)
            if primitive.radius is not None:
                primitive.radius *= factor
            if primitive.bbox:
                x0, y0, x1, y1 = primitive.bbox
                primitive.bbox = (x0 * factor, y0 * factor, x1 * factor, y1 * factor)
            # NOTE: line_width is intentionally NOT scaled.
            # It is a paper-space stroke weight (mm); scaling it by the
            # geometry factor produces "sausage" strokes on scaled drawings.
            if primitive.area is not None:
                primitive.area *= factor * factor

        for txt in data.text_items:
            tx, ty = txt.insertion
            txt.insertion = (tx * factor, ty * factor)
            if txt.bbox:
                x0, y0, x1, y1 = txt.bbox
                txt.bbox = (x0 * factor, y0 * factor, x1 * factor, y1 * factor)
            txt.font_size *= factor

        for image in page.images:
            image.x_mm *= factor
            image.y_mm *= factor
            image.width_mm *= factor
            image.height_mm *= factor
