from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


def _install_blender_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(version=(5, 2, 0)),
        ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(redraw_timer=lambda **_kwargs: None),
        ),
        types=types.SimpleNamespace(
            Collection=object,
            Material=object,
            Object=object,
            VectorFont=object,
        ),
    )
    monkeypatch.setitem(sys.modules, "bpy", fake_bpy)
    monkeypatch.setitem(sys.modules, "bmesh", types.SimpleNamespace())


def test_text_builder_honors_cancel_at_its_first_work_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: returning False from progress must prevent the next text build."""
    _install_blender_stubs(monkeypatch)
    builder = importlib.import_module("pdf_vector_importer.bl_text_builder")
    built: list[object] = []

    def fake_build(item, *_args, **_kwargs):
        built.append(item)
        return object()

    monkeypatch.setattr(builder, "build_text", fake_build)

    with pytest.raises(RuntimeError, match="cancel",):
        builder.build_all_text(
            [object(), object(), object()],
            object(),
            progress_callback=lambda _fraction: False,
        )

    assert built == []


def test_geometry_builder_honors_cancel_at_its_first_work_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: cancelled geometry must not create the next host object."""
    _install_blender_stubs(monkeypatch)
    builder = importlib.import_module("pdf_vector_importer.bl_geometry_builder")
    primitives = importlib.import_module("pdf_vector_importer.pdfcadcore.primitives")
    primitive = primitives.Primitive(
        id=9,
        type="line",
        points=[(0.0, 0.0), (10.0, 0.0)],
        stroke_color=(0.0, 0.0, 0.0),
    )
    page = primitives.PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        primitives=[primitive],
    )
    created: list[str] = []

    with (
        patch.object(builder, "_resolve_collection", return_value=object()),
        patch.object(builder, "_get_or_create_material", return_value=object()),
        patch.object(
            builder,
            "_draw_stroked_polyline",
            side_effect=lambda *_args, **_kwargs: created.append("line") or 1,
        ),
        patch.object(builder, "_create_multi_poly_curve", return_value=object()),
    ):
        with pytest.raises(RuntimeError, match="cancel"):
            builder.build_page(
                page,
                object(),
                progress_callback=lambda _fraction: False,
            )

    assert created == []


