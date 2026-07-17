"""Requested-representation invariant gates for Blender text delivery."""
from __future__ import annotations

import builtins
import math
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
import types

import pytest


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(
        types=types.SimpleNamespace(
            Collection=object,
            Material=object,
            Object=object,
            VectorFont=object,
        )
    )

from pdf_vector_importer import bl_text_builder
from pdf_vector_importer.pdfcadcore.primitives import NormalizedText, TextCharLayout
from pdf_vector_importer.text_delivery import AttemptOutcome, deliver_item, fallback_ladder


class _MaterialList(list):
    pass


class _FontData:
    type = "FONT"

    def __init__(self, name: str, *, baseline_available: bool = True):
        self.name = name
        self.body = ""
        self.size = 0.0
        self.extrude = 0.0
        self.resolution_u = 12
        self.align_x = ""
        self._align_y = ""
        self._baseline_available = baseline_available
        self.materials = _MaterialList()
        self.font = None

    @property
    def align_y(self):
        return self._align_y

    @align_y.setter
    def align_y(self, value):
        if value == "BOTTOM_BASELINE" and not self._baseline_available:
            raise TypeError("BOTTOM_BASELINE enum unavailable")
        self._align_y = value


class _CurveData:
    type = "CURVE"

    def __init__(self, name: str, base_dimensions=None):
        self.name = name
        self.splines = [object(), object()]
        self.materials = _MaterialList()
        self.base_dimensions = base_dimensions

    def copy(self):
        copied = _CurveData(f"{self.name}_copy", self.base_dimensions)
        copied.materials.extend(self.materials)
        return copied


class _MeshData:
    type = "MESH"

    def __init__(self, name: str, base_dimensions=None):
        self.name = name
        self.materials = _MaterialList()
        self.vertices = [object()]
        self.polygons = [object()]
        self.base_dimensions = base_dimensions


class _UVLayer:
    def __init__(self):
        self.data = [object(), object(), object(), object()]


class _UVLayers:
    def __init__(self, valid=True):
        self._layer = _UVLayer() if valid else None

    def get(self, name):
        return self._layer if name == "UVMap" else None


class _Socket:
    def __init__(self, node):
        self.node = node
        self.default_value = None


class _Node:
    def __init__(self, node_type, *, image=None):
        self.type = {
            "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
            "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
            "ShaderNodeTexImage": "TEX_IMAGE",
        }.get(node_type, node_type)
        self.image = image
        self.inputs = {
            "Base Color": _Socket(self),
            "Alpha": _Socket(self),
            "Surface": _Socket(self),
        }
        self.outputs = {
            "Color": _Socket(self),
            "Alpha": _Socket(self),
            "BSDF": _Socket(self),
        }


class _Nodes(list):
    def new(self, *, type):
        node = _Node(type)
        self.append(node)
        return node


class _Link:
    def __init__(self, from_node, to_node):
        self.from_node = from_node
        self.to_node = to_node


class _Links(list):
    def new(self, source, target):
        self.append(_Link(source.node, target.node))


class _Material:
    def __init__(self, name, image=None, *, valid_links=True):
        self.name = name
        self.use_nodes = True
        self.users = 0
        self.diffuse_color = (1.0, 0.0, 1.0, 1.0)
        nodes = _Nodes()
        links = _Links()
        if image is not None:
            texture = _Node("TEX_IMAGE", image=image)
            shader = _Node("BSDF_PRINCIPLED")
            output = _Node("OUTPUT_MATERIAL")
            nodes.extend((texture, shader, output))
            if valid_links:
                links.extend((_Link(texture, shader), _Link(shader, output)))
        self.node_tree = types.SimpleNamespace(nodes=nodes, links=links)


class _Materials:
    def __init__(self):
        self.items = {}
        self.removed = []

    def add(self, material):
        self.items[material.name] = material
        return material

    def new(self, *, name):
        actual_name = name
        suffix = 1
        while actual_name in self.items:
            actual_name = f"{name}.{suffix:03d}"
            suffix += 1
        return self.add(_Material(actual_name))

    def get(self, name):
        return self.items.get(name)

    def remove(self, material):
        self.removed.append(material.name)
        self.items.pop(material.name, None)


class _Object(dict):
    def __init__(self, name: str, data, *, allow_to_curve: bool = True):
        super().__init__()
        self.name = name
        self.data = data
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = [1.0, 1.0, 1.0]
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.matrix_world = types.SimpleNamespace(copy=lambda: "matrix")
        self._base_dimensions = getattr(data, "base_dimensions", None) or (0.020, 0.005, 0.0)
        self._allow_to_curve = allow_to_curve

    @property
    def type(self):
        return self.data.type

    @property
    def dimensions(self):
        return tuple(
            self._base_dimensions[index] * float(self.scale[index])
            for index in range(3)
        )

    def evaluated_get(self, _depsgraph):
        return self

    def to_curve(self, _depsgraph, apply_modifiers=False):
        assert apply_modifiers is False
        if not self._allow_to_curve:
            raise RuntimeError("curve conversion crashed")
        curve = _CurveData(f"{self.name}_outline", self.dimensions)
        curve.materials.extend(getattr(self.data, "materials", []))
        return curve

    def to_curve_clear(self):
        return None


class _AffineMatrix(tuple):
    def __new__(cls, rows):
        return super().__new__(
            cls,
            tuple(tuple(float(value) for value in row) for row in rows),
        )

    @classmethod
    def Identity(cls, size):
        return cls(
            tuple(
                tuple(1.0 if row == column else 0.0 for column in range(size))
                for row in range(size)
            )
        )

    def __matmul__(self, vector):
        values = tuple(float(value) for value in vector)
        homogeneous = (*values[:3], 1.0)
        return tuple(
            sum(self[row][column] * homogeneous[column] for column in range(4))
            for row in range(3)
        )


def _install_mathutils(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "mathutils",
        types.SimpleNamespace(
            Matrix=_AffineMatrix,
            Vector=lambda values: tuple(float(value) for value in values),
        ),
    )


class _CollectionObjects:
    def __init__(self):
        self.items = []

    def link(self, obj):
        self.items.append(obj)

    def unlink(self, obj):
        if obj in self.items:
            self.items.remove(obj)


class _Collection:
    def __init__(self):
        self.objects = _CollectionObjects()


class _AppendThenFailOnSecondLink(_CollectionObjects):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def link(self, obj):
        self.calls += 1
        self.items.append(obj)
        if self.calls == 2:
            raise RuntimeError("final entity link crashed after mutation")


class _Curves:
    def __init__(self, *, baseline_available=True):
        self.removed = []
        self.baseline_available = baseline_available

    def new(self, name: str, type: str):
        assert type == "FONT"
        return _FontData(name, baseline_available=self.baseline_available)

    def remove(self, data):
        self.removed.append(data.name)


class _Objects:
    def __init__(self, *, allow_to_curve=True):
        self.removed = []
        self.allow_to_curve = allow_to_curve

    def new(self, name: str, data):
        return _Object(name, data, allow_to_curve=self.allow_to_curve)

    def remove(self, obj, do_unlink=True):
        assert do_unlink is True
        self.removed.append(obj.name)


