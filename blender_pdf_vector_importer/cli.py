"""CLI for Blender PDF importer core pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .importer import apply_uniform_scale, run_import


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
    parser.add_argument("--verbose", action="store_true",
                        help="Print verbose progress")
    return parser


def main() -> int:
    args = build_parser().parse_args()
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

    run = run_import(args.pdf, mode=args.mode, overrides=overrides)
    if args.reference_detected_mm and args.reference_real_mm:
        if args.reference_detected_mm <= 0:
            raise SystemExit("--reference-detected-mm must be > 0")
        scale_factor = args.reference_real_mm / args.reference_detected_mm
        apply_uniform_scale(run.extraction, scale_factor)
    summary = run.extraction.summary()

    print(json.dumps(summary, indent=2))

    if args.json:
        out_path = Path(args.json).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Wrote summary: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
