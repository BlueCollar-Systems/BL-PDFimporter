from __future__ import annotations

from pdf_vector_importer.pdfcadcore.primitive_extractor import (
    _merge_stacked_fractions,
)
from pdf_vector_importer.pdfcadcore.primitives import (
    NormalizedText,
    next_id,
    reset_ids,
)


def _item(item_id: int, text: str, x: float, y: float) -> NormalizedText:
    return NormalizedText(
        id=item_id,
        text=text,
        normalized=text,
        insertion=(x, y),
        bbox=(x - 0.5, y - 0.5, x + 0.5, y + 0.5),
        font_size=3.0,
        page_number=1,
    )


def test_semantic_fraction_merge_does_not_consume_physical_id_allocator() -> None:
    reset_ids()
    try:
        merged = _merge_stacked_fractions(
            [
                _item(10, "1", 4.0, 4.0),
                _item(11, "/", 4.0, 3.0),
                _item(12, "4", 4.0, 2.0),
            ]
        )

        assert [item.text for item in merged] == ["1/4"]
        assert merged[0].id == 11
        assert next_id() == 1
    finally:
        reset_ids()