class _Meshes:
    def __init__(self, *, fail=False, available=True):
        self.fail = fail
        self.available = available
        self.removed = []
        if not available:
            self.new_from_object = None

    def new_from_object(self, evaluated, depsgraph=None):
        del depsgraph
        if not self.available:
            raise AttributeError("new_from_object unavailable")
        if self.fail:
            raise RuntimeError("mesh conversion crashed")
        mesh = _MeshData(f"{evaluated.name}_mesh", evaluated.dimensions)
        mesh.materials.extend(getattr(evaluated.data, "materials", []))
        return mesh

    def remove(self, data):
        self.removed.append(data.name)


class _Fonts:
    def __init__(self):
        self.loaded = []
        self.removed = []

    def get(self, _name):
        return None

    def load(self, path, check_existing=True):
        self.loaded.append((path, check_existing))
        font = types.SimpleNamespace(
            name="ExactEmbeddedFont",
            filepath=path,
            packed_file=None,
        )

        def pack():
            font.packed_file = types.SimpleNamespace(data=Path(path).read_bytes())

        font.pack = pack
        return font

    def remove(self, font):
        self.removed.append(font.name)


class _Image:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self.packed_file = types.SimpleNamespace(data=bytes(data))
        self.users = 0


class _Images:
    def __init__(self):
        self.items = {}
        self.removed = []

    def add_packed(self, name: str, data: bytes):
        image = _Image(name, data)
        self.items[name] = image
        return image

    def get(self, name):
        return self.items.get(name)

    def remove(self, image):
        self.removed.append(image.name)
        self.items.pop(image.name, None)


class _FakeBpy:
    def __init__(
        self,
        *,
        mesh_fail=False,
        mesh_available=True,
        curve_available=True,
        baseline_available=True,
    ):
        self.view_update_count = 0

        def update_view_layer():
            self.view_update_count += 1

        self.app = types.SimpleNamespace(version=(5, 2, 0))
        self.data = types.SimpleNamespace(
            curves=_Curves(baseline_available=baseline_available),
            objects=_Objects(allow_to_curve=curve_available),
            meshes=_Meshes(fail=mesh_fail, available=mesh_available),
            fonts=_Fonts(),
            images=_Images(),
            materials=_Materials(),
        )
        self.context = types.SimpleNamespace(
            evaluated_depsgraph_get=lambda: object(),
            view_layer=types.SimpleNamespace(update=update_view_layer),
        )


def _font_asset():
    font_bytes = b"exact-pdf-font"
    return types.SimpleNamespace(
        asset_id="sha256:abcdef",
        usable_sha256=sha256(font_bytes).hexdigest(),
        usable_format="cff",
        usable_bytes=font_bytes,
        source_sha256="123456",
        base_font_name="ExactPDF",
        source_xref=7,
        page_number=2,
        span_font_name="ExactPDF",
        units_per_em=1000,
        ascender=800,
        descender=-200,
        glyph_advances=tuple(500 for _index in range(128)),
    )


def _metric_font_asset():
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def glyph(points=()):
        pen = TTGlyphPen(None)
        if points:
            pen.moveTo(points[0])
            for point in points[1:]:
                pen.lineTo(point)
            pen.closePath()
        return pen.glyph()

    builder = FontBuilder(1000, isTTF=True)
    glyph_order = (".notdef", "A", "space")
    builder.setupGlyphOrder(glyph_order)
    builder.setupGlyf({
        ".notdef": glyph(((0, 0), (400, 0), (400, 700), (0, 700))),
        "A": glyph(((100, -200), (500, -200), (500, 700), (100, 700))),
        "space": glyph(),
    })
    builder.setupHorizontalMetrics({
        ".notdef": (500, 0),
        "A": (500, 100),
        "space": (300, 0),
    })
    builder.setupHorizontalHeader(ascent=1200, descent=-300)
    builder.setupCharacterMap({65: "A", 32: "space"})
    builder.setupNameTable({
        "familyName": "MetricFixture",
        "styleName": "Regular",
        "uniqueFontIdentifier": "MetricFixture Regular",
        "fullName": "MetricFixture Regular",
        "psName": "MetricFixture-Regular",
    })
    builder.setupOS2(
        sTypoAscender=1200,
        sTypoDescender=-300,
        usWinAscent=1200,
        usWinDescent=300,
    )
    builder.setupPost()
    builder.setupMaxp()
    stream = BytesIO()
    builder.save(stream)
    font_bytes = stream.getvalue()
    return types.SimpleNamespace(
        asset_id=f"sha256:{sha256(font_bytes).hexdigest()}",
        usable_sha256=sha256(font_bytes).hexdigest(),
        usable_format="ttf",
        usable_bytes=font_bytes,
        source_sha256="metric-source",
        base_font_name="MetricFixture",
        source_xref=9,
        page_number=2,
        span_font_name="MetricFixture",
        units_per_em=1000,
        ascender=1200,
        descender=-300,
        glyph_advances=(500, 500, 300),
    )


def _item(span_id: int = 41, *, font_asset=True) -> NormalizedText:
    return NormalizedText(
        id=span_id,
        text="  WELD 3/16  ",
        normalized="WELD 3/16",
        insertion=(12.0, 24.0),
        bbox=(12.0, 24.0, 52.0, 30.0),
        source_bbox_pdf=(34.0, 50.0, 147.0, 68.0),
        source_quad_pdf=((34.0, 50.0), (147.0, 50.0), (147.0, 68.0), (34.0, 68.0)),
        advance_width=40.0,
        glyph_height=6.0,
        font_size=6.0,
        rotation=30.0,
        font_name="ExactPDF",
        page_number=2,
        font_asset=_font_asset() if font_asset else None,
        font_failure=None if font_asset else types.SimpleNamespace(
            reason="no_exact_embedded_font_match",
            source_xref=None,
            page_number=2,
            span_font_name="ExactPDF",
            error_type="",
            proof_category="source_font_absent_for_item",
            detail="",
        ),
    )


def _character_layout():
    return (
        TextCharLayout(
            text="A",
            glyph_id=37,
            source_origin_pdf=(10.0, 20.0),
            source_bbox_pdf=(10.0, 10.0, 16.0, 22.0),
            source_quad_pdf=((10.0, 10.0), (16.0, 10.0), (16.0, 22.0), (10.0, 22.0)),
            target_origin=(12.0, 24.0),
            target_quad=((12.0, 30.0), (18.0, 30.0), (18.0, 24.0), (12.0, 24.0)),
            advance_width=6.0,
            glyph_height=6.0,
        ),
        TextCharLayout(
            text="B",
            glyph_id=91,
            source_origin_pdf=(18.0, 20.0),
            source_bbox_pdf=(18.0, 10.0, 24.0, 22.0),
            source_quad_pdf=((18.0, 10.0), (24.0, 10.0), (24.0, 22.0), (18.0, 22.0)),
            target_origin=(20.0, 24.0),
            target_quad=((20.0, 30.0), (26.0, 30.0), (26.0, 24.0), (20.0, 24.0)),
            advance_width=6.0,
            glyph_height=6.0,
        ),
    )


def _install(monkeypatch, **kwargs):
    fake = _FakeBpy(**kwargs)
    monkeypatch.setattr(bl_text_builder, "bpy", fake)
    bl_text_builder._FONT_CACHE.clear()
    return fake, _Collection()


