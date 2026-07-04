"""
Regression guardrail: apply_uniform_scale must NOT scale line_width.

Root cause (BL-1): importer.py:161 was scaling primitive.line_width by the
geometry factor, turning a 0.18 mm hairline into a 2.9 mm sausage stroke on
a 1/16 scale shop drawing.  line_width is paper-space; it must stay fixed.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_IMPORTER = (
    Path(__file__).resolve().parents[1]
    / "blender_pdf_vector_importer"
    / "importer.py"
)


class TestApplyUniformScaleLineWidth(unittest.TestCase):

    def _source(self) -> str:
        return _IMPORTER.read_text(encoding="utf-8")

    def test_line_width_not_multiplied_by_factor(self) -> None:
        """line_width *= factor must not appear in apply_uniform_scale."""
        src = self._source()
        self.assertNotIn(
            "primitive.line_width *= factor",
            src,
            "line_width must not be scaled by geometry factor (paper-space property)",
        )

    def test_apply_uniform_scale_exists(self) -> None:
        """apply_uniform_scale must still exist (function was not removed)."""
        src = self._source()
        tree = ast.parse(src)
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("apply_uniform_scale", names,
                      "apply_uniform_scale function must still exist")

    def test_geometry_fields_still_scaled(self) -> None:
        """Geometric coordinates must still be scaled (regression check)."""
        src = self._source()
        self.assertIn("primitive.radius *= factor", src,
                      "primitive.radius must still be scaled by factor")
        self.assertIn("primitive.points = [(x * factor", src,
                      "primitive.points must still be scaled by factor")

    def test_paper_space_comment_present(self) -> None:
        """A comment explaining WHY line_width is exempt must be present."""
        src = self._source()
        self.assertIn("paper-space", src,
                      "A comment explaining line_width paper-space exemption must exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
