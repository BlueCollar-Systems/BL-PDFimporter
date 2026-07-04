"""CLI for Blender PDF importer core pipeline."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .importer import apply_uniform_scale, run_import, write_import_report


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for BL CLI (BCS-ARCH-001 Rule 5 sweep).

    Only mode, text, import-text, pages, scale, json, and verbose are
    user-facing. Quality-tier flags (--hatch-mode, --arc-mode,
    --cleanup-level, --lineweight-mode, --raster-dpi,
    --strict-text-fidelity, --no-arcs, --no-raster-fallback,
    --grouping-mode) have been removed — their consolidated defaults
    apply universally.
    """
    parser = argparse.ArgumentParser(description="Parse PDF vectors for Blender import.")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "vector", "raster", "hybrid"],
                        help="Import mode (BCS-ARCH-001)")
    parser.add_argument("--pages", default=None,
                        help="Page spec: 1,3-5,all")
    parser.add_argument("--scale", type=float, default=None,
                        help="Additional scale multiplier")
    parser.add_argument("--text-mode", default=None,
                        choices=["labels", "3d_text", "glyphs", "geometry"],
                        help="Text handling (orthogonal to --mode)")
    parser.add_argument("--import-text",
                        action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Import text from the PDF (--no-import-text to skip)")
    parser.add_argument("--reference-detected-mm", type=float, default=None,
                        help="Measured length in imported geometry (mm)")
    parser.add_argument("--reference-real-mm", type=float, default=None,
                        help="Real-world reference length (mm)")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip embedded image extraction")
    parser.add_argument("--json", help="Write summary JSON")
    parser.add_argument("--import-report", help="Write bcs.import_report/1.1 JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="Print verbose progress")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.is_file():
        from pdfcadcore.cli_error_copy import cli_error

        print(cli_error("file_not_found", path=str(pdf_path)), file=sys.stderr)
        return 1

    from pdfcadcore.fitz_loader import PdfOpenError, safe_open

    try:
        doc = safe_open(str(pdf_path))
        doc.close()
    except PdfOpenError as exc:
        from pdfcadcore.cli_error_copy import cli_error

        print(cli_error("not_a_pdf", message=str(exc)), file=sys.stderr)
        return 1

    overrides = {}
    if args.pages is not None:
        overrides["pages"] = args.pages
    if args.scale is not None:
        overrides["user_scale"] = args.scale
    if args.text_mode is not None:
        overrides["text_mode"] = args.text_mode
        overrides["import_text"] = True
    if args.import_text is not None:
        overrides["import_text"] = bool(args.import_text)
    if args.no_images:
        overrides["ignore_images"] = True

    t0 = time.perf_counter()
    run = run_import(args.pdf, mode=args.mode, overrides=overrides)
    run_import_ms = (time.perf_counter() - t0) * 1000.0
    if args.reference_detected_mm and args.reference_real_mm:
        if args.reference_detected_mm <= 0:
            from pdfcadcore.cli_error_copy import cli_error

            print(cli_error("reference_detected_invalid"), file=sys.stderr)
            return 1
        scale_factor = args.reference_real_mm / args.reference_detected_mm
        apply_uniform_scale(run.extraction, scale_factor)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    summary = run.extraction.summary()
    report_path = None
    if args.import_report:
        report_path = Path(args.import_report).expanduser().resolve()
    elif args.json:
        summary_path = Path(args.json).expanduser().resolve()
        report_path = summary_path.with_name(f"{summary_path.stem}_import_report.json")
    if report_path is not None:
        write_import_report(
            run,
            str(report_path),
            elapsed_ms=elapsed_ms,
            performance_phases={
                "run_import_ms": run_import_ms,
                "total_ms": elapsed_ms,
            },
        )
        summary["import_report_path"] = str(report_path)

    print(json.dumps(summary, indent=2))

    if args.json:
        out_path = Path(args.json).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Wrote summary: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