def test_fallback_ladders_are_finite_distinct_and_never_alias_glyphs_geometry():
    assert fallback_ladder("labels") == (
        "labels", "text", "3d_text", "glyphs", "geometry", "raster"
    )
    assert fallback_ladder("text") == (
        "text", "3d_text", "glyphs", "geometry", "raster"
    )
    assert fallback_ladder("3d_text") == (
        "3d_text", "text", "glyphs", "geometry", "raster"
    )
    assert fallback_ladder("glyphs") == ("glyphs", "geometry", "raster")
    assert fallback_ladder("geometry") == ("geometry", "glyphs", "raster")
    assert fallback_ladder("raster") == ("raster",)
    with pytest.raises(ValueError, match="Unknown requested representation"):
        fallback_ladder("typo")


def test_strict_text_fidelity_cannot_be_disabled_at_the_builder_boundary(monkeypatch):
    _fake, collection = _install(monkeypatch)

    with pytest.raises(ValueError, match="strict_text_fidelity cannot be disabled"):
        bl_text_builder.build_text(
            _item(),
            collection,
            page_number=2,
            text_mode="text",
            strict_text_fidelity=False,
        )


@pytest.mark.parametrize(
    ("mode", "expected_type", "expected_extruded"),
    [
        ("text", "FONT", False),
        ("3d_text", "FONT", True),
        ("glyphs", "CURVE", False),
        ("geometry", "MESH", False),
    ],
)
def test_requested_mode_creates_distinct_verified_host_entity(
    monkeypatch,
    mode,
    expected_type,
    expected_extruded,
):
    fake, collection = _install(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is not None
    assert obj.type == expected_type
    assert obj["pdf_text_mode"] == mode
    assert obj["pdf_source_item_id"] == "page:2:text:41"
    assert collection.objects.items == [obj]
    assert obj.location == pytest.approx((0.012, 0.024, 0.0))
    assert obj.rotation_euler[2] == pytest.approx(math.radians(30.0))
    expected_scale = (1.0, 1.0) if mode in {"glyphs", "geometry"} else (2.0, 1.2)
    assert obj.scale[0] == pytest.approx(expected_scale[0])
    assert obj.scale[1] == pytest.approx(expected_scale[1])
    assert obj.dimensions[0] == pytest.approx(0.040)
    assert obj.dimensions[1] == pytest.approx(0.006)
    if mode in {"text", "3d_text"}:
        assert obj.data.body == "  WELD 3/16  "
        assert obj.data.font.name == "ExactEmbeddedFont"
        assert (obj.data.extrude > 0.0) is expected_extruded
    delivery = opts._text_delivery_records[-1]
    assert delivery["requested_representation"] == mode
    assert delivery["final_representation"] == mode
    assert delivery["status"] == "delivered"
    assert delivery["fallback_used"] is False
    assert delivery["attempts"][-1]["entity_ids"] == [obj.name]
    assert fake.data.fonts.loaded


def test_character_positioned_3d_text_stays_3d_text_and_records_every_entity(monkeypatch):
    fake, collection = _install(monkeypatch)
    verification_update_counts = []
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )

    def verify_positioned_transform(_obj, text_item):
        verification_update_counts.append(fake.view_update_count)
        return (
            [],
            {
                "expected_location_m": [
                    text_item.insertion[0] * 0.001,
                    text_item.insertion[1] * 0.001,
                ],
                "actual_baseline_anchor_m": [
                    text_item.insertion[0] * 0.001,
                    text_item.insertion[1] * 0.001,
                ],
                "evaluated_bounds_verified": True,
            },
        )

    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        verify_positioned_transform,
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="3d_text")
    item = _item()
    item.text = "AB"
    item.normalized = "AB"
    item.source_char_layout = _character_layout()
    item.requires_individual_positioning = True

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="3d_text",
        provenance_opts=opts,
    )

    assert obj is not None
    assert len(collection.objects.items) == 2
    assert {candidate.type for candidate in collection.objects.items} == {"FONT"}
    assert {candidate.data.body for candidate in collection.objects.items} == {"A", "B"}
    record = opts._text_delivery_records[-1]
    assert record["final_representation"] == "3d_text"
    assert record["fallback_used"] is False
    assert len(record["entity_ids"]) == 2
    assert len(set(record["entity_ids"])) == 2
    evidence = record["attempts"][-1]["evidence"]
    assert [entry["glyph_id"] for entry in evidence["character_entities"]] == [37, 91]
    assert all(entry["positioned_character"] is True for entry in evidence["character_entities"])
    assert evidence["source_xref"] == 7
    assert evidence["source_sha256"] == "123456"
    assert evidence["font_asset_page_number"] == 2
    assert evidence["expected_text_rgba"] == evidence["actual_text_rgba"]
    assert evidence["expected_location_m"] == evidence["actual_baseline_anchor_m"]
    assert fake.view_update_count == 1
    assert verification_update_counts == [1, 1]


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_positioned_conversion_batches_source_and_final_dependency_graph_updates(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    verification_updates = []
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )

    def verify_positioned_transform(obj, text_item):
        verification_updates.append((obj.type, fake.view_update_count))
        location = [
            text_item.insertion[0] * 0.001,
            text_item.insertion[1] * 0.001,
        ]
        return (
            [],
            {
                "expected_location_m": location,
                "actual_baseline_anchor_m": location,
                "evaluated_bounds_verified": True,
            },
        )

    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        verify_positioned_transform,
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    item = _item()
    item.text = "AB"
    item.normalized = "AB"
    item.source_char_layout = _character_layout()
    item.requires_individual_positioning = True

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is not None and obj.type == expected_type
    assert len(collection.objects.items) == 2
    assert {candidate.type for candidate in collection.objects.items} == {
        expected_type
    }
    record = opts._text_delivery_records[-1]
    assert record["final_representation"] == mode
    assert record["fallback_used"] is False
    assert len(record["entity_ids"]) == 2
    assert fake.view_update_count == 2
    assert verification_updates == [
        ("FONT", 1),
        ("FONT", 1),
        (expected_type, 2),
        (expected_type, 2),
    ]


@pytest.mark.parametrize(
    ("mode", "install_kwargs"),
    [
        ("glyphs", {"curve_available": False}),
        ("geometry", {"mesh_fail": True}),
    ],
)
def test_positioned_conversion_failure_is_atomic_and_cannot_cross_type_fallback(
    monkeypatch,
    mode,
    install_kwargs,
):
    fake, collection = _install(monkeypatch, **install_kwargs)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {}),
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    item = _item()
    item.text = "AB"
    item.normalized = "AB"
    item.source_char_layout = _character_layout()
    item.requires_individual_positioning = True

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is None
    assert collection.objects.items == []
    record = opts._text_delivery_records[-1]
    assert record["status"] == "failed"
    assert [attempt["attempted_representation"] for attempt in record["attempts"]] == [
        mode
    ]
    assert record["attempts"][0]["cleanup"]["status"] == "complete"
    assert fake.view_update_count == 1
    assert len(fake.data.objects.removed) >= 2


