"""CLI for Blender PDF importer core pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .importer import apply_uniform_scale, run_import


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--strict-text-fidelity",
                        action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Preserve exact text spans (default from preset)")
    parser.add_argument("--hatch-mode", default=None,
                        choices=["import", "group", "skip"],
                        help="Hatch handling override")
    parser.add_argument("--arc-mode", default=None,
                        choices=["auto", "preserve", "rebuild", "polyline"],
                        help="Arc reconstruction mode")
    parser.add_argument("--cleanup-level", default=None,
                        choices=["conservative", "balanced", "aggressive"],
                        help="Geometry cleanup aggressiveness")
    parser.add_argument("--lineweight-mode", default=None,
                        choices=["ignore", "preserve", "group", "map_to_layers"],
                        help="Lineweight handling mode")
    parser.add_argument("--grouping-mode", default=None,
                        choices=[
                            "single", "per_page", "per_layer", "per_color",
                            "nested_page_layer", "nested_page_lineweight",
                        ],
                        help="Grouping strategy")
    parser.add_argument("--raster-dpi", type=int, default=None,
                        help="Raster rendering DPI for raster/hybrid modes")
    parser.add_argument("--no-raster-fallback", action="store_true",
                        help="Disable automatic raster fallback when vectors are absent")
    parser.add_argument("--reference-detected-mm", type=float, default=None,
                        help="Measured length in imported geometry (mm)")
    parser.add_argument("--reference-real-mm", type=float, default=None,
                        help="Real-world reference length (mm)")
    parser.add_argument("--no-text", action="store_true",
                        help="Skip text extraction")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip embedded image extraction")
    parser.add_argument("--no-arcs", action="store_true",
                        help="Skip arc reconstruction")
    parser.add_argument("--json", help="Write summary JSON")
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
    if args.strict_text_fidelity is not None:
        overrides["strict_text_fidelity"] = bool(args.strict_text_fidelity)
    if args.hatch_mode is not None:
        overrides["hatch_mode"] = args.hatch_mode
    if args.arc_mode is not None:
        overrides["arc_mode"] = args.arc_mode
    if args.cleanup_level is not None:
        overrides["cleanup_level"] = args.cleanup_level
    if args.lineweight_mode is not None:
        overrides["lineweight_mode"] = args.lineweight_mode
    if args.grouping_mode is not None:
        overrides["grouping_mode"] = args.grouping_mode
    if args.raster_dpi is not None:
        overrides["raster_dpi"] = args.raster_dpi
    if args.no_raster_fallback:
        overrides["raster_fallback"] = False
    if args.no_text:
        overrides["import_text"] = False
    if args.no_images:
        overrides["ignore_images"] = True
    if args.no_arcs:
        overrides["detect_arcs"] = False

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
