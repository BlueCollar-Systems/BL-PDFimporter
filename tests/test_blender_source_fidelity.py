"""Owner-directive gates for Blender source text, transforms, and fonts."""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from pdf_vector_importer.pdfcadcore import embedded_fonts
from pdf_vector_importer.pdfcadcore.embedded_fonts import EmbeddedFontCatalog
from pdf_vector_importer.pdfcadcore.primitive_extractor import (
    _extract_text,
    _page_rotation_transform,
    _transform_pdf_point,
    extract_page,
)

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore


def _find_welding_pdf(filename: str) -> Path | None:
    candidates = []
    configured = os.environ.get("BCS_PDF_TEST_FILES", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    corpus = os.environ.get("BCS_CORPUS_ROOT", "").strip()
    if corpus:
        corpus_root = Path(corpus).expanduser()
        candidates.extend(
            (
                corpus_root,
                corpus_root / "PDFTest Files",
                corpus_root / "pdfs",
                corpus_root / "source-pdfs",
            )
        )
    repository = Path(__file__).resolve().parents[1]
    candidates.extend(
        (
            repository / "tests" / "fixtures",
            repository / "test-data",
            Path.home() / "Desktop" / "PDFTest Files",
        )
    )
    seen = set()
    for root in candidates:
        key = str(root.resolve(strict=False)).lower()
        if key in seen:
            continue
        seen.add(key)
        path = root / filename
        if path.is_file():
            return path
    return None


def test_welding_pdf_discovery_uses_configured_portable_root(monkeypatch, tmp_path):
    fixture = tmp_path / "AWSWeldSymbolchart.pdf"
    fixture.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setenv("BCS_PDF_TEST_FILES", str(tmp_path))

    assert _find_welding_pdf(fixture.name) == fixture


def test_page_font_inventory_failure_is_bound_to_each_span_and_not_absence():
    class InventoryFailure(RuntimeError):
        pass

    class Page:
        @staticmethod
        def get_texttrace():
            return []

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            raise InventoryFailure("transient parent inventory failure")

    catalog = EmbeddedFontCatalog.from_page(Page(), page_number=9)
    failure = catalog.failure_for_span("Exact-Font-Name")

    assert failure.page_number == 9
    assert failure.span_font_name == "Exact-Font-Name"
    assert failure.reason == "page_font_inventory_failed"
    assert failure.error_type == "InventoryFailure"
    assert "transient parent inventory failure" in failure.detail


def test_text_trace_failure_poisoning_is_page_bound_and_cannot_be_laundered_as_absence():
    class TraceFailure(RuntimeError):
        pass

    class Page:
        parent = None

        @staticmethod
        def get_texttrace():
            raise TraceFailure("trace inventory unavailable")

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            return []

    catalog = EmbeddedFontCatalog.from_page(Page(), page_number=4)
    failure = catalog.failure_for_span("Exact-Font-Name")

    assert catalog.assets == ()
    assert failure.page_number == 4
    assert failure.span_font_name == "Exact-Font-Name"
    assert failure.reason == "page_text_trace_inventory_failed"
    assert failure.error_type == "TraceFailure"
    assert "trace inventory unavailable" in failure.detail


def test_one_malformed_font_inventory_record_invalidates_the_whole_page():
    class Page:
        parent = object()

        @staticmethod
        def get_texttrace():
            return []

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            return [("not-an-xref",), (7, "ttf", "Type0", "GoodFont", "F1", "")]

    catalog = EmbeddedFontCatalog.from_page(Page(), page_number=6)
    failure = catalog.failure_for_span("GoodFont")

    assert catalog.assets == ()
    assert failure.page_number == 6
    assert failure.span_font_name == "GoodFont"
    assert failure.reason == "invalid_page_font_record"


def test_font_without_existing_cmap_or_pdf_unicode_map_is_rejected():
    with pytest.raises(ValueError, match="Unicode map unavailable"):
        embedded_fonts._usable_font(b"not-an-sfnt", "ttf", "ExactFont", {})


def test_trace_failure_never_uses_an_unbound_source_cmap_as_pdf_glyph_proof(monkeypatch):
    class Document:
        @staticmethod
        def extract_font(_xref):
            return "ExactFont", "ttf", "Type0", b"source-font"

    class Page:
        parent = Document()

        @staticmethod
        def get_texttrace():
            raise RuntimeError("trace unavailable")

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            return [(7, "ttf", "Type0", "ExactFont", "F1", "")]

    monkeypatch.setattr(embedded_fonts, "_sfnt_has_table", lambda *_args: True)
    monkeypatch.setattr(
        embedded_fonts,
        "_usable_font",
        lambda source, fmt, name, mapping: ("ttf", source, False),
    )

    catalog = EmbeddedFontCatalog.from_page(Page(), page_number=3)

    assert catalog.assets == ()
    failure = catalog.failure_for_span("ExactFont")
    assert failure.reason == "page_text_trace_inventory_failed"
    assert failure.proof_category == "runtime_inventory_unavailable_for_item"
    assert failure.error_type == "RuntimeError"
    assert failure.detail == "trace unavailable"


def test_embedded_font_work_bounds_reject_oversized_source_before_fonttools(monkeypatch):
    monkeypatch.setattr(embedded_fonts, "MAX_EMBEDDED_FONT_BYTES", 4)

    with pytest.raises(
        embedded_fonts.ExactFontSourceImpossible,
        match="exceeds embedded-font byte limit",
    ):
        embedded_fonts._validate_font_work_bounds(b"12345")


def test_embedded_font_work_bounds_reject_excessive_glyph_count(monkeypatch):
    monkeypatch.setattr(embedded_fonts, "MAX_EMBEDDED_FONT_GLYPHS", 3)

    with pytest.raises(
        embedded_fonts.ExactFontSourceImpossible,
        match="exceeds embedded-font glyph limit",
    ):
        embedded_fonts._validate_font_work_bounds(b"123", glyph_count=4)


def test_unexpected_fonttools_failure_is_runtime_bound_for_item(monkeypatch):
    class Document:
        @staticmethod
        def extract_font(_xref):
            return "ExactFont", "ttf", "Type0", b"source-font"

    class Page:
        parent = Document()

        @staticmethod
        def get_texttrace():
            return [{"font": "ExactFont", "chars": [(65, 1)]}]

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            return [(7, "ttf", "Type0", "ExactFont", "F1", "")]

    monkeypatch.setattr(
        embedded_fonts,
        "_usable_font",
        lambda *_args: (_ for _ in ()).throw(AttributeError("fontTools runtime bug")),
    )

    catalog = EmbeddedFontCatalog.from_page(Page(), page_number=3)
    failure = catalog.failure_for_span("ExactFont")

    assert failure.reason == "embedded_font_asset_build_failed"
    assert failure.proof_category == "runtime_capability_unavailable_for_item"
    assert failure.error_type == "AttributeError"
    assert failure.detail == "fontTools runtime bug"


def test_missing_source_document_has_complete_runtime_impossibility_evidence():
    class Page:
        parent = None

        @staticmethod
        def get_texttrace():
            return [{"font": "ExactFont", "chars": [(65, 1)]}]

        @staticmethod
        def get_fonts(*, full=False):
            assert full is True
            return [(7, "ttf", "Type0", "ExactFont", "F1", "")]

    failure = EmbeddedFontCatalog.from_page(Page(), 8).failure_for_span("ExactFont")

    assert failure.reason == "source_document_unavailable"
    assert failure.proof_category == "runtime_source_document_unavailable_for_item"
    assert failure.error_type == "SourceDocumentUnavailable"
    assert "extract_font" in failure.detail


def test_missing_fonttools_never_bypasses_exact_glyph_validation(monkeypatch):
    monkeypatch.setattr(embedded_fonts, "_sfnt_has_table", lambda *_args: True)
    monkeypatch.setattr(
        embedded_fonts,
        "_fonttools_loadable",
        lambda _data: (_ for _ in ()).throw(ImportError("fontTools unavailable")),
    )

    with pytest.raises(
        embedded_fonts.ExactFontRuntimeUnavailable,
        match="FontTools runtime required",
    ):
        embedded_fonts._usable_font(b"source-font", "ttf", "ExactFont", {})


class _TextDictPage:
    """Minimal PyMuPDF-shaped page for source-preservation tests."""

    parent = None

    def get_text(self, kind: str):
        assert kind == "dict"
        return {
            "blocks": [
                {
                    "type": 0,
                    "lines": [
                        {
                            "dir": (1.0, 0.0),
                            "bbox": (10.0, 30.0, 35.0, 45.0),
                            "spans": [
                                {
                                    "text": " 716 ",
                                    "origin": (10.0, 40.0),
                                    "bbox": (10.0, 30.0, 26.0, 43.0),
                                    "size": 10.0,
                                    "font": "Helvetica",
                                },
                                {
                                    "text": " / ",
                                    "origin": (11.0, 40.2),
                                    "bbox": (11.0, 30.2, 17.0, 43.2),
                                    "size": 10.0,
                                    "font": "Helvetica",
                                },
                            ],
                        }
                    ],
                }
            ]
        }

    def get_fonts(self, full=True):
        assert full is True
        return []

    def get_texttrace(self):
        return []


def test_extraction_preserves_edge_whitespace_and_does_not_invent_fraction_text():
    items = _extract_text(
        _TextDictPage(),
        page_h=100.0,
        page_num=1,
        flip_y=True,
        scale=1.0,
    )

    assert [item.text for item in items] == [" 716 ", " / "]
    assert [item.normalized for item in items] == ["716", "/"]
    assert items[0].source_bbox_pdf == (10.0, 30.0, 26.0, 43.0)
    assert items[0].bbox is not None
    assert items[0].bbox != items[0].source_bbox_pdf


def test_raw_text_extraction_preserves_each_character_glyph_origin_and_affine_quad():
    class RawTextPage:
        parent = None

        @staticmethod
        def get_text(kind: str):
            assert kind == "rawdict"
            return {
                "blocks": [{
                    "type": 0,
                    "lines": [{
                        "dir": (1.0, 0.0),
                        "bbox": (10.0, 10.0, 24.0, 24.0),
                        "spans": [{
                            "font": "ExactPDF",
                            "size": 10.0,
                            "ascender": 0.8,
                            "descender": -0.2,
                            "bbox": (10.0, 10.0, 24.0, 24.0),
                            "chars": [
                                {
                                    "c": "A",
                                    "origin": (10.0, 20.0),
                                    "bbox": (10.0, 10.0, 16.0, 22.0),
                                    "quad": (
                                        (10.0, 10.0), (16.0, 11.0),
                                        (16.5, 22.0), (10.5, 21.0),
                                    ),
                                },
                                {
                                    "c": "B",
                                    "origin": (18.0, 20.5),
                                    "bbox": (18.0, 11.0, 24.0, 23.0),
                                    "quad": (
                                        (18.0, 11.0), (24.0, 12.0),
                                        (24.5, 23.0), (18.5, 22.0),
                                    ),
                                },
                            ],
                        }],
                    }],
                }],
            }

        @staticmethod
        def get_fonts(*, full=True):
            assert full is True
            return []

        @staticmethod
        def get_texttrace():
            return [{
                "font": "ExactPDF",
                "chars": [
                    (ord("A"), 37, (10.0, 20.0), (10.0, 10.0, 16.0, 22.0)),
                    (ord("B"), 91, (18.0, 20.5), (18.0, 11.0, 24.0, 23.0)),
                ],
            }]

    items = _extract_text(
        RawTextPage(),
        page_h=100.0,
        page_num=3,
        flip_y=False,
        scale=1.0,
        to_model=lambda x, y: (float(x), float(y)),
    )

    assert len(items) == 1
    item = items[0]
    assert item.text == "AB"
    assert item.requires_individual_positioning is True
    assert item.baseline_descent == pytest.approx(2.0 * (25.4 / 72.0))
    assert [char.text for char in item.source_char_layout] == ["A", "B"]
    assert [char.glyph_id for char in item.source_char_layout] == [37, 91]
    assert item.source_char_layout[1].source_origin_pdf == (18.0, 20.5)
    assert item.source_char_layout[0].source_quad_pdf == (
        (10.0, 10.0), (16.0, 11.0), (16.5, 22.0), (10.5, 21.0)
    )
    assert item.source_char_layout[0].target_quad == item.source_char_layout[0].source_quad_pdf


def test_crop_rotate_and_userunit_use_page_rect_exactly(tmp_path):
    pdf_path = tmp_path / "crop-rotate-userunit.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((90, 120), "ROTATED SOURCE", fontsize=12)
    page.set_cropbox(fitz.Rect(50, 40, 350, 260))
    page.set_rotation(90)
    doc.xref_set_key(page.xref, "UserUnit", "2")
    doc.save(str(pdf_path))
    doc.close()

    reopened = fitz.open(str(pdf_path))
    try:
        source_page = reopened[0]
        data = extract_page(source_page, 1)
        expected_width = float(source_page.rect.width) * (25.4 / 72.0)
        expected_height = float(source_page.rect.height) * (25.4 / 72.0)
        assert data.width == pytest.approx(expected_width)
        assert data.height == pytest.approx(expected_height)
        assert data.text_items
        for item in data.text_items:
            assert item.bbox is not None
            x0, y0, x1, y1 = item.bbox
            assert -1e-6 <= x0 <= x1 <= data.width + 1e-6
            assert -1e-6 <= y0 <= y1 <= data.height + 1e-6
            assert math.isfinite(item.advance_width) and item.advance_width > 0
            assert math.isfinite(item.glyph_height) and item.glyph_height > 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("filename", "expected_spans"),
    [
        ("AWSWeldSymbolchart.pdf", 0),
        ("Welding-Symbol-Chart.pdf", 372),
    ],
)
def test_welding_pdfs_preserve_source_truth_and_item_font_evidence(
    filename: str,
    expected_spans: int,
):
    pdf_path = _find_welding_pdf(filename)
    if pdf_path is None:
        pytest.skip(f"welding PDF fixture missing: {filename}")

    doc = fitz.open(str(pdf_path))
    try:
        page = doc[0]
        data = extract_page(page, 1)
        assert data.width == pytest.approx(float(page.rect.width) * (25.4 / 72.0))
        assert data.height == pytest.approx(float(page.rect.height) * (25.4 / 72.0))
        assert len(data.text_items) == expected_spans
        if expected_spans == 0:
            assert len(page.get_images(full=True)) == 1
            assert page.rotation == 90
            xref = int(page.get_images(full=True)[0][0])
            image_rect, image_matrix = page.get_image_rects(xref, transform=True)[0]
            assert tuple(image_matrix) == pytest.approx((792.0, 0.0, 0.0, 612.0, 0.0, 0.0))
            rotation = _page_rotation_transform(page.rect, page.rotation_matrix)
            display_points = [
                _transform_pdf_point(
                    *(fitz.Point(u, v) * image_matrix),
                    rotation,
                )
                for u, v in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
            ]
            assert tuple(image_rect) == pytest.approx((0.0, 0.0, 792.0, 612.0))
            assert min(point[0] for point in display_points) == pytest.approx(page.rect.x0)
            assert max(point[0] for point in display_points) == pytest.approx(page.rect.x1)
            assert min(point[1] for point in display_points) == pytest.approx(page.rect.y0)
            assert max(point[1] for point in display_points) == pytest.approx(page.rect.y1)
            return

        font_names = {item.font_name for item in data.text_items}
        assert font_names == {
            "ArialMT",
            "MyriadPro-Regular",
            "Siwa-Bold",
            "Siwa-Regular",
        }
        assert all(
            (item.font_asset is None) != (item.font_failure is None)
            for item in data.text_items
        )
        assets = [item.font_asset for item in data.text_items if item.font_asset]
        assert assets
        assert all(asset.usable_bytes for asset in assets)
        assert all(asset.usable_sha256 for asset in assets)
        assert all(asset.units_per_em > 0 for asset in assets)
        assert all(asset.ascender > asset.descender for asset in assets)
        assert all(asset.glyph_advances for asset in assets)
        assert all(
            0 <= int(layout.glyph_id) < len(item.font_asset.glyph_advances)
            and item.font_asset.glyph_advances[int(layout.glyph_id)] > 0
            for item in data.text_items
            for layout in item.source_char_layout
            if item.font_asset is not None and layout.glyph_id is not None
        )
        assert all(item.source_bbox_pdf for item in data.text_items)
        assert all(item.source_quad_pdf for item in data.text_items)
    finally:
        doc.close()