def test_positioned_whitespace_without_glyph_id_uses_source_layout_identity_metrics():
    layout = TextCharLayout(
        text=" ",
        glyph_id=None,
        source_origin_pdf=(10.0, 20.0),
        source_bbox_pdf=(10.0, 10.0, 16.0, 22.0),
        source_quad_pdf=((10.0, 10.0), (16.0, 10.0), (16.0, 22.0), (10.0, 22.0)),
        target_origin=(12.0, 24.0),
        target_quad=((12.0, 30.0), (18.0, 30.0), (18.0, 24.0), (12.0, 24.0)),
        advance_width=6.0,
        glyph_height=6.0,
    )
    child = bl_text_builder._character_text_item(_item(), layout)
    data = _FontData("WhitespaceIdentity")
    data.size = 0.006
    obj = _Object("WhitespaceIdentity", data)

    metrics = bl_text_builder._positioned_font_axis_metrics(obj, child)

    assert metrics["glyph_id"] == -1
    assert metrics["metric_source"] == "source_layout_zero_ink"
    assert metrics["zero_ink_identity"] is True
    assert metrics["local_advance"] == pytest.approx(0.006)
    assert metrics["local_line_height"] == pytest.approx(0.006)


def test_positioned_font_axis_metrics_use_upem_for_horizontal_glyph_domain():
    layout = _character_layout()[0]
    layout.glyph_id = 1
    parent = _item()
    parent.font_asset = _metric_font_asset()
    child = bl_text_builder._character_text_item(parent, layout)
    data = _FontData("MetricDomain")
    data.size = 0.006
    obj = _Object("MetricDomain", data)
    obj["pdf_baseline_alignment"] = "BOTTOM"

    metrics = bl_text_builder._positioned_font_axis_metrics(obj, child)

    assert metrics["design_unit_scale"] == pytest.approx(0.006 / 1000.0)
    assert metrics["local_advance"] == pytest.approx(0.003)
    assert metrics["local_line_height"] == pytest.approx(0.009)
    assert metrics["local_baseline_y"] == pytest.approx(0.0018)
    assert metrics["source_ink_bounds_design_units"] == pytest.approx(
        (100.0, -200.0, 500.0, 700.0)
    )


def test_exact_glyph_design_bounds_rejects_cached_upem_mismatch(monkeypatch):
    monkeypatch.setattr(bl_text_builder, "_EXACT_GLYPH_DESIGN_BOUNDS_CACHE", {})
    asset = _metric_font_asset()

    assert bl_text_builder._exact_glyph_design_bounds(asset, 1) == pytest.approx(
        (100.0, -200.0, 500.0, 700.0)
    )
    mismatched_asset = types.SimpleNamespace(**vars(asset))
    mismatched_asset.units_per_em = 2048

    with pytest.raises(
        RuntimeError,
        match="embedded font metric metadata does not match glyph design units",
    ):
        bl_text_builder._exact_glyph_design_bounds(mismatched_asset, 1)


def test_positioned_metric_affine_stores_exact_source_glyph_world_ink_bounds(monkeypatch):
    _install_mathutils(monkeypatch)
    layout = _character_layout()[0]
    layout.glyph_id = 1
    parent = _item()
    parent.font_asset = _metric_font_asset()
    child = bl_text_builder._character_text_item(parent, layout)
    data = _FontData("SourceBounds")
    data.size = 0.006
    obj = _Object("SourceBounds", data)
    obj["pdf_baseline_alignment"] = "BOTTOM_BASELINE"

    bl_text_builder._apply_target_quad_affine(obj, child, 0.0)

    assert obj.get("pdf_metric_expected_world_ink_bounds_m") == pytest.approx(
        (0.0132, 0.0232, 0.018, 0.0268)
    )


