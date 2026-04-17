"""Batch CLI for Blender PDF importer core pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .importer import run_import


def _collect_pdfs(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(p for p in root.rglob("*.pdf") if p.is_file())
    return sorted(p for p in root.glob("*.pdf") if p.is_file())


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for BL batch CLI (BCS-ARCH-001 Rule 5 sweep).

    Only mode, pages, recursive, summary-dir, and json are user-facing.
    Quality-tier flags do not exist on this entry point — consolidated
    defaults apply universally.
    """
    p = argparse.ArgumentParser(description="Batch-parse PDF vectors for Blender workflows.")
    p.add_argument("input_dir", help="Directory containing PDF files")
    p.add_argument("--mode", default="auto",
                   choices=["auto", "vector", "raster", "hybrid"],
                   help="Import mode (BCS-ARCH-001)")
    p.add_argument("--pages", default="all", help="Page spec (default: all)")
    p.add_argument("--recursive", action="store_true", help="Include subfolders")
    p.add_argument("--summary-dir", default=None,
                   help="Optional directory to write per-file summaries")
    p.add_argument("--json", default=None, help="Write aggregate JSON report")
    p.add_argument("--verbose", action="store_true",
                   help="Print verbose progress")
    return p


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.input_dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Input directory not found: {root}")

    pdfs = _collect_pdfs(root, recursive=args.recursive)
    if not pdfs:
        raise SystemExit(f"No PDF files found under: {root}")

    summary_dir = Path(args.summary_dir).expanduser().resolve() if args.summary_dir else None
    if summary_dir is not None:
        summary_dir.mkdir(parents=True, exist_ok=True)

    aggregate = {"root": str(root), "total": len(pdfs), "passed": 0, "failed": 0, "results": []}

    for pdf in pdfs:
        overrides = {"pages": args.pages}
        try:
            run = run_import(str(pdf), mode=args.mode, overrides=overrides)
            summary = run.extraction.summary()
            aggregate["passed"] += 1
            entry = {"pdf": str(pdf), "status": "PASS", "summary": summary}
            if summary_dir is not None:
                out_file = summary_dir / f"{pdf.stem}.summary.json"
                out_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                entry["summary_json"] = str(out_file)
            aggregate["results"].append(entry)
        except Exception as exc:  # noqa: BLE001
            aggregate["failed"] += 1
            aggregate["results"].append({"pdf": str(pdf), "status": "FAIL", "error": str(exc)})

    print(json.dumps({k: v for k, v in aggregate.items() if k != "results"}, indent=2))

    if args.json:
        out = Path(args.json).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
        print(f"Wrote report: {out}")

    return 0 if aggregate["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

