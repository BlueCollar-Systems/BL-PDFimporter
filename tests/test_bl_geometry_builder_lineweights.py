from __future__ import annotations

import ast
import unittest
from pathlib import Path


GEOMETRY_BUILDER = (
    Path(__file__).resolve().parents[1]
    / "pdf_vector_importer"
    / "bl_geometry_builder.py"
)


class TestBlenderGeometryBuilderLineweights(unittest.TestCase):
    def test_lineweight_uses_radius_scale_and_thin_hairline_floor(self) -> None:
        source = GEOMETRY_BUILDER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assignments[target.id] = ast.get_source_segment(source, node.value)

        self.assertEqual(assignments.get("_LINEWIDTH_SCALE"), "MM_TO_M * 0.5")
        self.assertEqual(assignments.get("_MIN_BEVEL_DEPTH"), "0.0000125")
        self.assertEqual(assignments.get("_DEFAULT_HAIRLINE_BEVEL_DEPTH"), "0.000025")
        self.assertGreaterEqual(source.count("_line_bevel_depth(line_width)"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
