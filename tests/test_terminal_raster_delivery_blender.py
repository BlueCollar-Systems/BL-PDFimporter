"""Regression coverage for terminal raster delivery failures in Blender."""
from __future__ import annotations

import json
from pathlib import Path
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
        Object=object,
    )
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine, bl_text_builder
from pdf_vector_importer.pdfcadcore import fitz_loader
from pdf_vector_importer.text_delivery import AttemptOutcome

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore


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


class _RasterObject(dict):
    def __init__(self, name="PDF_Text_Raster"):
        super().__init__()
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(name=f"{name}_mesh", type="MESH")


class _MetadataRejectingRasterObject(_RasterObject):
    def __setitem__(self, key, value):
        del key, value
        raise RuntimeError("metadata write rejected after plane creation")


class _TextPixmap:
    width = 160
    height = 40
    samples = b"non-empty-raster"

    def save(self, path):
        Path(path).write_bytes(b"verified-png")


class _TransparentTextPixmap(_TextPixmap):
    samples = b"\x00" * 64

    def save(self, path):
        Path(path).write_bytes(b"verified-transparent-png")


class _TextRasterPage:
    def __init__(self):
        self.calls = []

    def get_pixmap(self, **kwargs):
        self.calls.append(kwargs)
        return _TextPixmap()


class _TransparentTextRasterPage(_TextRasterPage):
    def get_pixmap(self, **kwargs):
        self.calls.append(kwargs)
        return _TransparentTextPixmap()


class _RotatedTextRasterPage(_TextRasterPage):
    rotation = 90
    rotation_matrix = fitz.Matrix(0.0, 1.0, -1.0, 0.0, 3024.0, 0.0)


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


def _write_inline_image_pdf(path: Path, transforms):
    """Write a real one-page PDF whose images use BI/ID/EI, not XObjects."""
    document = fitz.open()
    page = document.new_page(width=100.0, height=100.0)
    content_xref = document.get_new_xref()
    document.update_object(content_xref, "<<>>")
    stream = bytearray()
    for a, b, c, d, e, f in transforms:
        stream.extend(f"q {a} {b} {c} {d} {e} {f} cm\n".encode("ascii"))
        stream.extend(b"BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI\nQ\n")
    document.update_stream(content_xref, bytes(stream))
    page.set_contents(content_xref)
    document.save(str(path))
    document.close()


def test_inline_image_is_imported_as_an_individual_exact_placement(tmp_path):
    pdf_path = tmp_path / "inline-image.pdf"
    _write_inline_image_pdf(pdf_path, [(20, 0, 0, 10, 10, 30)])

    document = fitz.open(str(pdf_path))
    try:
        page = document[0]
        inventory = page.get_image_info(hashes=True, xrefs=True)
        assert len(inventory) == 1
        assert inventory[0]["xref"] == 0
        placements = bl_import_engine._extract_image_placements(
            document,
            page,
            1,
            types.SimpleNamespace(flip_y=True, user_scale=1.0, raster_dpi=144),
            str(tmp_path),
        )
    finally:
        document.close()

    assert len(placements) == 1
    placement = placements[0]
    assert placement["source_kind"] == "inline"
    assert placement["source_image_number"] == 0
    assert placement["source_content_sha256"] == (
        "b9214335877109b56e1df370d4f375662e8a662d0aa9dd9d5cfb7498a8283c24"
    )
    assert Path(placement["path"]).is_file()
    expected_quad = [
        (3.5277777778, 10.5833333333),
        (10.5833333333, 10.5833333333),
        (10.5833333333, 14.1111111111),
        (3.5277777778, 14.1111111111),
    ]
    for actual, expected in zip(placement["quad_mm"], expected_quad, strict=True):
        assert actual == pytest.approx(expected)


def test_dense_inline_images_are_one_transparent_images_only_composite(tmp_path):
    pdf_path = tmp_path / "dense-inline-images.pdf"
    _write_inline_image_pdf(
        pdf_path,
        [(20, 0, 0, 10, 10, 30)] * 257,
    )

    document = fitz.open(str(pdf_path))
    try:
        placements = bl_import_engine._extract_image_placements(
            document,
            document[0],
            1,
            types.SimpleNamespace(flip_y=True, user_scale=1.0, raster_dpi=144),
            str(tmp_path),
        )
    finally:
        document.close()

    assert len(placements) == 1
    placement = placements[0]
    assert placement["composition"] == "images_only_transparent_composite"
    assert placement["source_kind"] == "inline_composite"
    assert placement["source_page_number"] == 1
    assert placement["source_image_count"] == 257
    assert placement["source_inline_image_count"] == 257
    assert len(placement["source_manifest_sha256"]) == 64
    assert placement["xref"] == -2
    composite = fitz.Pixmap(placement["path"])
    assert composite.alpha == 1
    assert composite.pixel(5, 5)[-1] == 0
    assert composite.pixel(30, 130)[-1] > 0


def test_rotated_embedded_image_placement_preserves_pdf_transform_and_uv_order(
    tmp_path,
):
    pdf_path = tmp_path / "rotated-image.pdf"
    document = fitz.open()
    page = document.new_page(width=792.0, height=612.0)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 1), False)
    pixmap.clear_with(0x336699)
    page.insert_image(page.rect, pixmap=pixmap, keep_proportion=False)
    page.set_rotation(90)
    document.save(str(pdf_path))
    document.close()

    document = fitz.open(str(pdf_path))
    try:
        page = document[0]
        placements = bl_import_engine._extract_image_placements(
            document,
            page,
            1,
            types.SimpleNamespace(flip_y=True, user_scale=1.0),
            str(tmp_path),
        )
    finally:
        document.close()

    assert len(placements) == 1
    placement = placements[0]
    # Vertex order is Blender UV order: image bottom-left, bottom-right,
    # top-right, top-left after the PDF image and page transforms.
    expected_quad = [
        (0.0, 279.4),
        (0.0, 0.0),
        (215.9, 0.0),
        (215.9, 279.4),
    ]
    for actual, expected in zip(placement["quad_mm"], expected_quad, strict=True):
        assert actual == pytest.approx(expected)
    assert placement["x_mm"] == pytest.approx(0.0)
    assert placement["y_mm"] == pytest.approx(0.0)
    assert placement["width_mm"] == pytest.approx(215.9)
    assert placement["height_mm"] == pytest.approx(279.4)