def test_positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds(
    monkeypatch,
):
    _install(monkeypatch)
    _install_mathutils(monkeypatch)
    identity = _AffineMatrix.Identity(4)
    child = bl_text_builder._character_text_item(_item(), _character_layout()[0])
    child.insertion = (0.0, 0.0)
    child.target_quad_model = (
        (0.0, 1000.0),
        (1000.0, 1000.0),
        (1000.0, 0.0),
        (0.0, 0.0),
    )
    data = _FontData("OverwideInk")
    obj = _Object("OverwideInk", data)
    obj.matrix_world = _AffineMatrix((
        (1.0, 0.0, 0.0, 4.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    obj._base_dimensions = (0.5, 1.0, 0.0)
    obj.bound_box = (
        (0.0, 0.0, 0.0),
        (0.5, 0.0, 0.0),
        (0.5, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.5, 0.0, 0.0),
        (0.5, 1.0, 0.0),
        (0.0, 1.0, 0.0),
    )
    evaluated = _Object("OverwideInkEvaluated", data)
    evaluated.matrix_world = identity
    evaluated._base_dimensions = (0.75, 1.0, 0.0)
    evaluated.bound_box = (
        (0.0, 0.0, 0.0),
        (0.75, 0.0, 0.0),
        (0.75, 1.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.75, 0.0, 0.0),
        (0.75, 1.0, 0.0),
        (0.0, 1.0, 0.0),
    )
    obj.evaluated_get = lambda _depsgraph: evaluated
    obj.update({
        "pdf_metric_local_advance": 1.0,
        "pdf_metric_local_line_height": 1.0,
        "pdf_metric_local_baseline_y": 0.0,
        "pdf_metric_zero_ink_identity": False,
        "pdf_metric_expected_world_ink_bounds_m": [0.0, 0.0, 0.5, 1.0],
        "pdf_affine_matrix": [value for row in identity for value in row],
    })

    failures, evidence = bl_text_builder._verify_metric_character_transform(obj, child)

    assert failures == ["evaluated_ink_bounds_outside_exact_source_glyph_bounds"]
    assert evidence["expected_world_ink_bounds_m"] == pytest.approx(
        (0.0, 0.0, 0.5, 1.0)
    )
    assert evidence["actual_world_ink_bounds_m"] == pytest.approx(
        (0.0, 0.0, 0.75, 1.0)
    )
    assert evidence["evaluated_affine_matrix"] == pytest.approx(
        [value for row in identity for value in row]
    )


def test_positioned_metric_verification_preserves_explicit_zero_ink_identity(monkeypatch):
    _install(monkeypatch)
    _install_mathutils(monkeypatch)
    identity = _AffineMatrix.Identity(4)
    layout = TextCharLayout(
        text=" ",
        glyph_id=None,
        source_origin_pdf=(0.0, 0.0),
        source_bbox_pdf=(0.0, 0.0, 1000.0, 1000.0),
        source_quad_pdf=((0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)),
        target_origin=(0.0, 0.0),
        target_quad=((0.0, 1000.0), (1000.0, 1000.0), (1000.0, 0.0), (0.0, 0.0)),
        advance_width=1000.0,
        glyph_height=1000.0,
    )
    child = bl_text_builder._character_text_item(_item(), layout)
    data = _FontData("ZeroInk")
    obj = _Object("ZeroInk", data)
    obj.matrix_world = identity
    obj.update({
        "pdf_metric_local_advance": 1.0,
        "pdf_metric_local_line_height": 1.0,
        "pdf_metric_local_baseline_y": 0.0,
        "pdf_metric_zero_ink_identity": True,
        "pdf_affine_matrix": [value for row in identity for value in row],
    })

    failures, evidence = bl_text_builder._verify_metric_character_transform(obj, child)

    assert failures == []
    assert evidence["zero_ink_identity"] is True
    assert evidence["expected_world_ink_bounds_m"] is None
    assert evidence["actual_world_ink_bounds_m"] is None


def test_zero_ink_metric_verification_still_rejects_corrupt_evaluated_transform(
    monkeypatch,
):
    _install(monkeypatch)
    _install_mathutils(monkeypatch)
    identity = _AffineMatrix.Identity(4)
    layout = TextCharLayout(
        text=" ",
        glyph_id=None,
        source_origin_pdf=(0.0, 0.0),
        source_bbox_pdf=(0.0, 0.0, 1000.0, 1000.0),
        source_quad_pdf=((0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)),
        target_origin=(0.0, 0.0),
        target_quad=((0.0, 1000.0), (1000.0, 1000.0), (1000.0, 0.0), (0.0, 0.0)),
        advance_width=1000.0,
        glyph_height=1000.0,
    )
    child = bl_text_builder._character_text_item(_item(), layout)
    data = _FontData("CorruptZeroInk")
    obj = _Object("CorruptZeroInk", data)
    obj.matrix_world = identity
    obj.update({
        "pdf_metric_local_advance": 1.0,
        "pdf_metric_local_line_height": 1.0,
        "pdf_metric_local_baseline_y": 0.0,
        "pdf_metric_zero_ink_identity": True,
        "pdf_affine_matrix": [value for row in identity for value in row],
    })
    evaluated = _Object("CorruptZeroInkEvaluated", data)
    evaluated.matrix_world = _AffineMatrix((
        (1.0, 0.0, 0.0, 0.01),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    obj.evaluated_get = lambda _depsgraph: evaluated

    failures, evidence = bl_text_builder._verify_metric_character_transform(obj, child)

    assert evidence["zero_ink_identity"] is True
    assert evidence["expected_world_ink_bounds_m"] is None
    assert evidence["actual_world_ink_bounds_m"] is None
    assert {
        "evaluated_baseline_anchor_mismatch",
        "evaluated_font_advance_axis_mismatch",
        "evaluated_font_line_axis_mismatch",
        "evaluated_affine_matrix_mismatch",
    }.issubset(failures)


def test_affine_coefficients_preserve_shear_and_mirror():
    affine = bl_text_builder._quad_affine_coefficients(
        ((4.0, 10.0), (-2.0, 8.0), (-1.0, 2.0), (5.0, 4.0)),
        unit_scale=1.0,
    )

    assert bl_text_builder._apply_affine_2d(affine, 0.0, 1.0) == pytest.approx((4.0, 10.0))
    assert bl_text_builder._apply_affine_2d(affine, 1.0, 1.0) == pytest.approx((-2.0, 8.0))
    assert bl_text_builder._apply_affine_2d(affine, 1.0, 0.0) == pytest.approx((-1.0, 2.0))
    assert bl_text_builder._apply_affine_2d(affine, 0.0, 0.0) == pytest.approx((5.0, 4.0))
    assert affine[0] < 0.0  # mirrored x axis is retained, never normalized away


def test_local_bounds_matrix_maps_all_four_corners_without_losing_affine_sign():
    matrix = bl_text_builder._affine_matrix_values(
        local_bounds=(-2.0, -1.0, 8.0, 4.0),
        target_quad=((4.0, 10.0), (-2.0, 8.0), (-1.0, 2.0), (5.0, 4.0)),
        z=0.25,
        unit_scale=1.0,
    )

    def mapped(x, y):
        return (
            matrix[0][0] * x + matrix[0][1] * y + matrix[0][3],
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][3],
        )

    assert mapped(-2.0, 4.0) == pytest.approx((4.0, 10.0))
    assert mapped(8.0, 4.0) == pytest.approx((-2.0, 8.0))
    assert mapped(8.0, -1.0) == pytest.approx((-1.0, 2.0))
    assert mapped(-2.0, -1.0) == pytest.approx((5.0, 4.0))
    assert matrix[2][3] == pytest.approx(0.25)
    assert matrix[0][0] < 0.0


def test_metric_character_matrix_pins_baseline_and_maps_font_axes() -> None:
    matrix = bl_text_builder._metric_character_matrix_values(
        local_advance=2.0,
        local_line_height=5.0,
        target_origin=(11.0, 21.0),
        target_quad=((8.0, 30.0), (14.0, 28.0), (16.0, 18.0), (10.0, 20.0)),
        z=0.25,
        unit_scale=1.0,
    )

    def mapped(x, y):
        return (
            matrix[0][0] * x + matrix[0][1] * y + matrix[0][3],
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][3],
        )

    assert mapped(0.0, 0.0) == pytest.approx((11.0, 21.0))
    assert mapped(2.0, 0.0) == pytest.approx((17.0, 19.0))
    assert mapped(0.0, 5.0) == pytest.approx((9.0, 31.0))
    assert matrix[2][3] == pytest.approx(0.25)


def test_two_object_affine_factor_retains_shear_without_shear_in_either_factor() -> None:
    original = (
        (2.3386829253292842, 0.0, 0.0, 0.0672869868967923),
        (-0.5593792270339143, 4.550226975837434, 0.0, 0.21237508097831248),
        (0.0, 0.0, 1.0, 0.4),
        (0.0, 0.0, 0.0, 1.0),
    )
    parent, child = bl_text_builder._factor_affine_matrix_values(original)

    def multiply(left, right):
        return tuple(
            tuple(sum(left[row][k] * right[k][col] for k in range(4)) for col in range(4))
            for row in range(4)
        )

    product = multiply(parent, child)
    assert [value for row in product for value in row] == pytest.approx(
        [value for row in original for value in row]
    )
    for factor in (parent, child):
        first = (factor[0][0], factor[1][0])
        second = (factor[0][1], factor[1][1])
        assert first[0] * second[0] + first[1] * second[1] == pytest.approx(0.0, abs=1e-12)


def test_bottom_alignment_is_measured_and_compensated_to_the_source_baseline(monkeypatch):
    _fake, collection = _install(monkeypatch, baseline_available=False)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")
    item = _item()
    item.baseline_descent = 1.5

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is not None
    assert obj.data.align_y == "BOTTOM"
    evidence = opts._text_delivery_records[-1]["attempts"][-1]["evidence"]
    assert evidence["baseline_alignment"] == "BOTTOM"
    assert evidence["baseline_compensation_m"] == pytest.approx(0.0015)
    assert evidence["actual_baseline_anchor_m"] == pytest.approx(
        evidence["expected_location_m"]
    )
    assert evidence["evaluated_bounds_verified"] is True


def test_text_attempt_never_reuses_or_mutates_same_named_user_material(monkeypatch):
    fake, collection = _install(monkeypatch)
    sentinel = _Material("PDF_Text_source")
    sentinel.diffuse_color = (1.0, 0.0, 1.0, 1.0)
    sentinel.use_nodes = True
    sentinel_nodes = list(sentinel.node_tree.nodes)
    fake.data.materials.add(sentinel)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is not None
    assigned = obj.data.materials[0]
    assert assigned is not sentinel
    assert assigned.name != sentinel.name
    assert tuple(sentinel.diffuse_color) == (1.0, 0.0, 1.0, 1.0)
    assert sentinel.use_nodes is True
    assert list(sentinel.node_tree.nodes) == sentinel_nodes
    assert obj["pdf_text_material_owned"] is True


def test_text_material_color_is_part_of_visual_verification(monkeypatch):
    fake, collection = _install(monkeypatch)
    original = bl_text_builder._get_or_create_text_material

    def _wrong_color(*args, **kwargs):
        material = original(*args, **kwargs)
        material.diffuse_color = (1.0, 0.0, 1.0, 1.0)
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                node.inputs["Base Color"].default_value = (1.0, 0.0, 1.0, 1.0)
        return material

    monkeypatch.setattr(bl_text_builder, "_get_or_create_text_material", _wrong_color)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert "text_material_color_mismatch" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert fake.data.materials.removed


def test_text_material_constructor_rolls_back_on_node_failure(monkeypatch):
    fake, _collection = _install(monkeypatch)

    class _BrokenNodes:
        def clear(self):
            pass

        def new(self, **_kwargs):
            raise RuntimeError("node factory failed")

    material = types.SimpleNamespace(
        name="attempt-material",
        diffuse_color=(0.0, 0.0, 0.0, 1.0),
        use_nodes=False,
        node_tree=types.SimpleNamespace(nodes=_BrokenNodes(), links=object()),
    )
    removed = []
    fake.data.materials = types.SimpleNamespace(
        new=lambda **_kwargs: material,
        remove=lambda value: removed.append(value.name),
    )

    with pytest.raises(RuntimeError, match="node factory failed"):
        bl_text_builder._get_or_create_text_material("source")

    assert removed == [material.name]


def test_failed_material_rollback_remains_owned_and_cannot_report_cleanup_complete(
    monkeypatch,
):
    fake, collection = _install(monkeypatch)

    class _BrokenNodes:
        def clear(self):
            pass

        def new(self, **_kwargs):
            raise RuntimeError("node factory failed")

    material = _Material("attempt-material")
    material.node_tree = types.SimpleNamespace(nodes=_BrokenNodes(), links=object())

    class _FailingMaterials:
        @staticmethod
        def new(**_kwargs):
            return material

        @staticmethod
        def remove(_value):
            raise RuntimeError("material removal blocked")

        @staticmethod
        def get(name):
            return material if name == material.name else None

    fake.data.materials = _FailingMaterials()
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["cleanup"]["status"] == "failed"
    assert any(
        artifact.get("datablock_id") == material.name
        for artifact in attempt["owned_artifacts"]
    )


def test_failed_exact_font_pack_removes_newly_loaded_host_datablock(monkeypatch):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "pack_and_verify_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pack failed")),
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    assert fake.data.fonts.removed == ["ExactEmbeddedFont"]


def test_failed_font_rollback_remains_owned_and_blocks_false_cleanup_success(monkeypatch):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "pack_and_verify_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pack failed")),
    )
    fake.data.fonts.remove = lambda _font: (_ for _ in ()).throw(
        RuntimeError("font removal blocked")
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["cleanup"]["status"] == "failed"
    assert any(
        artifact.get("datablock_id") == "ExactEmbeddedFont"
        for artifact in attempt["owned_artifacts"]
    )


def test_nonfinite_native_text_transform_cannot_be_verified(monkeypatch):
    _fake, collection = _install(monkeypatch)
    item = _item()
    item.insertion = (float("nan"), 24.0)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    failures = opts._text_delivery_records[-1]["attempts"][0]["evidence"]["failures"]
    assert "nonfinite_text_transform_or_dimensions" in failures


def test_labels_use_item_specific_host_capability_proof_before_text_fallback(
    monkeypatch,
):
    _fake, collection = _install(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="labels")

    obj = bl_text_builder.build_text(
        _item(), collection, page_number=2, text_mode="labels", provenance_opts=opts
    )

    assert obj is not None and obj.type == "FONT"
    assert obj["pdf_text_mode"] == "text"
    record = opts._text_delivery_records[-1]
    assert [attempt["attempted_representation"] for attempt in record["attempts"]] == [
        "labels",
        "text",
    ]
    label_attempt = record["attempts"][0]
    assert label_attempt["status"] == "impossible"
    assert label_attempt["evidence"]["item_id"] == "page:2:text:41"
    assert label_attempt["evidence"]["capability"] == "persistent_renderable_model_label"
    assert record["fallback_used"] is True
    assert record["fallback_attempted"] is True


def test_generic_mesh_exception_stops_without_cross_type_fallback_and_cleans_owned_object(
    monkeypatch,
):
    fake, collection = _install(monkeypatch, mesh_fail=True)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="geometry")

    obj = bl_text_builder.build_text(
        _item(), collection, page_number=2, text_mode="geometry", provenance_opts=opts
    )

    assert obj is None
    record = opts._text_delivery_records[-1]
    assert record["status"] == "failed"
    assert record["final_representation"] is None
    assert [a["attempted_representation"] for a in record["attempts"]] == ["geometry"]
    assert record["attempts"][0]["status"] == "failed"
    assert record["attempts"][0]["cleanup"]["status"] == "complete"
    assert collection.objects.items == []
    assert fake.data.objects.removed


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_failed_conversion_cleans_original_and_partially_linked_final_entity(
    monkeypatch,
    mode,
):
    _fake, _collection = _install(monkeypatch)
    collection = _Collection()
    collection.objects = _AppendThenFailOnSecondLink()
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is None
    assert collection.objects.items == []
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["cleanup"]["status"] == "complete"
    assert len(attempt["owned_artifacts"]) == 2


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_converted_representation_must_verify_transform_and_dimensions(
    monkeypatch,
    mode,
):
    fake, collection = _install(monkeypatch)
    original_copy = bl_text_builder._copy_object_transform

    def _copy_then_corrupt(source, target):
        original_copy(source, target)
        target.location = (99.0, 88.0, 0.0)

    monkeypatch.setattr(bl_text_builder, "_copy_object_transform", _copy_then_corrupt)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    obj = bl_text_builder.build_text(
        _item(), collection, page_number=2, text_mode=mode, provenance_opts=opts
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["reason"] == "converted_representation_visual_verification_failed"
    assert "x_anchor_mismatch" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert collection.objects.items == []
    assert fake.data.objects.removed


def test_explicit_missing_mesh_capability_permits_closest_glyph_fallback(monkeypatch):
    fake, collection = _install(monkeypatch, mesh_available=False)
    # Remove the method rather than throwing from an implementation that exists.
    opts = types.SimpleNamespace(import_mode="vector", text_mode="geometry")

    obj = bl_text_builder.build_text(
        _item(), collection, page_number=2, text_mode="geometry", provenance_opts=opts
    )

    assert obj is not None and obj.type == "CURVE"
    assert obj["pdf_text_mode"] == "glyphs"
    record = opts._text_delivery_records[-1]
    assert [a["status"] for a in record["attempts"]] == ["impossible", "delivered"]
    assert record["fallback_used"] is True
    assert record["fallback_attempted"] is True


def test_all_structural_rungs_and_terminal_raster_failure_are_loud(monkeypatch):
    fake, collection = _install(monkeypatch, mesh_available=False, curve_available=False)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        _item(font_asset=False),
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
        terminal_raster_callback=lambda *_args, **_kwargs: None,
    )

    assert obj is None
    record = opts._text_delivery_records[-1]
    assert record["status"] == "failed"
    assert [a["attempted_representation"] for a in record["attempts"]] == [
        "text", "3d_text", "glyphs", "geometry", "raster"
    ]
    assert all(a["status"] == "impossible" for a in record["attempts"][:-1])
    assert record["attempts"][-1]["status"] == "failed"
    assert record["attempts"][-1]["reason"] == "terminal_raster_not_verified"
    assert record["fallback_attempted"] is True
    assert record["fallback_used"] is False
    assert collection.objects.items == []


