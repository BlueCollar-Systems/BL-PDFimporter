from __future__ import annotations

import pytest

from pdf_vector_importer.pdfcadcore.primitives import NormalizedText, PageData
from pdf_vector_importer.pdfcadcore.resolved_scale import resolve_page_scale


def _page(*values: str) -> PageData:
    return PageData(
        page_number=1,
        width=1000.0,
        height=700.0,
        text_items=[
            NormalizedText(
                id=index,
                text=value,
                normalized=value.upper(),
                insertion=(900.0, 100.0),
            )
            for index, value in enumerate(values, start=1)
        ],
    )


@pytest.mark.parametrize(
    "temporal_text",
    (
        "5/27/2016 9:10:47 AM",
        "9:10:47 AM",
        "ISSUED 5-27-2016",
        "PRINTED 9:10 AM",
    ),
)
def test_titleblock_dates_and_times_are_not_scale_evidence(temporal_text: str) -> None:
    resolved = resolve_page_scale(_page(temporal_text))

    assert resolved.factor == pytest.approx(1.0)
    assert resolved.source == "default"
    assert resolved.confidence == pytest.approx(0.0)


def test_temporal_noise_does_not_hide_valid_architectural_scale() -> None:
    resolved = resolve_page_scale(
        _page("5/27/2016 9:10:47 AM", 'SCALE: 1/4" = 1\'-0"')
    )

    assert resolved.factor == pytest.approx(48.0)
    assert resolved.source == "titleblock"


def test_temporal_noise_does_not_hide_valid_ratio_scale() -> None:
    resolved = resolve_page_scale(_page("9:10:47 AM", "SCALE 1:50"))

    assert resolved.factor == pytest.approx(50.0)
    assert resolved.source == "titleblock"