def test_embedded_image_transform_uses_crop_local_userunit_safe_page_rotation(
    tmp_path,
):
    pdf_path = tmp_path / "crop-rotate-userunit-image.pdf"
    document = fitz.open()
    page = document.new_page(width=800.0, height=600.0)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 1), False)
    pixmap.clear_with(0x336699)
    page.insert_image(
        fitz.Rect(120.0, 80.0, 520.0, 280.0),
        pixmap=pixmap,
        keep_proportion=False,
    )
    page.set_cropbox(fitz.Rect(100.0, 50.0, 700.0, 550.0))
    page.set_rotation(90)
    document.xref_set_key(page.xref, "UserUnit", "2")
    document.save(str(pdf_path))
    document.close()

    document = fitz.open(str(pdf_path))
    try:
        placements = bl_import_engine._extract_image_placements(
            document,
            document[0],
            1,
            types.SimpleNamespace(flip_y=True, user_scale=1.0),
            str(tmp_path),
        )
    finally:
        document.close()

    placement = placements[0]
    expected_quad = [
        (190.5, 409.2222222222),
        (190.5, 127.0),
        (331.6111111111, 127.0),
        (331.6111111111, 409.2222222222),
    ]
    for actual, expected in zip(placement["quad_mm"], expected_quad, strict=True):
        assert actual == pytest.approx(expected)
    assert placement["x_mm"] == pytest.approx(190.5)
    assert placement["y_mm"] == pytest.approx(127.0)
    assert placement["width_mm"] == pytest.approx(141.1111111111)
    assert placement["height_mm"] == pytest.approx(282.2222222222)


def test_image_plane_geometry_uses_transformed_quad_without_changing_uvs():
    placement = {
        "quad_mm": [
            (0.0, 279.4),
            (0.0, 0.0),
            (215.9, 0.0),
            (215.9, 279.4),
        ]
    }

    origin, vertices, face, uv_by_vertex = bl_import_engine._image_plane_geometry(
        placement
    )

    assert origin == pytest.approx((0.0, 0.2794))
    expected_vertices = [
        (0.0, 0.0, 0.0),
        (0.0, -0.2794, 0.0),
        (0.2159, -0.2794, 0.0),
        (0.2159, 0.0, 0.0),
    ]
    for actual, expected in zip(vertices, expected_vertices, strict=True):
        assert actual == pytest.approx(expected)
    assert face == (0, 1, 2, 3)
    assert uv_by_vertex == {
        0: (0.0, 0.0),
        1: (1.0, 0.0),
        2: (1.0, 1.0),
        3: (0.0, 1.0),
    }

    _, _, mirrored_face, mirrored_uvs = bl_import_engine._image_plane_geometry(
        {"quad_mm": [(0.0, 0.0), (-1.0, 0.0), (-1.0, 1.0), (0.0, 1.0)]}
    )
    assert mirrored_face == (0, 3, 2, 1)
    assert mirrored_uvs == uv_by_vertex


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
    with pytest.raises(bl_import_engine.IncompleteImportError) as caught:
        bl_import_engine.import_pdf(
            str(input_pdf),
            config={
                "mode": "raster",
                "pages": "1",
                "auto_focus_view": False,
                "auto_hide_default_cube": False,
            },
        )
    assert len(report_calls) == 1
    return caught.value.stats, report_calls[0]


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


def test_item_terminal_raster_is_clipped_and_placed_at_the_item_target_bbox(
    monkeypatch,
    tmp_path,
):
    page = _TextRasterPage()
    text_item = types.SimpleNamespace(
        id=41,
        text="WELD",
        source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
        bbox=(12.0, 24.0, 52.0, 30.0),
    )
    captured = []
    expected = _RasterObject()

    def _capture_plane(placement, collection, z_offset_m=0.0):
        captured.append((dict(placement), collection, z_offset_m))
        return expected

    monkeypatch.setattr(bl_import_engine, "_create_image_plane", _capture_plane)
    config = types.SimpleNamespace(raster_dpi=288)
    collection = object()

    actual = bl_import_engine._render_text_item_raster(
        page,
        text_item,
        collection,
        page_num=2,
        item_id="page:2:text:41",
        import_cfg=config,
        image_dir=str(tmp_path),
        z_offset_m=0.00035,
    )

    assert actual is expected
    assert len(page.calls) == 1
    assert len(captured) == 1
    placement, used_collection, used_z = captured[0]
    assert used_collection is collection
    assert used_z == 0.00035
    assert placement["x_mm"] == 12.0
    assert placement["y_mm"] == 24.0
    assert placement["width_mm"] == 40.0
    assert placement["height_mm"] == 6.0
    assert placement["source_bbox_pdf"] == [34.0, 50.0, 147.0, 68.0]
    assert placement["source_item_id"] == "page:2:text:41"
    assert Path(placement["path"]).read_bytes() == b"verified-png"
    assert expected["pdf_raster_source_item_id"] == "page:2:text:41"


def test_whitespace_terminal_raster_accepts_verified_exact_transparent_clip(
    monkeypatch,
    tmp_path,
):
    page = _TransparentTextRasterPage()
    source_bbox = (34.0, 50.0, 37.0, 68.0)
    text_item = types.SimpleNamespace(
        id=42,
        text=" \t ",
        source_bbox_pdf=source_bbox,
        bbox=(12.0, 24.0, 13.0, 30.0),
    )
    captured = []
    expected = _RasterObject()
    monkeypatch.setattr(
        bl_import_engine,
        "_create_image_plane",
        lambda placement, *_args, **_kwargs: captured.append(dict(placement))
        or expected,
    )

    actual = bl_import_engine._render_text_item_raster(
        page,
        text_item,
        object(),
        page_num=2,
        item_id="page:2:text:42",
        import_cfg=types.SimpleNamespace(raster_dpi=300),
        image_dir=str(tmp_path),
    )

    assert actual is expected
    assert len(page.calls) == 1
    assert tuple(page.calls[0]["clip"]) == pytest.approx(source_bbox)
    assert captured[0]["source_bbox_pdf"] == list(source_bbox)
    assert captured[0]["source_render_clip_pdf"] == list(source_bbox)
    assert captured[0]["source_expected_transparent"] is True
    assert expected["pdf_raster_expected_transparent"] is True
    assert Path(captured[0]["path"]).read_bytes() == b"verified-transparent-png"


def test_visible_text_terminal_raster_rejects_transparent_clip(monkeypatch, tmp_path):
    plane_calls = []
    monkeypatch.setattr(
        bl_import_engine,
        "_create_image_plane",
        lambda *_args, **_kwargs: plane_calls.append(True) or _RasterObject(),
    )

    actual = bl_import_engine._render_text_item_raster(
        _TransparentTextRasterPage(),
        types.SimpleNamespace(
            id=43,
            text="VISIBLE",
            source_bbox_pdf=(34.0, 50.0, 70.0, 68.0),
            bbox=(12.0, 24.0, 24.0, 30.0),
        ),
        object(),
        page_num=2,
        item_id="page:2:text:43",
        import_cfg=types.SimpleNamespace(raster_dpi=300),
        image_dir=str(tmp_path),
    )

    assert actual is None
    assert plane_calls == []
    assert list(tmp_path.glob("*.png")) == []