def _raster_callback_for_test(
    path,
    *,
    corrupt_location=False,
    corrupt_packed_bytes=False,
    explode_during_verification=False,
    broken_texture_binding=False,
    nonfinite_location=False,
):
    def _callback(_text_item, collection, _page_number, item_id):
        class _VerificationExplodingObject(_Object):
            @property
            def location(self):
                if getattr(self, "_explode_location", False):
                    raise RuntimeError("unexpected host location read failure")
                return self._location

            @location.setter
            def location(self, value):
                self._location = value

        plane_type = _VerificationExplodingObject if explode_during_verification else _Object
        plane = plane_type("P2_text_41_raster", _MeshData("P2_text_41_raster_mesh"))
        plane.location = (
            (float("nan"), 0.024, 0.0)
            if nonfinite_location
            else (99.0, 88.0, 0.0)
            if corrupt_location
            else (0.012, 0.024, 0.0)
        )
        plane.scale = [2.0, 1.2, 1.0]
        if explode_during_verification:
            plane._explode_location = True
        plane["pdf_raster_source_item_id"] = item_id
        plane["pdf_raster_source_bbox_pdf"] = [34.0, 50.0, 147.0, 68.0]
        plane["pdf_image_path"] = str(path)
        plane["pdf_image_material"] = ""
        image_name = "P2_text_41_raster_image"
        image_bytes = b"wrong-packed-bytes" if corrupt_packed_bytes else Path(path).read_bytes()
        bl_text_builder.bpy.data.images.add_packed(image_name, image_bytes)
        plane["pdf_image_datablock"] = image_name
        material_name = "P2_text_41_raster_material"
        material = _Material(
            material_name,
            bl_text_builder.bpy.data.images.get(image_name),
            valid_links=not broken_texture_binding,
        )
        bl_text_builder.bpy.data.materials.add(material)
        plane.data.materials.append(material)
        plane.data.uv_layers = _UVLayers(valid=not broken_texture_binding)
        plane.data.loops = [object(), object(), object(), object()]
        plane["pdf_image_material"] = material_name
        plane["pdf_image_material_owned"] = True
        plane["pdf_image_datablock_owned"] = True
        plane["pdf_image_packed"] = True
        plane["pdf_image_sha256"] = sha256(Path(path).read_bytes()).hexdigest()
        collection.objects.link(plane)
        return plane

    return _callback