def test_complexity_estimate_is_mode_sensitive_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: expensive geometry text must not look as cheap as flat text."""
    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")

    flat = engine.estimate_import_complexity(
        page_count=2,
        primitive_count=100,
        text_item_count=10,
        glyph_count=100,
        text_mode="text",
    )
    geometry = engine.estimate_import_complexity(
        page_count=2,
        primitive_count=100,
        text_item_count=10,
        glyph_count=100,
        text_mode="geometry",
    )

    assert geometry["work_units"] > flat["work_units"]
    assert geometry["tier"] in {"large", "extreme"}
    assert geometry["cancel_granularity"] == "object_heartbeat"
    assert geometry["resume_granularity"] == "completed_page"
    assert "page" in geometry["message"].lower()


def test_cancel_heartbeat_is_bounded_for_extreme_object_counts() -> None:
    """Regression: old hardware must never wait hundreds of objects to cancel."""
    session = importlib.import_module("pdf_vector_importer.import_session")

    assert session.cancel_heartbeat_interval(1) == 1
    assert session.cancel_heartbeat_interval(10_000) <= 25


def test_resume_checkpoint_round_trip_is_atomic_and_source_bound(
    tmp_path: Path,
) -> None:
    """Regression: another PDF/config must never consume an interrupted run."""
    session = importlib.import_module("pdf_vector_importer.import_session")
    checkpoint = tmp_path / "resume.json"
    state = session.build_resume_state(
        source_sha256="a" * 64,
        config_sha256="b" * 64,
        requested_pages=[1, 2, 3, 4],
        completed_pages=[1, 2],
        root_collection="PDF Import - drawing",
        next_stack_offset_m=-0.42,
        aggregate_stats={"pages_imported": 2, "primitives": 17},
        text_delivery_items=[{"item_id": "P1_text_1", "status": "delivered"}],
    )

    session.write_resume_checkpoint(checkpoint, state)
    loaded = session.load_resume_checkpoint(
        checkpoint,
        source_sha256="a" * 64,
        config_sha256="b" * 64,
    )

    assert loaded["completed_pages"] == [1, 2]
    assert loaded["remaining_pages"] == [3, 4]
    assert loaded["resume_pages"] == "3-4"
    assert loaded["root_collection"] == "PDF Import - drawing"
    assert not checkpoint.with_suffix(checkpoint.suffix + ".tmp").exists()

    with pytest.raises(ValueError, match="source"):
        session.load_resume_checkpoint(
            checkpoint,
            source_sha256="c" * 64,
            config_sha256="b" * 64,
        )


def test_resume_config_digest_ignores_checkpoint_location_but_binds_delivery_mode() -> None:
    """Regression: moving evidence is safe; changing representation is not."""
    session = importlib.import_module("pdf_vector_importer.import_session")
    left = session.resume_config_sha256({
        "mode": "vector",
        "text_mode": "geometry",
        "pages": "1-3",
        "resume_checkpoint_path": "C:/TMP/left.json",
    })
    right = session.resume_config_sha256({
        "mode": "vector",
        "text_mode": "geometry",
        "pages": "1-3",
        "resume_checkpoint_path": "C:/TMP/right.json",
    })
    changed = session.resume_config_sha256({
        "mode": "vector",
        "text_mode": "glyphs",
        "pages": "1-3",
        "resume_checkpoint_path": "C:/TMP/right.json",
    })

    assert left == right
    assert changed != left


def test_cancelled_report_is_explicit_and_carries_page_resume_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: partial page work must never be reported as success."""
    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    report_module = importlib.import_module("pdf_vector_importer.pdfcadcore.import_report")
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4\nidentity fixture\n%%EOF\n")
    report_path = tmp_path / "cancelled.json"
    resume = {
        "schema": "bcs.blender.page_resume/1.0",
        "completed_pages": [1],
        "remaining_pages": [2, 3],
        "resume_pages": "2-3",
    }
    monkeypatch.setattr(report_module, "_pdf_audit_extras", lambda _path: {})
    monkeypatch.setattr(engine, "_pymupdf_version", lambda: "")

    engine.write_import_report(
        str(source),
        {"import_text": False},
        {
            "pages_imported": 1,
            "primitives": 5,
            "text_items": 0,
            "collections": 2,
            "elapsed": 0.02,
            "cancelled": True,
            "resume": resume,
        },
        import_mode="vector",
        output_path=str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["extra"]["result_status"] == "cancelled"
    assert report["extra"]["resume"] == resume


def test_import_report_exposes_representation_aware_complexity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: users must be told when requested host work is expensive."""
    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    report_module = importlib.import_module("pdf_vector_importer.pdfcadcore.import_report")
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4\nidentity fixture\n%%EOF\n")
    report_path = tmp_path / "complexity.json"
    complexity = engine.estimate_import_complexity(
        page_count=4,
        primitive_count=400,
        text_item_count=100,
        glyph_count=1200,
        text_mode="geometry",
    )
    monkeypatch.setattr(report_module, "_pdf_audit_extras", lambda _path: {})
    monkeypatch.setattr(engine, "_pymupdf_version", lambda: "")

    engine.write_import_report(
        str(source),
        {"import_text": True, "text_mode": "geometry"},
        {
            "pages_imported": 4,
            "primitives": 400,
            "text_items": 100,
            "text_source_spans": 100,
            "text_glyph_estimate": 1200,
            "collections": 5,
            "elapsed": 1.0,
            "complexity": complexity,
        },
        import_mode="vector",
        output_path=str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["extra"]["complexity"] == complexity
    assert report["extra"]["complexity"]["tier"] == "extreme"


def test_operator_exposes_active_cancel_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Cancel must be an executable host command, not a dead flag."""
    bpy_module = types.ModuleType("bpy")

    class Operator:
        pass

    class ImportHelper:
        pass

    bpy_module.types = types.SimpleNamespace(Operator=Operator)
    props = types.ModuleType("bpy.props")
    props.BoolProperty = lambda **_kwargs: None
    props.EnumProperty = lambda **_kwargs: None
    props.FloatProperty = lambda **_kwargs: None
    props.StringProperty = lambda **_kwargs: None
    io_utils = types.ModuleType("bpy_extras.io_utils")
    io_utils.ImportHelper = ImportHelper
    bpy_extras = types.ModuleType("bpy_extras")
    bpy_extras.io_utils = io_utils
    monkeypatch.setitem(sys.modules, "bpy", bpy_module)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy_extras", bpy_extras)
    monkeypatch.setitem(sys.modules, "bpy_extras.io_utils", io_utils)
    sys.modules.pop("pdf_vector_importer.operators", None)
    operators = importlib.import_module("pdf_vector_importer.operators")

    operators._begin_import_session()
    try:
        assert operators.IMPORT_OT_pdf_vector_cancel.poll(None) is True
        cancel = operators.IMPORT_OT_pdf_vector_cancel()
        cancel.report = lambda *_args, **_kwargs: None
        assert cancel.execute(None) == {"FINISHED"}
        assert operators._cancel_requested() is True
    finally:
        operators._end_import_session()

    assert operators.IMPORT_OT_pdf_vector_cancel.poll(None) is False


def test_engine_propagates_cancel_to_geometry_and_writes_resume_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: an expensive page heartbeat must stop and remain resumable."""
    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    fitz_loader = importlib.import_module("pdf_vector_importer.pdfcadcore.fitz_loader")
    session = importlib.import_module("pdf_vector_importer.import_session")

    class Children:
        def __init__(self):
            self.items = []

        def link(self, value):
            self.items.append(value)

    class Collection:
        def __init__(self, name):
            self.name = name
            self.children = Children()
            self.all_objects = []

    class Collections:
        def __init__(self):
            self.items = []

        def new(self, name):
            value = Collection(name)
            self.items.append(value)
            return value

        def remove(self, value):
            self.items.remove(value)

    class Page:
        rect = types.SimpleNamespace(width=72.0, height=72.0)

        def get_drawings(self):
            return []

        def get_text(self, _kind):
            return []

        mediabox = types.SimpleNamespace(width=72.0, height=72.0)

    class Document:
        page_count = 1
        is_closed = False

        def load_page(self, _index):
            return Page()

        def close(self):
            self.is_closed = True

    fake_bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(version=(5, 2, 0)),
        data=types.SimpleNamespace(
            collections=Collections(),
            objects=types.SimpleNamespace(remove=lambda *_args, **_kwargs: None),
        ),
        context=types.SimpleNamespace(
            scene=types.SimpleNamespace(collection=types.SimpleNamespace(children=Children())),
            view_layer=types.SimpleNamespace(update=lambda: None),
        ),
        types=sys.modules["bpy"].types,
        ops=sys.modules["bpy"].ops,
    )
    monkeypatch.setattr(engine, "bpy", fake_bpy)
    monkeypatch.setattr(engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Document())
    page_data = types.SimpleNamespace(
        primitives=[types.SimpleNamespace()],
        text_items=[],
        width=25.4,
        height=25.4,
        resolved_scale=None,
    )
    monkeypatch.setattr(engine, "extract_page", lambda *_args, **_kwargs: page_data)
    monkeypatch.setattr(engine, "recognition", types.SimpleNamespace(run=lambda *_args, **_kwargs: None))
    monkeypatch.setattr(engine, "cleanup_primitives", lambda *_args, **_kwargs: {})

    def cancellable_geometry(_page, _collection, _config, progress_callback=None):
        assert progress_callback is not None
        if progress_callback(0.1) is False:
            raise session.ImportCancelledError("cancel requested")
        raise AssertionError("engine discarded the cancel request")

    monkeypatch.setattr(engine, "build_page", cancellable_geometry)
    monkeypatch.setattr(engine, "write_import_report", lambda *_args, **_kwargs: str(tmp_path / "report.json"))
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    checkpoint = tmp_path / "resume.json"

    stats = engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "vector",
            "pages": "1",
            "import_text": False,
            "ignore_images": True,
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
            "resume_checkpoint_path": str(checkpoint),
        },
        cancel_callback=lambda: True,
    )

    assert stats["cancelled"] is True
    assert stats["pages_imported"] == 0
    assert stats["resume"]["remaining_pages"] == [1]
    assert checkpoint.is_file()


def test_stream_extraction_cancel_is_latched_as_cancelled_not_success() -> None:
    """Regression: extractor callbacks returning False must not fall through GREEN."""
    source = (
        Path(__file__).parents[1] / "pdf_vector_importer" / "bl_import_engine.py"
    ).read_text(encoding="utf-8")
    callback = source[source.index("def _on_stream_progress") : source.index(
        "for loop_i, (page_num, page_data)", source.index("def _on_stream_progress")
    )]
    assert "stream_cancelled = True" in callback
    assert "if stream_cancelled and cancelled_page is None" in source


def test_engine_resume_reuses_root_and_only_builds_unfinished_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: resume must assemble into the prior model without duplicate pages."""
    import hashlib

    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    fitz_loader = importlib.import_module("pdf_vector_importer.pdfcadcore.fitz_loader")
    session = importlib.import_module("pdf_vector_importer.import_session")

    class Children:
        def __init__(self):
            self.items = []

        def link(self, value):
            self.items.append(value)

    class Collection:
        def __init__(self, name):
            self.name = name
            self.children = Children()
            self.all_objects = []

    class Collections:
        def __init__(self, initial):
            self.items = list(initial)

        def new(self, name):
            value = Collection(name)
            self.items.append(value)
            return value

        def get(self, name):
            return next((item for item in self.items if item.name == name), None)

        def remove(self, value, **_kwargs):
            self.items.remove(value)

    class Page:
        rect = types.SimpleNamespace(width=72.0, height=72.0)
        mediabox = types.SimpleNamespace(width=72.0, height=72.0)

    class Document:
        page_count = 2
        is_closed = False

        def load_page(self, _index):
            return Page()

        def close(self):
            self.is_closed = True

    root = Collection("PDF Import - input")
    collections = Collections([root])
    fake_bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(version=(5, 2, 0)),
        data=types.SimpleNamespace(
            collections=collections,
            objects=types.SimpleNamespace(remove=lambda *_args, **_kwargs: None),
        ),
        context=types.SimpleNamespace(
            scene=types.SimpleNamespace(collection=types.SimpleNamespace(children=Children())),
            view_layer=types.SimpleNamespace(update=lambda: None),
        ),
        types=sys.modules["bpy"].types,
        ops=sys.modules["bpy"].ops,
    )
    monkeypatch.setattr(engine, "bpy", fake_bpy)
    monkeypatch.setattr(engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Document())
    page2 = types.SimpleNamespace(
        page_number=2,
        primitives=[],
        text_items=[],
        width=25.4,
        height=25.4,
        resolved_scale=None,
    )
    streamed_pages: list[list[int]] = []

    def fake_iter_pages(_doc, pages, **_kwargs):
        streamed_pages.append(list(pages))
        yield 2, page2

    monkeypatch.setattr(engine, "iter_pages", fake_iter_pages)
    built_pages: list[int] = []
    monkeypatch.setattr(
        engine,
        "build_page",
        lambda data, *_args, **_kwargs: built_pages.append(data.page_number) or {},
    )
    monkeypatch.setattr(engine, "write_import_report", lambda *_args, **_kwargs: str(tmp_path / "report.json"))
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    config = {
        "mode": "vector",
        "pages": "1-2",
        "import_text": False,
        "ignore_images": True,
        "auto_focus_view": False,
        "auto_hide_default_cube": False,
    }
    checkpoint = tmp_path / "resume.json"
    state = session.build_resume_state(
        source_sha256=hashlib.sha256(input_pdf.read_bytes()).hexdigest(),
        config_sha256=session.resume_config_sha256(config),
        requested_pages=[1, 2],
        completed_pages=[1],
        root_collection=root.name,
        next_stack_offset_m=-0.0254,
        aggregate_stats={
            "pages_imported": 1,
            "primitives": 5,
            "collections": 2,
            "cancelled": True,
            "raster_pages_imported": 1,
            "geometry_delivery_issues": [
                {"page": 1, "status": "verified", "reason": "source_points"}
            ],
        },
        text_delivery_items=[],
    )
    session.write_resume_checkpoint(checkpoint, state)

    stats = engine.import_pdf(
        str(input_pdf),
        config={
            **config,
            "resume": True,
            "resume_checkpoint_path": str(checkpoint),
        },
    )

    assert streamed_pages == [[2]]
    assert built_pages == [2]
    assert stats["pages_imported"] == 2
    assert stats["primitives"] == 5
    assert stats["cancelled"] is False
    assert stats["raster_pages_imported"] == 1
    assert stats["geometry_delivery_issues"] == [
        {"page": 1, "status": "verified", "reason": "source_points"}
    ]
    assert collections.items.count(root) == 1
    assert not checkpoint.exists()