def test_item_terminal_raster_uses_attempt_owned_resources(monkeypatch, tmp_path):
    captured_kwargs = []

    def _capture_plane(_placement, _collection, **kwargs):
        captured_kwargs.append(kwargs)
        return _RasterObject()

    monkeypatch.setattr(bl_import_engine, "_create_image_plane", _capture_plane)

    actual = bl_import_engine._render_text_item_raster(
        _TextRasterPage(),
        types.SimpleNamespace(
            id=41,
            text="WELD",
            source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
            bbox=(12.0, 24.0, 52.0, 30.0),
        ),
        object(),
        page_num=2,
        item_id="page:2:text:41",
        import_cfg=types.SimpleNamespace(raster_dpi=288),
        image_dir=str(tmp_path),
        image_cache=object(),
    )

    assert actual is not None
    assert len(captured_kwargs) == 1
    assert "image_cache" not in captured_kwargs[0]
    assert "style_identity" not in captured_kwargs[0]


def test_item_terminal_raster_transforms_unrotated_source_bbox_for_page_clip(
    monkeypatch,
    tmp_path,
):
    page = _RotatedTextRasterPage()
    source_bbox = (1147.1431884765625, 697.7354125976562, 1163.6844482421875, 729.83837890625)
    text_item = types.SimpleNamespace(
        id=1,
        text="1HR",
        source_bbox_pdf=source_bbox,
        bbox=(404.688, 408.181, 416.015, 419.503),
    )
    captured = []
    expected = _RasterObject()
    monkeypatch.setattr(
        bl_import_engine,
        "_create_image_plane",
        lambda placement, *_args, **_kwargs: captured.append(dict(placement))
        or expected,
    )

    actual = bl_import_engine._render_text_item_raster(
        page,
        text_item,
        object(),
        page_num=2,
        item_id="page:2:text:1",
        import_cfg=types.SimpleNamespace(raster_dpi=300),
        image_dir=str(tmp_path),
    )

    assert actual is expected
    assert len(page.calls) == 1
    expected_clip = fitz.Rect(*source_bbox) * page.rotation_matrix
    actual_clip = page.calls[0]["clip"]
    assert tuple(actual_clip) == pytest.approx(tuple(expected_clip))
    assert captured[0]["source_bbox_pdf"] == list(source_bbox)
    assert captured[0]["source_render_clip_pdf"] == pytest.approx(
        list(expected_clip)
    )


def test_item_terminal_raster_metadata_failure_cleans_created_plane(monkeypatch, tmp_path):
    page = _TextRasterPage()
    text_item = types.SimpleNamespace(
        id=41,
        text="WELD",
        source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
        bbox=(12.0, 24.0, 52.0, 30.0),
    )
    plane = _MetadataRejectingRasterObject()
    cleanup_calls = []

    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: plane)
    monkeypatch.setattr(
        bl_import_engine,
        "_remove_created_image_plane",
        lambda obj, collection: cleanup_calls.append((obj, collection)) or {
            "status": "complete",
            "removed": [obj.name, obj.data.name],
        },
    )
    collection = object()

    actual = bl_import_engine._render_text_item_raster(
        page,
        text_item,
        collection,
        page_num=2,
        item_id="page:2:text:41",
        import_cfg=types.SimpleNamespace(raster_dpi=288),
        image_dir=str(tmp_path),
    )

    assert actual is None
    assert cleanup_calls == [(plane, collection)]
    assert list(tmp_path.glob("*.png")) == []


def test_item_terminal_raster_plane_failure_removes_owned_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: None)

    actual = bl_import_engine._render_text_item_raster(
        _TextRasterPage(),
        types.SimpleNamespace(
            id=41,
            text="WELD",
            source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
            bbox=(12.0, 24.0, 52.0, 30.0),
        ),
        object(),
        page_num=2,
        item_id="page:2:text:41",
        import_cfg=types.SimpleNamespace(raster_dpi=288),
        image_dir=str(tmp_path),
    )

    assert actual is None
    assert list(tmp_path.glob("*.png")) == []


def test_image_plane_constructor_rolls_back_object_and_mesh_after_partial_mutation(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "clip.png"
    image_path.write_bytes(b"png")

    class _Mesh:
        def __init__(self):
            self.name = "partial_mesh"
            self.uv_layers = types.SimpleNamespace(
                new=lambda **_kwargs: types.SimpleNamespace(data=[])
            )
            self.polygons = []
            self.loops = []
            self.materials = []

        def from_pydata(self, *_args):
            pass

        def update(self):
            pass

    class _Registry:
        def __init__(self, factory):
            self.factory = factory
            self.removed = []

        def new(self, *_args, **_kwargs):
            return self.factory()

        def remove(self, value, **_kwargs):
            self.removed.append(value.name)

    class _Object(dict):
        def __init__(self, mesh):
            super().__init__()
            self.name = "partial_object"
            self.data = mesh

    class _LinkedObjects:
        def __init__(self):
            self.items = []

        def link(self, value):
            self.items.append(value)

        def unlink(self, value):
            if value in self.items:
                self.items.remove(value)

    meshes = _Registry(_Mesh)
    objects = _Registry(lambda: None)
    objects.new = lambda _name, mesh: _Object(mesh)
    materials = types.SimpleNamespace(
        get=lambda _name: None,
        new=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("material creation failed")),
    )
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(meshes=meshes, objects=objects, materials=materials),
    )
    collection = types.SimpleNamespace(objects=_LinkedObjects())
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)

    result = bl_import_engine._create_image_plane(
        {
            "path": str(image_path),
            "x_mm": 0.0,
            "y_mm": 0.0,
            "width_mm": 10.0,
            "height_mm": 5.0,
            "xref": -41,
            "page_number": 2,
        },
        collection,
    )

    assert result is None
    assert collection.objects.items == []
    assert objects.removed == ["partial_object"]
    assert meshes.removed == ["partial_mesh"]


