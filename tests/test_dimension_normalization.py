from __future__ import annotations

import unittest

from pdf_vector_importer.pdfcadcore.primitive_extractor import (
    _extract_text,
    _normalize_dimension_text,
)


class _FakePage:
    def __init__(self, text_dict):
        self._text_dict = text_dict

    def get_text(self, mode: str):
        if mode != "dict":
            raise ValueError("Expected dict text mode")
        return self._text_dict


class TestDimensionNormalization(unittest.TestCase):
    def test_compact_feet_inches_fraction(self) -> None:
        self.assertEqual(_normalize_dimension_text("6'-91516", aggressive=False), "6'-9 15/16")
        self.assertEqual(_normalize_dimension_text("1'-41516", aggressive=False), "1'-4 15/16")
        self.assertEqual(_normalize_dimension_text("2'-31516", aggressive=False), "2'-3 15/16")

    def test_parenthesized_nominal_size_rewrite(self) -> None:
        raw = "(PIPE1-12/ STD x 13'-338/ )"
        self.assertEqual(
            _normalize_dimension_text(raw, aggressive=False),
            "(PIPE1-1/2 STD x 13'-3 3/8 )",
        )

    def test_ocr_noise_cleanup_in_dimension_tokens(self) -> None:
        raw = "6'-9 I5//I6(PIPEI-I/2 STD x I3'-3 3/8)"
        self.assertEqual(
            _normalize_dimension_text(raw, aggressive=False),
            "6'-9 15/16 (PIPE1-1/2 STD x 13'-3 3/8)",
        )
        self.assertEqual(
            _normalize_dimension_text("1'-O 3/4", aggressive=False),
            "1'-0 3/4",
        )
        self.assertEqual(
            _normalize_dimension_text("I'-0 3/4", aggressive=False),
            "1'-0 3/4",
        )
        self.assertEqual(
            _normalize_dimension_text("PIPEI-I/2STD", aggressive=False),
            "PIPE1-1/2STD",
        )

    def test_compact_hyphen_fractions_without_slash(self) -> None:
        self.assertEqual(
            _normalize_dimension_text("8 - 1516 Ø HOLES", aggressive=False),
            "8 - 15/16 Ø HOLES",
        )
        self.assertEqual(
            _normalize_dimension_text("3-716", aggressive=False),
            "3-7/16",
        )
        # Guardrail: non-numeric left-hand IDs must remain untouched.
        self.assertEqual(
            _normalize_dimension_text("DWG A-1516 REV 2", aggressive=False),
            "DWG A-1516 REV 2",
        )

    def test_preserve_non_dimension_tokens(self) -> None:
        self.assertEqual(
            _normalize_dimension_text("DATE : 03/15/2024", aggressive=False),
            "DATE : 03/15/2024",
        )
        self.assertEqual(
            _normalize_dimension_text("DWG A-1516 REV 2", aggressive=False),
            "DWG A-1516 REV 2",
        )
        self.assertEqual(
            _normalize_dimension_text("WATERPROOF(ING)", aggressive=False),
            "WATERPROOF(ING)",
        )
        self.assertEqual(
            _normalize_dimension_text("ALVORD TX — GARDEN MAP — FINAL", aggressive=False),
            "ALVORD TX — GARDEN MAP — FINAL",
        )

    def test_strict_text_fidelity_still_normalizes_compact_dimensions(self) -> None:
        page = _FakePage(
            {
                "blocks": [
                    {
                        "type": 0,
                        "lines": [
                            {
                                "bbox": (0.0, 0.0, 200.0, 20.0),
                                "dir": (1.0, 0.0),
                                "spans": [
                                    {
                                        "text": "3'-1012/",
                                        "origin": (0.0, 10.0),
                                        "size": 10.0,
                                        "font": "Arial",
                                    }
                                ],
                            },
                            {
                                "bbox": (0.0, 30.0, 240.0, 50.0),
                                "dir": (1.0, 0.0),
                                "spans": [
                                    {
                                        "text": "6'-91516 (PIPE1-12/ STD x 13'-338/)",
                                        "origin": (0.0, 40.0),
                                        "size": 10.0,
                                        "font": "Arial",
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        )

        items = _extract_text(
            page=page,
            page_h=1000.0,
            page_num=1,
            flip_y=True,
            scale=1.0,
            strict_text_fidelity=True,
        )

        self.assertEqual(items[0].text, "3'-10 1/2")
        self.assertEqual(
            items[1].text,
            "6'-9 15/16 (PIPE1-1/2 STD x 13'-3 3/8)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
