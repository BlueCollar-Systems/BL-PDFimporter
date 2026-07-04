from __future__ import annotations

import ast
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