def test_image_plane_constructor_never_mutates_or_reuses_existing_resources(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "clip.png"
    image_path.write_bytes(b"png-bytes")

    class _Socket:
        def __init__(self, node):
            self.node = node

    class _Node:
        def __init__(self, node_type):
            self.type = {
                "ShaderNodeTexImage": "TEX_IMAGE",
                "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
                "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
            }.get(node_type, node_type)
            self.image = None
            self.outputs = {"Color": _Socket(self), "Alpha": _Socket(self), "BSDF": _Socket(self)}
            self.inputs = {"Base Color": _Socket(self), "Alpha": _Socket(self), "Surface": _Socket(self)}

    class _Nodes(list):
        def clear(self):
            super().clear()

        def new(self, *, type):
            node = _Node(type)
            self.append(node)
            return node

    class _Links(list):
        def new(self, source, target):
            self.append(types.SimpleNamespace(from_node=source.node, to_node=target.node))

    class _Material:
        def __init__(self, name, *, sentinel=False):
            self.name = name
            self.use_nodes = True
            self.node_tree = types.SimpleNamespace(nodes=_Nodes(), links=_Links())
            if sentinel:
                self.node_tree.nodes.append(_Node("SENTINEL"))
            self.users = 0

    class _MaterialRegistry:
        def __init__(self):
            self.sentinel = _Material("PDF_Image_Mat_2_-41", sentinel=True)
            self.created = []

        def get(self, name):
            return self.sentinel if name == self.sentinel.name else None

        def new(self, *, name):
            material = _Material(f"{name}.001")
            self.created.append(material)
            return material

        def remove(self, _value):
            pass

    class _PackedImage:
        def __init__(self, name, data):
            self.name = name
            self.packed_file = types.SimpleNamespace(data=data)
            self.users = 0

        def pack(self):
            pass

    class _ImageRegistry:
        def __init__(self):
            self.sentinel = _PackedImage("clip.png", b"png-bytes")
            self.created = []
            self.load_flags = []

        def __iter__(self):
            return iter([self.sentinel, *self.created])

        def load(self, path, *, check_existing=True):
            self.load_flags.append(check_existing)
            if check_existing:
                return self.sentinel
            image = _PackedImage("clip.png.001", Path(path).read_bytes())
            self.created.append(image)
            return image

        def remove(self, _value):
            pass

    class _UVEntry:
        uv = None

    class _Mesh:
        def __init__(self):
            self.name = "mesh"
            self.materials = []
            self.polygons = [types.SimpleNamespace(loop_indices=[0, 1, 2, 3])]
            self.loops = [types.SimpleNamespace(vertex_index=i) for i in range(4)]
            self.uv_layers = types.SimpleNamespace(
                new=lambda **_kwargs: types.SimpleNamespace(
                    data=[_UVEntry(), _UVEntry(), _UVEntry(), _UVEntry()]
                )
            )

        def from_pydata(self, *_args):
            pass

        def update(self):
            pass

    class _Object(dict):
        def __init__(self, name, mesh):
            super().__init__()
            self.name = name
            self.data = mesh
            self.location = (0.0, 0.0, 0.0)

    class _Registry:
        def __init__(self, new):
            self._new = new

        def new(self, *args, **kwargs):
            return self._new(*args, **kwargs)

        def remove(self, *_args, **_kwargs):
            pass

    materials = _MaterialRegistry()
    images = _ImageRegistry()
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            meshes=_Registry(lambda *_args, **_kwargs: _Mesh()),
            objects=_Registry(lambda name, mesh: _Object(name, mesh)),
            materials=materials,
            images=images,
        )
    )
    linked = []
    collection = types.SimpleNamespace(
        objects=types.SimpleNamespace(link=linked.append, unlink=lambda value: linked.remove(value))
    )
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)

    result = bl_import_engine._create_image_plane(
        {
            "path": str(image_path),
            "width_mm": 10.0,
            "height_mm": 5.0,
            "xref": -41,
            "page_number": 2,
        },
        collection,
    )

    assert result is not None
    assert images.load_flags == [False]
    assert len(materials.created) == 1
    assert len(images.created) == 1
    assert result.data.materials[0] is materials.created[0]
    assert result["pdf_image_material_owned"] is True
    assert result["pdf_image_datablock_owned"] is True
    assert [node.type for node in materials.sentinel.node_tree.nodes] == ["SENTINEL"]

    image_cache = bl_import_engine._ImportImageCache()
    first_placement = {
        "path": str(image_path),
        "x_mm": 10.0,
        "y_mm": 20.0,
        "width_mm": 10.0,
        "height_mm": 5.0,
        "xref": 44,
        "page_number": 3,
        "source_kind": "inline",
        "source_image_number": 7,
        "source_digest": "inline-digest-7",
        "composition": "individual_exact_placement",
        "source_image_count": 1,
        "source_inline_image_count": 1,
        "source_unique_content_count": 1,
        "source_content_sha256": (
            "994f1f8f87e80120b9315d83df2f9558d06c952a83f1f7f05719a6d3a0e96f89"
        ),
    }
    second_placement = dict(first_placement, x_mm=30.0, y_mm=40.0, source_image_number=8)
    cached_first = bl_import_engine._create_image_plane(
        first_placement,
        collection,
        image_cache=image_cache,
        style_identity=("source", "base-color-alpha", "hashed"),
    )
    cached_second = bl_import_engine._create_image_plane(
        second_placement,
        collection,
        image_cache=image_cache,
        style_identity=("source", "base-color-alpha", "hashed"),
    )

    assert cached_first is not cached_second
    assert cached_first.data is cached_second.data
    assert cached_first.location == (0.01, 0.02, 0.0)
    assert cached_second.location == (0.03, 0.04, 0.0)
    assert len(materials.created) == 2
    assert len(images.created) == 2
    assert cached_first["pdf_image_source_kind"] == "inline"
    assert cached_first["pdf_image_source_page_number"] == 3
    assert cached_first["pdf_image_source_number"] == 7
    assert cached_first["pdf_image_source_digest"] == "inline-digest-7"
    assert cached_first["pdf_image_composition"] == "individual_exact_placement"
    assert cached_first["pdf_image_source_inline_count"] == 1
    assert cached_first["pdf_image_source_unique_content_count"] == 1
    assert cached_second["pdf_image_source_number"] == 8

    different_style = bl_import_engine._create_image_plane(
        first_placement,
        collection,
        image_cache=image_cache,
        style_identity=("blueprint", "base-color-alpha", "hashed"),
    )
    assert different_style.data is not cached_first.data
    assert len(materials.created) == 3
    assert len(images.created) == 2

    next_import = bl_import_engine._ImportImageCache()
    isolated = bl_import_engine._create_image_plane(
        first_placement,
        collection,
        image_cache=next_import,
        style_identity=("source", "base-color-alpha", "hashed"),
    )
    assert isolated.data is not cached_first.data
    assert len(materials.created) == 4
    assert len(images.created) == 3


def test_remove_created_image_plane_removes_all_owned_datablocks(monkeypatch):
    class _Registry:
        def __init__(self, value):
            self.value = value
            self.removed = []

        def get(self, name):
            return self.value if name == self.value.name else None

        def remove(self, value, **_kwargs):
            self.removed.append(value.name)

    mesh = types.SimpleNamespace(name="mesh", type="MESH")
    material = types.SimpleNamespace(name="material", users=0)
    image = types.SimpleNamespace(name="image", users=0)
    obj = _RasterObject()
    obj.data = mesh
    obj["pdf_image_material"] = material.name
    obj["pdf_image_material_owned"] = True
    obj["pdf_image_datablock"] = image.name
    obj["pdf_image_datablock_owned"] = True
    objects = _Registry(obj)
    meshes = _Registry(mesh)
    materials = _Registry(material)
    images = _Registry(image)
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            objects=objects,
            meshes=meshes,
            materials=materials,
            images=images,
        )
    )
    unlinked = []
    collection = types.SimpleNamespace(
        objects=types.SimpleNamespace(unlink=lambda value: unlinked.append(value.name))
    )
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)

    cleanup = bl_import_engine._remove_created_image_plane(obj, collection)

    assert cleanup["status"] == "complete"
    assert objects.removed == [obj.name]
    assert meshes.removed == [mesh.name]
    assert materials.removed == [material.name]
    assert images.removed == [image.name]


