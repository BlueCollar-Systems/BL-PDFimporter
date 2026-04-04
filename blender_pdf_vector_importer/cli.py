"""CLI for Blender PDF importer core pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .importer import run_import


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse PDF vectors for Blender import.")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--preset", default="general",
                        choices=["fast", "general", "technical", "shop", "max"],
                        help="Import preset")
    parser.add_argument("--pages", default=None,
                        help="Page spec: 1,3-5,all")
    parser.add_argument("--scale", type=float, default=None,
                        help="Additional scale multiplier")
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
    if args.no_text:
        overrides["import_text"] = False
        overrides["text_mode"] = "none"
    if args.no_images:
        overrides["ignore_images"] = True
    if args.no_arcs:
        overrides["detect_arcs"] = False

    run = run_import(args.pdf, preset=args.preset, overrides=overrides)
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