def test_import_report_carries_exact_build_module_host_and_source_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: a normal host result must identify the exact tested package."""
    _install_blender_stubs(monkeypatch)
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    monkeypatch.setattr(engine, "bpy", sys.modules["bpy"])
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4\nidentity fixture\n%%EOF\n")
    report_path = tmp_path / "report.json"
    identity = {
        "schema": "bcs.blender.package_identity/1.0",
        "status": "verified",
        "importer_version": "1.0.76",
        "source_commit": "1" * 40,
        "source_tag": "v1.0.76",
        "package_sha256": "2" * 64,
        "package_hash_kind": "installed_content_manifest_sha256",
        "modules": {
            "pdf_vector_importer.bl_import_engine": {
                "path": "pdf_vector_importer/bl_import_engine.py",
                "sha256": "3" * 64,
            },
            "pdf_vector_importer.bl_text_builder": {
                "path": "pdf_vector_importer/bl_text_builder.py",
                "sha256": "4" * 64,
            },
        },
    }
    monkeypatch.setattr(engine, "runtime_package_identity", lambda: identity, raising=False)
    monkeypatch.setattr(engine, "_pymupdf_version", lambda: "")
    report_module = importlib.import_module("pdf_vector_importer.pdfcadcore.import_report")
    monkeypatch.setattr(report_module, "_pdf_audit_extras", lambda _path: {})

    engine.write_import_report(
        str(source),
        {"import_text": False},
        {
            "pages_imported": 1,
            "primitives": 1,
            "text_items": 0,
            "collections": 1,
            "elapsed": 0.01,
        },
        import_mode="vector",
        output_path=str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["host"] == {"app": "blender", "version": "5.2.0"}
    assert len(report["input"]["sha256"]) == 64
    assert report["extra"]["package_identity"] == identity