def test_remove_cached_image_plane_keeps_shared_datablocks(monkeypatch):
    class _Registry:
        def __init__(self, value):
            self.value = value
            self.removed = []

        def get(self, name):
            return self.value if name == self.value.name else None

        def remove(self, value, **_kwargs):
            self.removed.append(value.name)

    mesh = types.SimpleNamespace(name="shared-mesh", type="MESH")
    material = types.SimpleNamespace(name="shared-material", users=1)
    image = types.SimpleNamespace(name="shared-image", users=1)
    obj = _RasterObject()
    obj.data = mesh
    obj["pdf_image_mesh_owned"] = False
    obj["pdf_image_material"] = material.name
    obj["pdf_image_material_owned"] = False
    obj["pdf_image_datablock"] = image.name
    obj["pdf_image_datablock_owned"] = False
    objects = _Registry(obj)
    meshes = _Registry(mesh)
    materials = _Registry(material)
    images = _Registry(image)
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            objects=objects,
            meshes=meshes,
            materials=materials,
            images=images,
        )
    )
    unlinked = []
    collection = types.SimpleNamespace(
        objects=types.SimpleNamespace(unlink=lambda value: unlinked.append(value.name))
    )
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)

    cleanup = bl_import_engine._remove_created_image_plane(obj, collection)

    assert cleanup["status"] == "complete"
    assert objects.removed == [obj.name]
    assert meshes.removed == []
    assert materials.removed == []
    assert images.removed == []


def test_page_raster_strategy_does_not_suppress_requested_structural_text(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    page_data = _page_data()
    page_data.text_items = [types.SimpleNamespace(id=9, text="WELD")]
    build_calls = []

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: page_data)
    monkeypatch.setattr(bl_import_engine, "_render_page_raster", lambda *_args, **_kwargs: _raster_placement())
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(
        bl_import_engine,
        "write_import_report",
        lambda *_args, **_kwargs: str(tmp_path / "import_report.json"),
    )

    def _capture_text(*args, **kwargs):
        build_calls.append((args, kwargs))
        kwargs["provenance_opts"]._text_delivery_records = [{
            "item_id": "page:1:text:9",
            "page": 1,
            "source_span_id": 9,
            "requested_representation": "text",
            "final_representation": "text",
            "status": "delivered",
            "fallback_used": False,
            "entity_ids": ["Text_9"],
            "attempts": [],
        }]
        return 1

    monkeypatch.setattr(bl_import_engine, "build_all_text", _capture_text)
    monkeypatch.setattr(
        bl_import_engine,
        "_reverify_text_delivery_after_stack",
        lambda *_args, **_kwargs: [],
    )
    stats = bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "raster",
            "pages": "1",
            "import_text": True,
            "text_mode": "text",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )

    assert stats["text_items"] == 1
    assert len(build_calls) == 1
    assert build_calls[0][1]["text_mode"] == "text"
    assert callable(build_calls[0][1]["terminal_raster_callback"])


def test_page_raster_composition_excludes_only_text_that_was_delivered_separately(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    delivered = types.SimpleNamespace(
        id=9,
        text="WELD",
        source_bbox_pdf=(10.0, 12.0, 30.0, 24.0),
    )
    failed = types.SimpleNamespace(
        id=10,
        text="KEEP IN BACKGROUND",
        source_bbox_pdf=(40.0, 12.0, 68.0, 24.0),
    )
    page_data = _page_data()
    page_data.text_items = [delivered, failed]
    captured = []

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: page_data)
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(
        bl_import_engine,
        "write_import_report",
        lambda *_args, **_kwargs: str(tmp_path / "import_report.json"),
    )

    def _build_text(_items, _collection, _page, **kwargs):
        opts = kwargs["provenance_opts"]
        opts._text_delivery_records = [
            {
                "page": 1,
                "source_span_id": 9,
                "status": "delivered",
                "final_representation": "text",
                "entity_ids": ["Text_9"],
                "attempts": [],
            },
            {
                "page": 1,
                "source_span_id": 10,
                "status": "failed",
                "final_representation": None,
                "entity_ids": [],
                "attempts": [],
            },
        ]
        return 1

    def _capture_render(*_args, **kwargs):
        captured.append(kwargs)
        return _raster_placement()

    monkeypatch.setattr(bl_import_engine, "build_all_text", _build_text)
    monkeypatch.setattr(bl_import_engine, "_render_page_raster", _capture_render)

    with pytest.raises(bl_import_engine.IncompleteImportError):
        bl_import_engine.import_pdf(
            str(input_pdf),
            config={
                "mode": "raster",
                "pages": "1",
                "import_text": True,
                "text_mode": "text",
                "auto_focus_view": False,
                "auto_hide_default_cube": False,
            },
        )

    assert captured[-1]["excluded_text_bboxes"] == [(10.0, 12.0, 30.0, 24.0)]


def test_page_raster_redaction_removes_text_but_keeps_nontext_graphics(tmp_path):
    document = fitz.open()
    page = document.new_page(width=200.0, height=100.0)
    page.insert_text((10.0, 30.0), "DUPLICATE", fontsize=18.0, color=(0, 0, 0))
    text_bbox = tuple(page.search_for("DUPLICATE")[0])
    page.draw_rect(fitz.Rect(150.0, 70.0, 180.0, 90.0), color=(0, 0, 0), fill=(0, 0, 0))
    config = types.SimpleNamespace(raster_dpi=144, user_scale=1.0)

    placement = bl_import_engine._render_page_raster(
        page,
        1,
        config,
        str(tmp_path),
        excluded_text_bboxes=[text_bbox],
    )

    assert placement is not None
    assert placement["excluded_text_bbox_count"] == 1
    assert placement["composition"] == "page_background_without_delivered_text"
    pixmap = fitz.Pixmap(placement["path"])

    def dark_pixels(rect):
        scale = 2
        x0, y0, x1, y1 = (int(round(value * scale)) for value in rect)
        count = 0
        samples = pixmap.samples
        channels = pixmap.n
        for y in range(max(0, y0), min(pixmap.height, y1)):
            for x in range(max(0, x0), min(pixmap.width, x1)):
                offset = (y * pixmap.width + x) * channels
                if min(samples[offset:offset + min(3, channels)]) < 80:
                    count += 1
        return count

    assert dark_pixels(text_bbox) == 0
    assert dark_pixels((150.0, 70.0, 180.0, 90.0)) > 500
    document.close()


