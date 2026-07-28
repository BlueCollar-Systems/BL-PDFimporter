"""Semantic text recognition must never rewrite Blender delivery spans."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "pdf_vector_importer"))
sys.path.insert(0, str(REPO_ROOT))

from pdfcadcore import primitive_extractor  # noqa: E402
from pdfcadcore.document_profiler import profile  # noqa: E402
from pdfcadcore.generic_recognizer import analyze  # noqa: E402
from pdfcadcore import primitives  # noqa: E402
from pdfcadcore.resolved_scale import resolve_page_scale  # noqa: E402


def _item(
    span_id: int,
    text: str,
    *,
    x: float,
    y: float,
    font_size: float,
):
    width = max(len(text), 1) * font_size * 0.5
    return primitives.NormalizedText(
        id=span_id,
        text=text,
        normalized=text.upper(),
        insertion=(x, y),
        bbox=(x, y, x + width, y + font_size),
        font_size=font_size,
        font_name="ExactPDF",
        page_number=1,
    )


def test_stacked_fraction_semantics_do_not_mutate_delivery_spans() -> None:
    semantic_text_projection = getattr(
        primitive_extractor,
        "semantic_text_projection",
        None,
    )
    text_items_for_analysis = getattr(primitives, "text_items_for_analysis", None)
    assert callable(semantic_text_projection), (
        "stacked-fraction recognition needs an analysis-only projection"
    )
    assert callable(text_items_for_analysis), (
        "analysis consumers need an explicit semantic-text accessor"
    )

    # The source encodes a full-size whole number plus a positioned stacked 1/4.
    delivery = [
        _item(1, "2", x=5.0, y=20.0, font_size=6.0),
        _item(2, "1", x=13.0, y=22.0, font_size=3.78),
        _item(3, "/", x=13.0, y=20.0, font_size=3.78),
        _item(4, "4", x=13.0, y=18.0, font_size=3.78),
    ]
    delivery_snapshot = [
        (item.id, item.text, item.insertion, item.bbox, item.font_size)
        for item in delivery
    ]

    semantic = semantic_text_projection(delivery)
    page = primitives.PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    assert [
        (item.id, item.text, item.insertion, item.bbox, item.font_size)
        for item in page.text_items
    ] == delivery_snapshot
    assert [item.text for item in page.text_items] == ["2", "1", "/", "4"]
    assert [item.id for item in page.text_items] == [1, 2, 3, 4]

    analysis = text_items_for_analysis(page)
    assert analysis is page.semantic_text_items
    assert [item.text for item in analysis] == ["2", "1/4"]
    merged = next(item for item in analysis if item.text == "1/4")
    assert merged.semantic_projection is True
    assert merged.source_span_ids == (2, 3, 4)
    assert merged.requires_individual_positioning is False


def test_semantic_projection_does_not_consume_physical_identity_allocator() -> None:
    semantic_text_projection = getattr(
        primitive_extractor,
        "semantic_text_projection",
        None,
    )
    assert callable(semantic_text_projection)
    delivery = [
        _item(1, "1", x=1.0, y=4.0, font_size=3.0),
        _item(2, "/", x=1.0, y=3.0, font_size=3.0),
        _item(3, "4", x=1.0, y=2.0, font_size=3.0),
    ]

    primitives.reset_ids()
    try:
        semantic = semantic_text_projection(delivery)

        assert [item.text for item in semantic] == ["1/4"]
        assert primitives.next_id() == 1
    finally:
        primitives.reset_ids()


def test_analysis_consumers_use_projection_without_rewriting_delivery() -> None:
    semantic_text_projection = getattr(
        primitive_extractor,
        "semantic_text_projection",
        None,
    )
    assert callable(semantic_text_projection)
    delivery = [
        _item(1, "1", x=1.0, y=4.0, font_size=3.0),
        _item(2, "/", x=1.0, y=3.0, font_size=3.0),
        _item(3, "4", x=1.0, y=2.0, font_size=3.0),
    ]
    semantic = semantic_text_projection(delivery)
    page = primitives.PageData(
        page_number=1,
        width=10.0,
        height=10.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    profile(page)
    result = analyze(page)

    association = next(item for item in result.dimension_assocs if item["text"] == "1/4")
    assert association["semantic_projection"] is True
    assert association["source_span_ids"] == [1, 2, 3]
    assert [item.text for item in page.text_items] == ["1", "/", "4"]


def test_scale_resolution_uses_semantic_projection_without_rewriting_delivery() -> None:
    text_items_for_analysis = getattr(primitives, "text_items_for_analysis", None)
    assert callable(text_items_for_analysis)
    delivery = [_item(1, "SOURCE", x=60.0, y=10.0, font_size=3.0)]
    semantic = [_item(1, "SCALE 1:25", x=60.0, y=10.0, font_size=3.0)]
    semantic[0].normalized = "SCALE 1:25"
    semantic[0].semantic_projection = True
    semantic[0].generic_tags.extend(("scale_like", "titleblock_like"))
    page = primitives.PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    result = resolve_page_scale(page)

    assert text_items_for_analysis(page) is semantic
    assert result.factor == 25.0
    assert result.notation == "SCALE 1:25"
    assert [item.text for item in page.text_items] == ["SOURCE"]


def test_importer_uses_same_physical_spans_for_obligations_and_rendering() -> None:
    """Keep the host boundary pinned to PageData.text_items, never semantics."""

    source = (
        REPO_ROOT / "pdf_vector_importer" / "bl_import_engine.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    physical_inventory_assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "page_text_items"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "list"
        and node.value.args
        and "page_data.text_items" in ast.unparse(node.value.args[0])
    ]
    assert physical_inventory_assignments, (
        "obligations need an inventory copied from physical page_data.text_items"
    )

    source_id_generators = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.GeneratorExp)
        and node.generators
        and isinstance(node.generators[0].iter, ast.Name)
        and node.generators[0].iter.id == "page_text_items"
        and "page:%d:text:%d" in ast.unparse(node.elt)
    ]
    assert source_id_generators, (
        "obligation source IDs must be derived from the physical inventory"
    )

    build_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_all_text"
    ]
    assert len(build_calls) == 1
    first_argument = build_calls[0].args[0]
    assert isinstance(first_argument, ast.Name)
    assert first_argument.id == "page_text_items", (
        "rendering must consume the same physical list that created obligations"
    )
