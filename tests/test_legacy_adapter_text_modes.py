"""Regression checks for the deprecated Blender adapter compatibility wrapper."""
from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER = REPO_ROOT / "blender_pdf_vector_importer" / "adapters" / "blender_adapter.py"
ENTRYPOINT = REPO_ROOT / "blender_pdf_vector_importer" / "__init__.py"


def _shipping_engine(monkeypatch: pytest.MonkeyPatch):
    fake_bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(version=(4, 1, 0)),
        types=types.SimpleNamespace(
            Collection=object,
            Material=object,
            Object=object,
            VectorFont=object,
        ),
    )
    monkeypatch.setitem(sys.modules, "bpy", fake_bpy)
    monkeypatch.setitem(sys.modules, "bmesh", types.SimpleNamespace())
    from pdf_vector_importer import bl_import_engine

    return bl_import_engine


def test_legacy_adapter_delegates_every_delivery_to_the_shipping_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blender_pdf_vector_importer.adapters import blender_adapter
    bl_import_engine = _shipping_engine(monkeypatch)

    captured = {}

    def fake_import_pdf(filepath, config=None, progress_callback=None, context=None):
        captured.update(filepath=filepath, config=dict(config or {}), context=context)
        return {
            "pages_imported": 2,
            "primitives": 7,
            "text_items": 3,
            "images": 1,
            "text_delivery_failed_items": 0,
            "text_delivery_fallback_items": 0,
            "raster_delivery_failures": [],
            "import_report_path": "proof.json",
        }

    monkeypatch.setattr(bl_import_engine, "import_pdf", fake_import_pdf)

    result = blender_adapter.import_into_blender(
        "drawing.pdf",
        mode="hybrid",
        options=blender_adapter.BlenderImportOptions(
            pages="1,3",
            import_text=True,
            text_mode="glyphs",
            import_images=False,
            group_by_color=False,
        ),
    )

    assert captured == {
        "filepath": "drawing.pdf",
        "config": {
            "mode": "hybrid",
            "pages": "1,3",
            "import_text": True,
            "text_mode": "glyphs",
            "ignore_images": True,
            "group_by_color": False,
            "strict_text_fidelity": True,
        },
        "context": None,
    }
    assert result.summary() == {
        "pages": 2,
        "primitives": 7,
        "text_items": 3,
        "images": 1,
        "text_delivery_failed_items": 0,
        "text_delivery_fallback_items": 0,
        "raster_delivery_failures": [],
        "import_report_path": "proof.json",
    }


def test_legacy_adapter_rejects_silent_loss_of_source_layer_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blender_pdf_vector_importer.adapters import blender_adapter

    monkeypatch.setitem(sys.modules, "bpy", object())
    with pytest.raises(ValueError, match="Source layer preservation cannot be disabled"):
        blender_adapter.import_into_blender(
            "drawing.pdf",
            options=blender_adapter.BlenderImportOptions(group_by_layer=False),
        )


def test_legacy_adapter_surfaces_report_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blender_pdf_vector_importer.adapters import blender_adapter
    bl_import_engine = _shipping_engine(monkeypatch)

    monkeypatch.setattr(
        bl_import_engine,
        "import_pdf",
        lambda *args, **kwargs: {"import_report_error": "disk full"},
    )

    with pytest.raises(RuntimeError, match="Import report could not be written: disk full"):
        blender_adapter.import_into_blender("drawing.pdf")


def test_legacy_adapter_contains_no_independent_host_builders() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    assert "run_import" not in source
    assert "_create_curve_object" not in source
    assert "_create_image_plane" not in source
    assert "_create_text_object" not in source
    assert "bpy.data" not in source
    assert "tempfile" not in source


def test_legacy_ui_has_no_control_for_the_removed_delivery_path() -> None:
    source = ENTRYPOINT.read_text(encoding="utf-8")

    assert "group_by_layer: BoolProperty" not in source
    assert "group_by_layer=self.group_by_layer" not in source
    assert "context=context" in source
    assert 'summary["text_delivery_failed_items"]' in source
    assert 'summary["raster_delivery_failures"]' in source