def test_post_stack_reverification_binds_final_entity_locations(monkeypatch):
    obj = types.SimpleNamespace(
        name="Text_9",
        type="FONT",
        location=[0.01, -0.08, 0.0],
    )
    monkeypatch.setattr(
        bl_import_engine,
        "bpy",
        types.SimpleNamespace(
            data=types.SimpleNamespace(
                objects=types.SimpleNamespace(get=lambda name: obj if name == obj.name else None)
            )
        ),
    )
    record = {
        "page": 2,
        "status": "delivered",
        "final_representation": "text",
        "entity_ids": ["Text_9"],
        "attempts": [{
            "status": "delivered",
            "evidence": {"actual_location_m": [0.01, 0.02]},
        }],
    }

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=-0.10,
    )

    assert failures == []
    proof = record["final_state_verification"]
    assert proof["status"] == "verified"
    assert proof["stack_offset_m"] == -0.10
    assert proof["entities"][0]["actual_location_m"] == [0.01, -0.08, 0.0]


def test_post_stack_failure_cleans_owned_delivery_and_repairs_runtime_truth(monkeypatch):
    class Registry:
        def __init__(self, values=()):
            self.items = {value.name: value for value in values}
            self.removed = []

        def get(self, name):
            return self.items.get(name)

        def remove(self, value, **_kwargs):
            self.items.pop(value.name, None)
            self.removed.append(value.name)

    class Object(dict):
        def __init__(self, name, data):
            super().__init__()
            self.name = name
            self.type = "MESH"
            self.location = [0.01, -0.08, 0.0]
            self.data = data

    data = types.SimpleNamespace(name="Text_9_mesh", type="MESH")
    obj = Object("Text_9", data)
    objects = Registry([obj])
    meshes = Registry([data])
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            objects=objects,
            meshes=meshes,
            curves=Registry(),
            materials=Registry(),
            images=Registry(),
            fonts=Registry(),
        ),
        context=types.SimpleNamespace(
            view_layer=types.SimpleNamespace(update=lambda: None),
        ),
    )
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)
    monkeypatch.setattr(bl_text_builder, "bpy", fake_bpy)

    item_id = "page:2:text:9"
    outcome = AttemptOutcome.delivered(
        obj,
        entity_ids=(obj.name,),
        owned_objects=(obj,),
        owned_datablocks=(data,),
    )
    provenance = types.SimpleNamespace(page=2, span_id=9)
    opts = types.SimpleNamespace(
        _text_delivery_outcomes={item_id: outcome},
        _text_delivered_entity_counts={"native_text": 1},
        _source_provenance_objects=[provenance],
    )
    record = {
        "item_id": item_id,
        "page": 2,
        "source_span_id": 9,
        "status": "delivered",
        "requested_representation": "text",
        "final_representation": "text",
        "fallback_attempted": False,
        "fallback_used": False,
        "entity_ids": [obj.name],
        "attempts": [{
            "status": "delivered",
            "evidence": {"actual_location_m": [0.01, 0.02]},
        }],
    }

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=-0.10,
        provenance_opts=opts,
    )

    assert len(failures) == 1
    assert failures[0]["cleanup"]["status"] == "complete"
    assert record["status"] == "failed"
    assert record["reason"] == "post_stack_final_state_verification_failed"
    assert record["final_representation"] is None
    assert record["entity_ids"] == []
    assert record["fallback_used"] is False
    assert objects.removed == [obj.name]
    assert meshes.removed == [data.name]
    assert opts._text_delivered_entity_counts["native_text"] == 0
    assert opts._source_provenance_objects == []
    assert opts._text_delivery_outcomes == {}


def test_post_stack_cleanup_exception_stays_reportable_and_preserves_orphan_id(
    monkeypatch,
):
    obj = types.SimpleNamespace(
        name="Text_9",
        type="MESH",
        location=[0.01, -0.08, 0.0],
    )
    monkeypatch.setattr(
        bl_import_engine,
        "bpy",
        types.SimpleNamespace(
            data=types.SimpleNamespace(
                objects=types.SimpleNamespace(
                    get=lambda name: obj if name == obj.name else None
                )
            ),
            context=types.SimpleNamespace(
                view_layer=types.SimpleNamespace(update=lambda: None),
            ),
        ),
    )
    monkeypatch.setattr(
        bl_import_engine,
        "cleanup_delivery_outcome",
        lambda _outcome: (_ for _ in ()).throw(RuntimeError("cleanup blocked")),
    )
    item_id = "page:2:text:9"
    opts = types.SimpleNamespace(
        _text_delivery_outcomes={item_id: object()},
        _text_delivered_entity_counts={"native_text": 1},
    )
    record = {
        "item_id": item_id,
        "page": 2,
        "source_span_id": 9,
        "status": "delivered",
        "final_representation": "text",
        "fallback_used": False,
        "entity_ids": [obj.name],
        "attempts": [{
            "status": "delivered",
            "evidence": {"actual_location_m": [0.01, 0.02]},
        }],
    }

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=-0.10,
        provenance_opts=opts,
    )

    assert failures[0]["cleanup"]["status"] == "failed"
    assert failures[0]["cleanup"]["exception_type"] == "RuntimeError"
    assert record["status"] == "failed"
    assert record["entity_ids"] == [obj.name]
    assert opts._text_delivery_outcomes[item_id] is not None
    assert opts._text_delivered_entity_counts["native_text"] == 0


def test_page_stacking_moves_only_roots_when_text_uses_affine_carrier():
    carrier = types.SimpleNamespace(name="Text_9_Affine", parent=None, location=[0.0, 0.0, 0.0])
    text = types.SimpleNamespace(
        name="Text_9",
        parent=carrier,
        location=[0.01, 0.02, 0.0],
    )
    geometry = types.SimpleNamespace(
        name="Line_1",
        parent=None,
        location=[0.03, 0.04, 0.0],
    )

    moved = bl_import_engine._stack_page_objects(
        [carrier, text, geometry],
        -0.10,
    )

    assert moved == 2
    assert carrier.location == [0.0, -0.10, 0.0]
    assert text.location == [0.01, 0.02, 0.0]
    assert geometry.location[0] == 0.03
    assert geometry.location[1] == pytest.approx(-0.06)
    assert geometry.location[2] == 0.0


