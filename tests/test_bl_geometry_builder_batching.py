from __future__ import annotations

import ast
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


GEOMETRY_BUILDER = (
    Path(__file__).resolve().parents[1]
    / "pdf_vector_importer"
    / "bl_geometry_builder.py"
)


class TestBlenderGeometryBuilderBatching(unittest.TestCase):
    def setUp(self) -> None:
        self.source = GEOMETRY_BUILDER.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_open_curve_batching_is_default_enabled(self) -> None:
        self.assertIn('config.get("batch_open_curves", True)', self.source)
        self.assertIn("open_curve_batches", self.source)

    def test_batches_flush_to_multi_spline_curve_objects(self) -> None:
        self.assertIn("_flush_open_curve_batches()", self.source)
        self.assertIn("_create_multi_poly_curve(", self.source)
        self.assertIn('"batched_curve_primitives"', self.source)

    def test_direct_poly_curve_path_still_exists_for_closed_geometry(self) -> None:
        names = {
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_create_poly_curve", names)
        self.assertIn("_draw_stroked_polyline", names)
        self.assertIn("_batch_width_key", names)

    def test_unknown_primitive_approximation_is_verified_and_reportable(self) -> None:
        self.assertNotIn("Unknown type — best effort as polyline", self.source)
        self.assertIn('"geometry_delivery_issues": []', self.source)
        self.assertIn('"unknown_normalized_primitive_type"', self.source)
        self.assertIn('"source_points_preserved"', self.source)

    def test_unknown_primitive_delivery_record_is_item_bound(self) -> None:
        bpy = sys.modules.setdefault("bpy", types.SimpleNamespace())
        if not hasattr(bpy, "types"):
            bpy.types = types.SimpleNamespace()
        for name in ("Collection", "Material", "Object"):
            if not hasattr(bpy.types, name):
                setattr(bpy.types, name, object)
        sys.modules.setdefault("bmesh", types.SimpleNamespace())

        builder = importlib.import_module("pdf_vector_importer.bl_geometry_builder")
        primitives = importlib.import_module("pdf_vector_importer.pdfcadcore.primitives")
        primitive = primitives.Primitive(
            id=17,
            type="bezier",
            points=[(1.0, 2.0), (3.0, 4.0)],
            closed=False,
        )
        page = primitives.PageData(
            page_number=3,
            width=100.0,
            height=100.0,
            primitives=[primitive],
        )
        with (
            patch.object(builder, "_resolve_collection", return_value=object()),
            patch.object(builder, "_get_or_create_material", return_value=object()),
            patch.object(builder, "_draw_stroked_polyline", return_value=1),
        ):
            stats = builder.build_page(page, object())

        self.assertEqual(stats["curves"], 1)
        self.assertEqual(stats["geometry_delivery_issues"], [{
            "page": 3,
            "primitive_id": 17,
            "requested_type": "geometry",
            "source_primitive_type": "bezier",
            "delivered_type": "polyline_geometry",
            "status": "verified",
            "reason": "unknown_normalized_primitive_type",
            "verification": "source_points_preserved",
        }])


if __name__ == "__main__":
    unittest.main(verbosity=2)
