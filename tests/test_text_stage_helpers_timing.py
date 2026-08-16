# -*- coding: utf-8 -*-
"""text_ms sub-stage timers: present, accounted, and reported.

On `1011 (1 OF 2) - Rev 0.pdf` the Blender text stage is 88-90% of the importer
clock (text 57.2 s of 63.2 s; glyphs 25.4 s of 28.7 s) and had no sub-stages, so the
next cut was a guess. These tests pin that the timers exist, that they wrap the
boundaries that matter (font load, object creation, every per-item
`view_layer.update()`, bbox fit, affine carrier, verification, cleanup, record,
converted-page work), and that they reach the report as `performance.helpers_ms`
-- never as `phases`, where they would be summed as siblings of `text_ms`.
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace(
        Collection=object, Material=object, Object=object, VectorFont=object
    )
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_text_builder  # noqa: E402
from pdf_vector_importer.bl_import_engine import write_import_report  # noqa: E402

BUILDER_SOURCE = (ROOT / "pdf_vector_importer" / "bl_text_builder.py").read_text(
    encoding="utf-8"
)


class TestTextStageAccumulator(unittest.TestCase):
    def setUp(self) -> None:
        bl_text_builder.reset_text_stage_timings()

    def tearDown(self) -> None:
        bl_text_builder.reset_text_stage_timings()

    def test_stage_accumulates_ms_and_counts_across_calls(self) -> None:
        for _ in range(3):
            with bl_text_builder._text_stage("view_layer_update"):
                time.sleep(0.002)
        with bl_text_builder._text_stage("verify"):
            pass
        timings = bl_text_builder.text_stage_timings()
        self.assertGreaterEqual(timings["text_view_layer_update_ms"], 5.0)
        self.assertEqual(timings["text_view_layer_update_count"], 3.0)
        self.assertEqual(timings["text_verify_count"], 1.0)
        self.assertGreaterEqual(timings["text_verify_ms"], 0.0)
        # Every value is a float so the shared writer's float() coercion holds.
        self.assertTrue(all(isinstance(v, float) for v in timings.values()))

    def test_stage_records_time_even_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with bl_text_builder._text_stage("affine"):
                raise RuntimeError("host said no")
        timings = bl_text_builder.text_stage_timings()
        self.assertEqual(timings["text_affine_count"], 1.0)

    def test_reset_clears_everything(self) -> None:
        with bl_text_builder._text_stage("fit_bbox"):
            pass
        bl_text_builder.reset_text_stage_timings()
        self.assertEqual(bl_text_builder.text_stage_timings(), {})

    def test_build_all_text_resets_the_accumulator_per_page(self) -> None:
        # Source-level pin: the reset happens inside build_all_text before any item.
        tree = ast.parse(BUILDER_SOURCE)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "build_all_text"
        )
        calls = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "reset_text_stage_timings"
        ]
        self.assertEqual(len(calls), 1)


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _stage_name_of_with(node: ast.With):
    for item in node.items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_text_stage"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        ):
            return call.args[0].value
    return None


def _enclosing_stage(fn: ast.FunctionDef, target: ast.AST):
    """Innermost `_text_stage(...)` name whose With body contains `target`."""
    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    node = target
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.With):
            name = _stage_name_of_with(node)
            if name is not None:
                return name
    return None


class TestTextStageBoundaries(unittest.TestCase):
    """The timers must wrap the real boundaries -- checked against the source so a
    refactor cannot silently drop one and leave a stage looking free."""

    tree = ast.parse(BUILDER_SOURCE)

    def test_every_view_layer_update_in_create_font_candidate_is_timed(self) -> None:
        fn = _function(self.tree, "_create_font_candidate")
        updates = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "view_layer"
        ]
        # Negative control: the per-item depsgraph passes are really there.
        self.assertEqual(len(updates), 3, "expected the three per-item view_layer.update() calls")
        for call in updates:
            self.assertEqual(_enclosing_stage(fn, call), "view_layer_update")

    def test_font_load_fit_and_affine_are_timed_in_create_font_candidate(self) -> None:
        fn = _function(self.tree, "_create_font_candidate")
        expected = {
            "_load_exact_font": "font_load",
            "_fit_text_to_bbox": "fit_bbox",
            "_apply_target_quad_affine": "affine",
        }
        seen = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in expected:
                    seen[node.func.id] = _enclosing_stage(fn, node)
        self.assertEqual(seen, expected)

    def test_verification_cleanup_record_and_converted_work_are_timed(self) -> None:
        checks = {
            ("_attempt_native_font", "_verify_font_candidate"): "verify",
            ("_deliver_text_item", "_finish_text_item_delivery"): "delivery_record",
            ("_deliver_text_item", "_cleanup_attempt"): "cleanup",
            ("build_all_text", "_flush_converted_page_jobs"): "converted_flush",
        }
        for (fn_name, callee), stage in checks.items():
            fn = _function(self.tree, fn_name)
            calls = [
                node for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == callee
            ]
            self.assertTrue(calls, f"{callee} not called in {fn_name}")
            for call in calls:
                self.assertEqual(_enclosing_stage(fn, call), stage, f"{fn_name}->{callee}")

        fn = _function(self.tree, "build_all_text")
        create_sources = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_sources"
        ]
        self.assertEqual(len(create_sources), 1)
        self.assertEqual(_enclosing_stage(fn, create_sources[0]), "converted_create_sources")


class TestHelpersReachTheReport(unittest.TestCase):
    def test_helper_timings_land_in_helpers_ms_not_phases(self) -> None:
        stats = {
            "pages_imported": 1,
            "primitives": 5,
            "text_items": 2,
            "collections": 1,
            "elapsed": 1.5,
            "performance_phases": {"text_ms": 1000.0, "total_ms": 1500.0},
            "helper_timings_ms": {
                "text_view_layer_update_ms": 600.0,
                "text_view_layer_update_count": 6.0,
                "text_verify_ms": 200.0,
                "text_verify_count": 2.0,
            },
        }
        with tempfile.TemporaryDirectory(prefix="bl_text_helpers_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            with patch(
                "pdf_vector_importer.bl_import_engine._pymupdf_version",
                return_value="",
            ):
                write_import_report(
                    str(Path(tmp) / "sample.pdf"),
                    {"import_text": True, "text_mode": "text"},
                    stats,
                    import_mode="auto",
                    output_path=str(report_path),
                )
            data = json.loads(report_path.read_text(encoding="utf-8"))
        perf = data["performance"]
        self.assertEqual(perf["phases"]["text_ms"], 1000.0)
        self.assertNotIn("text_view_layer_update_ms", perf["phases"])
        self.assertEqual(perf["helpers_ms"]["text_view_layer_update_ms"], 600.0)
        self.assertEqual(perf["helpers_ms"]["text_view_layer_update_count"], 6.0)
        self.assertEqual(perf["helpers_ms"]["text_verify_ms"], 200.0)

    def test_no_helpers_means_no_helpers_block(self) -> None:
        stats = {
            "pages_imported": 1, "primitives": 5, "text_items": 0, "collections": 1,
            "elapsed": 0.5, "performance_phases": {"total_ms": 500.0},
        }
        with tempfile.TemporaryDirectory(prefix="bl_text_helpers_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            with patch(
                "pdf_vector_importer.bl_import_engine._pymupdf_version",
                return_value="",
            ):
                write_import_report(
                    str(Path(tmp) / "sample.pdf"), {"import_text": False}, stats,
                    import_mode="auto", output_path=str(report_path),
                )
            data = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertNotIn("helpers_ms", data["performance"])


if __name__ == "__main__":
    unittest.main()