def test_post_stack_reverification_reads_parented_entity_world_location(monkeypatch):
    obj = types.SimpleNamespace(
        name="Text_9",
        type="FONT",
        location=[0.01, 0.02, 0.0],
        matrix_world=types.SimpleNamespace(translation=[0.01, -0.08, 0.0]),
    )
    monkeypatch.setattr(
        bl_import_engine,
        "bpy",
        types.SimpleNamespace(
            data=types.SimpleNamespace(
                objects=types.SimpleNamespace(get=lambda name: obj if name == obj.name else None)
            ),
            context=types.SimpleNamespace(
                view_layer=types.SimpleNamespace(update=lambda: None),
            ),
        ),
    )
    record = {
        "page": 2,
        "status": "delivered",
        "final_representation": "text",
        "entity_ids": ["Text_9"],
        "attempts": [{
            "status": "delivered",
            "evidence": {"actual_location_m": [0.01, 0.02]},
        }],
    }

    failures = bl_import_engine._reverify_text_delivery_after_stack(
        [record],
        page_number=2,
        stack_offset_m=-0.10,
    )

    assert failures == []
    proof = record["final_state_verification"]
    assert proof["status"] == "verified"
    assert proof["entities"][0]["actual_location_m"] == [0.01, -0.08, 0.0]


def test_owned_import_image_temp_directory_is_removed_after_packed_delivery(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    image_dir = tmp_path / "bc_bl_pdf_images_owned"
    image_dir.mkdir()
    (image_dir / "packed-source.png").write_bytes(b"temporary")

    class _CacheProbe:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    image_cache = _CacheProbe()
    seen_caches = []

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: _page_data())
    monkeypatch.setattr(bl_import_engine, "_render_page_raster", lambda *_args, **_kwargs: _raster_placement())
    monkeypatch.setattr(bl_import_engine, "_ImportImageCache", lambda: image_cache)

    def _capture_image_plane(*_args, **kwargs):
        seen_caches.append(kwargs.get("image_cache"))
        return object()

    monkeypatch.setattr(bl_import_engine, "_create_image_plane", _capture_image_plane)
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(image_dir))
    monkeypatch.setattr(
        bl_import_engine,
        "write_import_report",
        lambda *_args, **_kwargs: str(tmp_path / "import_report.json"),
    )

    bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "raster",
            "pages": "1",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )

    assert not image_dir.exists()
    assert seen_caches == [image_cache]
    assert image_cache.released is True


def test_dense_composite_stats_preserve_source_image_instance_count(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    placement = {
        "path": str(image_dir / "images-only.png"),
        "width_mm": 25.4,
        "height_mm": 25.4,
        "xref": -2,
        "page_number": 1,
        "source_kind": "inline_composite",
        "composition": "images_only_transparent_composite",
        "source_image_count": 257,
        "source_inline_image_count": 257,
        "source_manifest_sha256": "a" * 64,
    }

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: _page_data())
    monkeypatch.setattr(bl_import_engine, "build_page", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(bl_import_engine, "build_all_text", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        bl_import_engine,
        "_extract_image_placements",
        lambda *_args, **_kwargs: [placement],
    )
    monkeypatch.setattr(bl_import_engine, "_create_image_plane", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(image_dir))
    monkeypatch.setattr(
        bl_import_engine,
        "write_import_report",
        lambda *_args, **_kwargs: str(tmp_path / "import_report.json"),
    )

    stats = bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "vector",
            "pages": "1",
            "import_text": False,
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )

    assert stats["images"] == 1
    assert stats["image_source_instances"] == 257
    assert stats["inline_image_source_instances"] == 257
    assert stats["image_composites"] == 1


def test_import_stats_exclude_post_stack_failed_text(monkeypatch, tmp_path):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    image_dir = tmp_path / "images"
    image_dir.mkdir()

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: _page_data())
    monkeypatch.setattr(
        bl_import_engine,
        "build_page",
        lambda *_args, **_kwargs: {
            "curves": 0,
            "meshes": 0,
            "circles": 0,
            "arcs": 0,
            "skipped_fill_only": 0,
            "model3d_solids": 0,
        },
    )
    monkeypatch.setattr(bl_import_engine, "build_all_text", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(bl_import_engine, "_extract_image_placements", lambda *_args: [])
    monkeypatch.setattr(
        bl_import_engine,
        "_reverify_text_delivery_after_stack",
        lambda *_args, **_kwargs: [{"item_id": "page:1:text:1"}],
    )
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(image_dir))
    monkeypatch.setattr(
        bl_import_engine,
        "write_import_report",
        lambda *_args, **_kwargs: str(tmp_path / "import_report.json"),
    )

    stats = bl_import_engine.import_pdf(
        str(input_pdf),
        config={
            "mode": "vector",
            "pages": "1",
            "import_text": True,
            "text_mode": "text",
            "auto_focus_view": False,
            "auto_hide_default_cube": False,
        },
    )

    assert stats["text_items"] == 0
    assert stats["text_final_state_failures"] == [{"item_id": "page:1:text:1"}]


def test_import_pdf_raises_after_report_when_any_required_text_item_is_undelivered(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    report_path = tmp_path / "import_report.json"
    page_data = _page_data()
    page_data.text_items = [
        types.SimpleNamespace(id=1, text="WELD", bbox=(0.0, 0.0, 10.0, 2.0))
    ]

    monkeypatch.setattr(bl_import_engine, "bpy", _FakeBpy())
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: _Document())
    monkeypatch.setattr(bl_import_engine, "extract_page", lambda *_args, **_kwargs: page_data)
    monkeypatch.setattr(bl_import_engine, "build_page", lambda *_args, **_kwargs: {})

    def fail_text_delivery(_items, _collection, page_number, **kwargs):
        provenance_opts = kwargs["provenance_opts"]
        provenance_opts._text_delivery_records = [{
            "item_id": f"page:{page_number}:text:1",
            "page": page_number,
            "source_span_id": 1,
            "requested_representation": "text",
            "attempts": [{
                "attempt_index": 0,
                "attempted_representation": "text",
                "status": "failed",
                "reason": "requested_font_representation_visual_verification_failed",
                "evidence": {},
                "entity_ids": [],
                "owned_artifacts": [],
                "superseded": True,
                "cleanup": {"status": "complete", "removed": []},
            }],
            "final_representation": None,
            "status": "failed",
            "fallback_attempted": False,
            "fallback_used": False,
            "entity_ids": [],
        }]
        return 0

    def write_report(_filepath, _config, _stats, **_kwargs):
        report_path.write_text('{"extra":{"result_status":"incomplete"}}', encoding="utf-8")
        return str(report_path)

    monkeypatch.setattr(bl_import_engine, "build_all_text", fail_text_delivery)
    monkeypatch.setattr(bl_import_engine, "_extract_image_placements", lambda *_args: [])
    monkeypatch.setattr(bl_import_engine.tempfile, "mkdtemp", lambda **_kwargs: str(image_dir))
    monkeypatch.setattr(bl_import_engine, "write_import_report", write_report)

    with pytest.raises(bl_import_engine.IncompleteImportError) as caught:
        bl_import_engine.import_pdf(
            str(input_pdf),
            config={
                "mode": "vector",
                "pages": "1",
                "import_text": True,
                "text_mode": "text",
                "auto_focus_view": False,
                "auto_hide_default_cube": False,
            },
        )

    assert report_path.is_file()
    assert caught.value.stats["text_delivery_source_items"] == 1
    assert caught.value.stats["text_delivery_delivered_items"] == 0
    assert caught.value.stats["text_delivery_failed_items"] == 1
    assert caught.value.report_path == str(report_path)


