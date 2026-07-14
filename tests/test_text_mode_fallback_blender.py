"""TEXTMODE-1 regression coverage for Blender's production text path."""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace(
        Collection=object,
        Material=object,
        Object=object,
        VectorFont=object,
    )
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine, bl_text_builder
from pdf_vector_importer.pdfcadcore.primitives import NormalizedText


class _FakeFontData:
    type = "FONT"

    def __init__(self, name: str):
        self.name = name
        self.body = ""
        self.size = 0.0
        self.extrude = 0.0
        self.resolution_u = 12
        self.align_x = ""
        self.align_y = ""
        self.materials = []


class _FakeObject(dict):
    def __init__(self, name: str, data):
        super().__init__()
        self.name = name
        self.data = data
        self.location = None
        self.rotation_euler = None
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.matrix_world = types.SimpleNamespace(copy=lambda: "matrix")

    def evaluated_get(self, _depsgraph):
        return self


class _FakeCollectionObjects:
    def __init__(self):
        self.items = []

    def link(self, obj):
        self.items.append(obj)

    def unlink(self, obj):
        self.items.remove(obj)


class _FakeCollection:
    def __init__(self):
        self.objects = _FakeCollectionObjects()


class _FakeCurves:
    def new(self, name: str, type: str):
        assert type == "FONT"
        return _FakeFontData(name)


class _FakeObjects:
    def new(self, name: str, data):
        return _FakeObject(name, data)

    def remove(self, _obj, do_unlink=True):
        assert do_unlink is True


class _FakeMeshes:
    def new_from_object(self, *_args, **_kwargs):
        raise RuntimeError("mesh conversion unavailable")


class _FailingMeshifyBpy:
    def __init__(self):
        self.data = types.SimpleNamespace(
            curves=_FakeCurves(),
            objects=_FakeObjects(),
            meshes=_FakeMeshes(),
        )
        self.context = types.SimpleNamespace(
            evaluated_depsgraph_get=lambda: object(),
        )


def _text_item() -> NormalizedText:
    return NormalizedText(
        id=41,
        text="MESH FALLBACK",
        normalized="MESH FALLBACK",
        insertion=(12.0, 24.0),
        bbox=(12.0, 24.0, 52.0, 30.0),
        font_size=6.0,
        rotation=30.0,
    )


def _configure_headless_builder(monkeypatch: pytest.MonkeyPatch) -> _FakeCollection:
    monkeypatch.setattr(bl_text_builder, "bpy", _FailingMeshifyBpy())
    monkeypatch.setattr(bl_text_builder, "_get_preferred_font", lambda: None)
    monkeypatch.setattr(
        bl_text_builder,
        "_get_or_create_text_material",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            diffuse_color=(0.1, 0.1, 0.1, 1.0),
        ),
    )
    return _FakeCollection()


def _write_active_report(tmp_path, *, requested_mode: str, opts, text_count: int):
    report_path = tmp_path / "import_report.json"
    bl_import_engine.write_import_report(
        str(tmp_path / "sample.pdf"),
        {"import_text": True, "text_mode": requested_mode},
        {
            "pages_imported": 1,
            "primitives": 1,
            "text_items": text_count,
            "collections": 1,
            "elapsed": 0.01,
            "text_source_spans": text_count,
            "text_glyph_estimate": 8,
        },
        import_mode="vector",
        output_path=str(report_path),
        provenance_opts=opts,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def _assert_requested_mode_delivered_or_loudly_reported(report: dict) -> None:
    requested = report["extra"]["text_mode"]
    text_fallback = report["fallback"].get("text")
    if text_fallback is None:
        assert requested == report["extra"]["actual_text_entity_types"]["entity_type"]
        return
    assert text_fallback["requested"] == requested
    assert text_fallback["delivered"] != requested
    assert text_fallback["count"] > 0
    assert "text_mode_fallback" in report["extra"]["diagnostics"]["signals"]


def test_meshify_failure_retags_font_curve_and_reports_fallback(monkeypatch, tmp_path):
    collection = _configure_headless_builder(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="glyphs")

    obj = bl_text_builder.build_text(
        _text_item(),
        collection,
        text_mode="glyphs",
        provenance_opts=opts,
    )

    assert obj.data.type == "FONT"
    assert obj.data.size == pytest.approx(0.006)
    assert obj["pdf_text_mode"] == "labels"
    assert obj["pdf_text_requested_mode"] == "glyphs"
    assert obj["pdf_text_fallback_reason"] == "meshify_failed"
    assert opts._text_delivered_entity_counts == {"native_label": 1}
    provenance = opts._source_provenance_objects[-1]
    assert provenance.created_entity_type == "native_label"
    assert provenance.selected_text_mode == "glyphs"

    report = _write_active_report(
        tmp_path,
        requested_mode="glyphs",
        opts=opts,
        text_count=1,
    )
    assert report["fallback"]["text"] == {
        "requested": "glyphs",
        "delivered": "labels",
        "reason": "meshify_failed",
        "count": 1,
    }
    assert report["extra"]["actual_text_entity_types"]["native_label"] == 1
    assert report["extra"]["actual_text_entity_types"]["font_rendered"] is True
    _assert_requested_mode_delivered_or_loudly_reported(report)


def test_unknown_text_mode_warns_and_reports_normalization(monkeypatch, tmp_path, caplog):
    collection = _configure_headless_builder(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="typo_mode")

    with caplog.at_level(logging.WARNING, logger=bl_text_builder.__name__):
        obj = bl_text_builder.build_text(
            _text_item(),
            collection,
            text_mode="typo_mode",
            provenance_opts=opts,
        )

    assert "Unknown Blender text mode 'typo_mode'" in caplog.text
    assert obj["pdf_text_mode"] == "3d_text"
    assert obj["pdf_text_requested_mode"] == "typo_mode"
    assert opts._text_delivered_entity_counts == {"native_3d_text": 1}

    report = _write_active_report(
        tmp_path,
        requested_mode="typo_mode",
        opts=opts,
        text_count=1,
    )
    assert report["fallback"]["text"] == {
        "requested": "typo_mode",
        "delivered": "3d_text",
        "reason": "unknown_text_mode_normalized",
        "count": 1,
    }
    assert report["extra"]["actual_text_entity_types"]["font_rendered"] is True
    _assert_requested_mode_delivered_or_loudly_reported(report)


def test_supported_mode_is_delivered_without_a_text_mode_fallback(monkeypatch, tmp_path):
    collection = _configure_headless_builder(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="labels")

    obj = bl_text_builder.build_text(
        _text_item(),
        collection,
        text_mode="labels",
        provenance_opts=opts,
    )

    assert obj["pdf_text_mode"] == "labels"
    report = _write_active_report(
        tmp_path,
        requested_mode="labels",
        opts=opts,
        text_count=1,
    )
    assert "text" not in report["fallback"]
    _assert_requested_mode_delivered_or_loudly_reported(report)