def test_raster_delivery_verifies_real_plane_and_reports_owned_clip(monkeypatch, tmp_path):
    _fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(clip),
    )

    assert obj is not None and obj.type == "MESH"
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["status"] == "delivered"
    assert attempt["evidence"]["placement_verified"] is True
    assert attempt["owned_artifacts"][0]["file_path"] == str(clip)


def test_raster_visual_mismatch_fails_and_cleans_plane_and_clip(monkeypatch, tmp_path):
    fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(clip, corrupt_location=True),
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["reason"] == "terminal_raster_visual_verification_failed"
    assert attempt["cleanup"]["status"] == "complete"
    assert collection.objects.items == []
    assert fake.data.objects.removed
    assert not clip.exists()


def test_raster_delivery_rejects_packed_bytes_that_do_not_match_the_clip(
    monkeypatch,
    tmp_path,
):
    fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            corrupt_packed_bytes=True,
        ),
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert "raster_packed_image_hash_mismatch" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert fake.data.images.removed == ["P2_text_41_raster_image"]
    assert not clip.exists()


def test_unexpected_post_creation_raster_verification_failure_retains_ownership(
    monkeypatch,
    tmp_path,
):
    fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            explode_during_verification=True,
        ),
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["reason"] == "terminal_raster_verification_raised"
    assert attempt["cleanup"]["status"] == "complete"
    assert collection.objects.items == []
    assert fake.data.images.removed == ["P2_text_41_raster_image"]
    assert not clip.exists()


def test_raster_verification_runs_with_python_39_zip_semantics(monkeypatch, tmp_path):
    original_zip = builtins.zip
    monkeypatch.setattr(builtins, "zip", lambda *iterables: original_zip(*iterables))
    _fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(clip),
    )

    assert obj is not None
    assert opts._text_delivery_records[-1]["status"] == "delivered"


def test_raster_delivery_rejects_blank_plane_without_uv_and_node_links(
    monkeypatch,
    tmp_path,
):
    fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            broken_texture_binding=True,
        ),
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert "raster_uv_map_unverified" in attempt["evidence"]["failures"]
    assert "raster_material_node_links_unverified" in attempt["evidence"]["failures"]
    assert attempt["cleanup"]["status"] == "complete"
    assert fake.data.materials.removed == ["P2_text_41_raster_material"]
    assert fake.data.images.removed == ["P2_text_41_raster_image"]


def test_nonfinite_raster_geometry_cannot_be_verified(monkeypatch, tmp_path):
    _fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(b"verified-png")
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            nonfinite_location=True,
        ),
    )

    assert obj is None
    failures = opts._text_delivery_records[-1]["attempts"][0]["evidence"]["failures"]
    assert "raster_nonfinite_geometry" in failures


def test_unknown_mode_fails_before_mutating_scene(monkeypatch):
    _fake, collection = _install(monkeypatch)
    with pytest.raises(ValueError, match="Unknown requested representation"):
        bl_text_builder.build_text(_item(), collection, text_mode="typo")
    assert collection.objects.items == []


@pytest.mark.parametrize(
    ("reason", "evidence"),
    [
        (
            "generic_host_limitation",
            {
                "importer_id": "bc_pdf_vector_importer.blender",
                "item_id": "page:2:text:41",
                "page_number": 2,
                "source_span_id": 41,
            },
        ),
        (
            "exact_source_font_unavailable_for_item",
            {
                "importer_id": "bc_pdf_vector_importer.blender",
                "item_id": "page:99:text:41",
                "page_number": 99,
                "source_span_id": 41,
                "reason": "no_exact_embedded_font_match",
                "font_name": "Exact-Font-Name",
            },
        ),
        (
            "exact_source_font_unavailable_for_item",
            {
                "importer_id": "bc_pdf_vector_importer.blender",
                "item_id": "page:2:text:41",
                "page_number": 2,
                "source_span_id": 41,
                "reason": "page_font_inventory_failed",
                "font_name": "Exact-Font-Name",
                "error_type": "RuntimeError",
                "detail": "transient inventory failure",
            },
        ),
    ],
)
def test_unbound_generic_or_runtime_impossibility_proof_cannot_advance_ladder(
    reason,
    evidence,
):
    attempted = []

    def attempt(representation):
        attempted.append(representation)
        if len(attempted) == 1:
            return AttemptOutcome.impossible(reason, evidence=evidence)
        return AttemptOutcome.delivered(object(), entity_ids=("must-not-exist",))

    entity, record = deliver_item(
        item_id="page:2:text:41",
        page_number=2,
        source_span_id=41,
        requested="text",
        attempt=attempt,
        cleanup=lambda _outcome: {"status": "complete", "removed": []},
    )

    assert entity is None
    assert attempted == ["text"]
    assert record["status"] == "failed"
    assert record["attempts"][0]["status"] == "failed"
    assert record["attempts"][0]["reason"] == (
        "impossibility_evidence_not_affirmative"
    )
    assert record["attempts"][0]["evidence"]["proof_failures"]