def test_unknown_text_representation_fails_before_root_collection_creation(
    monkeypatch,
    tmp_path,
):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    fake_bpy = _FakeBpy()
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)
    monkeypatch.setattr(fitz_loader, "import_fitz", lambda **_kwargs: object())
    monkeypatch.setattr(
        fitz_loader,
        "safe_open",
        lambda _path: (_ for _ in ()).throw(AssertionError("PDF opened after invalid mode")),
    )

    import pytest

    with pytest.raises(ValueError, match="Unknown requested representation"):
        bl_import_engine.import_pdf(
            str(input_pdf),
            config={
                "mode": "vector",
                "import_text": True,
                "text_mode": "typo_mode",
                "auto_hide_default_cube": False,
            },
        )

    assert fake_bpy.data.collections.items == []


def test_strict_text_fidelity_false_is_rejected_before_import_mutation(monkeypatch, tmp_path):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-1.7\n")
    fake_bpy = _FakeBpy()
    monkeypatch.setattr(bl_import_engine, "bpy", fake_bpy)
    monkeypatch.setattr(bl_import_engine, "check_pymupdf", lambda: True)
    monkeypatch.setattr(bl_import_engine, "ensure_lib_path", lambda: None)

    import pytest

    with pytest.raises(ValueError, match="strict_text_fidelity cannot be disabled"):
        bl_import_engine.import_pdf(
            str(input_pdf),
            config={
                "strict_text_fidelity": False,
                "auto_hide_default_cube": False,
            },
        )

    assert fake_bpy.data.collections.items == []


def test_terminal_raster_failure_is_loud_and_not_counted_as_text_fallback(
    monkeypatch,
    tmp_path,
):
    report_path = tmp_path / "import_report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")
    provenance_opts = types.SimpleNamespace(
        _text_mode_fallbacks=[{
            "requested": "text",
            "delivered": "raster",
            "reason": "exact_source_font_unavailable_for_item",
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
        {"import_text": True, "text_mode": "text"},
        stats,
        import_mode="raster",
        output_path=str(report_path),
        provenance_opts=provenance_opts,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["fallback"] == {"used": False, "reason": None}
    assert report["extra"]["raster_delivery_failures"] == stats["raster_delivery_failures"]
    assert report["extra"]["result_status"] == "incomplete"
    assert set(report["extra"]["terminal_failure"]) == {
        "raster_delivery",
        "text_delivery",
    }
    assert "raster_delivery_failed" in report["extra"]["diagnostics"]["signals"]
    assert report["result"]["warnings"] == 2


def test_report_preserves_every_item_attempt_and_summarizes_failures_loudly(
    monkeypatch,
    tmp_path,
):
    report_path = tmp_path / "import_report.json"
    monkeypatch.setattr(bl_import_engine, "_pymupdf_version", lambda: "")
    records = [
        {
            "item_id": "page:1:text:1",
            "page": 1,
            "source_span_id": 1,
            "requested_representation": "text",
            "final_representation": "text",
            "status": "delivered",
            "fallback_used": False,
            "entity_ids": ["P1_text_1"],
            "attempts": [{
                "attempt_index": 0,
                "attempted_representation": "text",
                "status": "delivered",
                "reason": "verified",
                "evidence": {"actual_object_type": "FONT"},
                "entity_ids": ["P1_text_1"],
                "owned_artifacts": [],
                "superseded": False,
                "cleanup": {"status": "not_required", "removed": []},
            }],
        },
        {
            "item_id": "page:1:text:2",
            "page": 1,
            "source_span_id": 2,
            "requested_representation": "text",
            "final_representation": "raster",
            "status": "delivered",
            "fallback_used": True,
            "entity_ids": ["P1_text_2_raster"],
            "attempts": [
                {
                    "attempt_index": 0,
                    "attempted_representation": "text",
                    "status": "impossible",
                    "reason": "exact_source_font_unavailable_for_item",
                    "evidence": {"source_xref": 9},
                    "entity_ids": [],
                    "owned_artifacts": [],
                    "superseded": True,
                    "cleanup": {"status": "complete", "removed": []},
                },
                {
                    "attempt_index": 4,
                    "attempted_representation": "raster",
                    "status": "delivered",
                    "reason": "verified",
                    "evidence": {"actual_object_type": "MESH"},
                    "entity_ids": ["P1_text_2_raster"],
                    "owned_artifacts": [],
                    "superseded": False,
                    "cleanup": {"status": "not_required", "removed": []},
                },
            ],
        },
        {
            "item_id": "page:1:text:3",
            "page": 1,
            "source_span_id": 3,
            "requested_representation": "text",
            "final_representation": None,
            "status": "failed",
            "fallback_used": True,
            "entity_ids": [],
            "attempts": [{
                "attempt_index": 0,
                "attempted_representation": "text",
                "status": "failed",
                "reason": "font_object_creation_failed_not_impossibility_proof",
                "evidence": {"exception_type": "RuntimeError"},
                "entity_ids": [],
                "owned_artifacts": [],
                "superseded": True,
                "cleanup": {"status": "complete", "removed": []},
            }],
        },
    ]
    provenance_opts = types.SimpleNamespace(_text_delivery_records=records)
    stats = {
        "pages_imported": 1,
        "primitives": 5,
        "text_items": 2,
        "text_source_spans": 3,
        "collections": 1,
        "elapsed": 0.01,
    }

    bl_import_engine.write_import_report(
        str(tmp_path / "input.pdf"),
        {"import_text": True, "text_mode": "text"},
        stats,
        import_mode="vector",
        output_path=str(report_path),
        provenance_opts=provenance_opts,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    delivery = report["extra"]["text_delivery"]
    assert delivery["schema"] == "bcs.text_delivery/1.0"
    assert delivery["summary"] == {
        "source_items": 3,
        "delivered_items": 2,
        "fallback_items": 1,
        "failed_items": 1,
        "requested_counts": {"text": 3},
        "final_counts": {"raster": 1, "text": 1},
        "failed_item_ids": ["page:1:text:3"],
    }
    assert delivery["items"] == records
    assert report["fallback"]["used"] is True
    assert report["fallback"]["text"] == {
        "requested": "text",
        "delivered": "raster",
        "reason": "exact_source_font_unavailable_for_item",
        "count": 1,
    }
    signals = report["extra"]["diagnostics"]["signals"]
    assert "text_representation_fallback_used" in signals
    assert "text_delivery_failed" in signals


def test_auto_raster_delivery_failure_marks_fallback_attempted_not_used(monkeypatch, tmp_path):
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
    assert report["fallback"] == {"used": False, "reason": None}
    assert report["extra"]["fallback_attempted"] is True
    assert report["extra"]["result_status"] == "incomplete"


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
