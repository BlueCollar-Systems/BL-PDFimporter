"""Regression coverage for terminal raster delivery failures in Blender."""
from __future__ import annotations

import json
import sys
import types

if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace(
        Collection=object,
        Object=object,
    )
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine
from pdf_vector_importer.pdfcadcore import fitz_loader


class _Children:
    def __init__(self):
        self.items = []

    def link(self, item):
        self.items.append(item)


class _Collection:
    def __init__(self, name):
        self.name = name
        self.children = _Children()
        self.all_objects = []


class _Collections:
    def __init__(self):
        self.items = []

    def new(self, name):
        collection = _Collection(name)
        self.items.append(collection)
        return collection


class _FakeBpy:
    def __init__(self):
        self.data = types.SimpleNamespace(
            collections=_Collections(),
            objects=types.SimpleNamespace(get=lambda _name: None),
        )
        self.context = types.SimpleNamespace(
            scene=types.SimpleNamespace(
                collection=types.SimpleNamespace(children=_Children()),
            ),
            view_layer=types.SimpleNamespace(update=lambda: None),
        )


class _Page:
    rect = types.SimpleNamespace(width=72.0, height=72.0)


class _Document:
    page_count = 1

    def load_page(self, _index):
        return _Page()

    def close(self):
        pass


def _page_data():
    return types.SimpleNamespace(
        primitives=[],
        text_items=[],
        width=25.4,
        height=25.4,
        resolved_scale=None,
    )


def _raster_placement():
    return {
        "path": "rendered-page.png",
        "x_mm": 0.0,
        "y_mm": 0.0,
        "width_mm": 25.4,
        "height_mm": 25.4,
        "xref": -1,
        "page_number": 1,
    }


def _run_raster_import(monkeypatch, tmp_path, *, rendered, plane_created):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    report_calls = []

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: _page_data())
    monkeypatch.setattr(bl_import_engine, "_render_page_raster", lambda *_args, **_kwargs: rendered)
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: plane_created)
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))

    def _capture_report(_filepath, _config, stats, **_kwargs):
        report_calls.append(dict(stats))
        return str(tmp_path / "import_report.json")

    monkeypatch.setattr(bl_import_engine, "write_import_report", _capture_report)
    stats = bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "raster",
            "pages": "1",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )
    assert len(report_calls) == 1
    return stats, report_calls[0]


def test_raster_render_failure_is_recorded_in_returned_and_reported_stats(monkeypatch, tmp_path):
    stats, reported_stats = _run_raster_import(
        monkeypatch,
        tmp_path,
        rendered=None,
        plane_created=True,
    )

    expected = [{
        "page": 1,
        "stage": "render",
        "reason": "raster_render_failed",
    }]
    assert stats["raster_delivery_failures"] == expected
    assert reported_stats["raster_delivery_failures"] == expected


def test_raster_plane_failure_is_recorded_in_returned_and_reported_stats(monkeypatch, tmp_path):
    stats, reported_stats = _run_raster_import(
        monkeypatch,
        tmp_path,
        rendered=_raster_placement(),
        plane_created=None,
    )

    expected = [{
        "page": 1,
        "stage": "plane",
        "reason": "raster_plane_creation_failed",
    }]
    assert stats["raster_delivery_failures"] == expected
    assert reported_stats["raster_delivery_failures"] == expected


def test_terminal_raster_failure_is_loud_in_report_without_clobbering_text_fallback(
    monkeypatch,
    tmp_path,
):
    report_path = tmp_path / "import_report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")
    provenance_opts = types.SimpleNamespace(
        _text_mode_fallbacks=[{
            "requested": "glyphs",
            "delivered": "labels",
            "reason": "meshify_failed",
            "count": 1,
        }],
    )
    stats = {
        "pages_imported": 1,
        "primitives": 0,
        "text_items": 1,
        "collections": 1,
        "elapsed": 0.01,
        "raster_delivery_failures": [{
            "page": 1,
            "stage": "render",
            "reason": "raster_render_failed",
        }],
    }

    bl_import_engine.write_import_report(
        str(tmp_path / "input.pdf"),
        {"import_text": True, "text_mode": "glyphs"},
        stats,
        import_mode="raster",
        output_path=str(report_path),
        provenance_opts=provenance_opts,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["fallback"]["reason"] == "raster_delivery_failed"
    assert report["fallback"]["text"] == {
        "requested": "glyphs",
        "delivered": "labels",
        "reason": "meshify_failed",
        "count": 1,
    }
    assert report["extra"]["raster_delivery_failures"] == stats["raster_delivery_failures"]
    assert "raster_delivery_failed" in report["extra"]["diagnostics"]["signals"]


def test_auto_raster_delivery_failure_marks_fallback_as_used(monkeypatch, tmp_path):
    report_path = tmp_path / "import_report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")
    stats = {
        "pages_imported": 1,
        "primitives": 0,
        "text_items": 0,
        "collections": 1,
        "elapsed": 0.01,
        "raster_delivery_failures": [{
            "page": 1,
            "stage": "render",
            "reason": "raster_render_failed",
        }],
    }

    bl_import_engine.write_import_report(
        str(tmp_path / "input.pdf"),
        {"import_text": False},
        stats,
        import_mode="auto",
        output_path=str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["fallback"] == {
        "used": True,
        "reason": "raster_delivery_failed",
    }


def test_hybrid_sparse_shell_raster_success_is_reported_as_fallback(monkeypatch, tmp_path):
    """A successful terminal raster in Hybrid mode must not be reported as no fallback."""
    input_pdf = tmp_path / "input.pdf"
    report_path = tmp_path / "import_report.json"
    input_pdf.write_bytes(b"%PDF-1.7\n")

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: _page_data())
    monkeypatch.setattr(bl_import_engine, "build_page", lambda *_args, **_kwargs: {
        "curves": 0,
        "meshes": 0,
        "circles": 0,
        "arcs": 0,
        "skipped_fill_only": 0,
        "model3d_solids": 0,
    })
    monkeypatch.setattr(bl_import_engine, "build_all_text", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bl_import_engine, "_extract_image_placements", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bl_import_engine, "_render_page_raster", lambda *_args, **_kwargs: _raster_placement())
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")

    stats = bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "hybrid",
            "pages": "1",
            "import_report_path": str(report_path),
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert stats["images"] == 1
    assert stats["raster_delivery_failures"] == []
    assert report["fallback"] == {
        "used": True,
        "reason": "raster_fallback_1_page",
    }