def test_wrong_page_font_failure_proof_cannot_advance_the_ladder():
    attempted = []

    def attempt(representation):
        attempted.append(representation)
        if len(attempted) == 1:
            return AttemptOutcome.impossible(
                "exact_source_font_unavailable_for_item",
                evidence={
                    "importer_id": "bc_pdf_vector_importer.blender",
                    "item_id": "page:2:text:41",
                    "page_number": 2,
                    "source_span_id": 41,
                    "reason": "no_exact_embedded_font_match",
                    "font_name": "ExactPDF",
                    "font_failure_page_number": 99,
                    "font_failure_span_font_name": "ExactPDF",
                },
            )
        return AttemptOutcome.delivered(object(), entity_ids=("must-not-exist",))

    entity, record = deliver_item(
        item_id="page:2:text:41",
        page_number=2,
        source_span_id=41,
        requested="text",
        attempt=attempt,
        cleanup=lambda _outcome: {"status": "complete", "removed": []},
    )

    assert entity is None
    assert attempted == ["text"]
    assert "font_failure_page_identity_unbound" in record["attempts"][0]["evidence"][
        "proof_failures"
    ]


@pytest.mark.parametrize(
    ("reason", "proof_category"),
    [
        ("embedded_font_asset_build_failed", "source_specific_impossibility"),
        ("page_text_trace_inventory_failed", "runtime_inventory_unavailable_for_item"),
    ],
)
def test_structured_item_bound_font_proof_can_advance_to_closest_fallback(
    reason,
    proof_category,
):
    attempted = []

    def attempt(representation):
        attempted.append(representation)
        if len(attempted) == 1:
            return AttemptOutcome.impossible(
                "exact_source_font_unavailable_for_item",
                evidence={
                    "importer_id": "bc_pdf_vector_importer.blender",
                    "item_id": "page:2:text:41",
                    "page_number": 2,
                    "source_span_id": 41,
                    "reason": reason,
                    "proof_category": proof_category,
                    "font_name": "ExactPDF",
                    "font_failure_page_number": 2,
                    "font_failure_span_font_name": "ExactPDF",
                    "source_xref": 7,
                    "error_type": "ExactSourceFailure",
                    "detail": "bounded source/runtime proof",
                },
            )
        return AttemptOutcome.delivered(object(), entity_ids=("fallback",))

    entity, record = deliver_item(
        item_id="page:2:text:41",
        page_number=2,
        source_span_id=41,
        requested="text",
        attempt=attempt,
        cleanup=lambda _outcome: {"status": "complete", "removed": []},
    )

    assert entity is not None
    assert attempted == ["text", "3d_text"]
    assert record["fallback_used"] is True
    assert record["fallback_attempted"] is True


def test_wrong_page_exact_font_asset_fails_before_host_load(monkeypatch):
    fake, collection = _install(monkeypatch)
    item = _item()
    item.font_asset.page_number = 99
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is None
    assert fake.data.fonts.loaded == []
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["reason"] == "exact_font_asset_identity_mismatch"


def test_corrupt_deterministic_font_cache_is_atomically_repaired(monkeypatch, tmp_path):
    fake, collection = _install(monkeypatch)
    item = _item()
    digest = item.font_asset.usable_sha256
    cache_path = tmp_path / "bc_bl_pdf_exact_fonts" / f"{digest}.cff"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"corrupt-prior-attempt")
    monkeypatch.setattr(bl_text_builder.tempfile, "gettempdir", lambda: str(tmp_path))
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is not None
    assert cache_path.read_bytes() == item.font_asset.usable_bytes
    assert fake.data.fonts.loaded == [(str(cache_path), False)]


def test_removed_blender_font_datablock_is_evicted_and_reloaded(monkeypatch, tmp_path):
    fake, _collection = _install(monkeypatch)
    item = _item()
    digest = item.font_asset.usable_sha256
    monkeypatch.setattr(bl_text_builder.tempfile, "gettempdir", lambda: str(tmp_path))

    class RemovedFont:
        @property
        def packed_file(self):
            raise ReferenceError("StructRNA of type VectorFont has been removed")

    stale = RemovedFont()
    bl_text_builder._FONT_CACHE[digest] = stale

    font, failure = bl_text_builder._load_exact_font(item, "page:2:text:41", 2)

    assert failure is None
    assert font is not None and font is not stale
    assert bl_text_builder._FONT_CACHE[digest] is font
    assert len(fake.data.fonts.loaded) == 1


def test_font_cache_uses_unique_attempt_temp_files_and_leaves_none_behind(
    monkeypatch,
    tmp_path,
):
    _fake, _collection = _install(monkeypatch)
    item = _item()
    digest = item.font_asset.usable_sha256
    cache_path = tmp_path / "bc_bl_pdf_exact_fonts" / f"{digest}.cff"
    cache_path.parent.mkdir(parents=True)
    monkeypatch.setattr(bl_text_builder.tempfile, "gettempdir", lambda: str(tmp_path))
    original_replace = bl_text_builder.os.replace
    replace_sources = []

    def recording_replace(source, destination):
        replace_sources.append(str(source))
        return original_replace(source, destination)

    monkeypatch.setattr(bl_text_builder.os, "replace", recording_replace)
    for index in range(2):
        cache_path.write_bytes(f"corrupt-{index}".encode("ascii"))
        bl_text_builder._FONT_CACHE.clear()
        font, failure = bl_text_builder._load_exact_font(item, "page:2:text:41", 2)
        assert font is not None and failure is None

    assert len(replace_sources) == 2
    assert len(set(replace_sources)) == 2
    assert not list(cache_path.parent.glob("*.tmp"))


def test_missing_delivered_identity_retains_owned_refs_for_cleanup():
    owned_object = object()
    owned_data = object()
    cleanup_calls = []

    entity, record = deliver_item(
        item_id="page:2:text:41",
        page_number=2,
        source_span_id=41,
        requested="raster",
        attempt=lambda _representation: AttemptOutcome.delivered(
            owned_object,
            entity_ids=(),
            owned_artifacts=({"object": "partial", "datablock": "partial_mesh"},),
            owned_objects=(owned_object,),
            owned_datablocks=(owned_data,),
        ),
        cleanup=lambda outcome: cleanup_calls.append(outcome) or {
            "status": "complete",
            "removed": ["partial", "partial_mesh"],
        },
    )

    assert entity is None
    assert record["status"] == "failed"
    assert record["attempts"][0]["reason"] == "delivered_attempt_missing_verified_entity_identity"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].owned_objects == (owned_object,)
    assert cleanup_calls[0].owned_datablocks == (owned_data,)
