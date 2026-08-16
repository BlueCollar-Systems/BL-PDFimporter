"""Requested-representation invariant gates for Blender text delivery."""
from __future__ import annotations

import builtins
from dataclasses import replace
from io import BytesIO
import math
from hashlib import sha256
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
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine, bl_text_builder
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
        copied.splines = list(self.splines)
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

    def copy(self):
        copied = _MeshData(f"{self.name}_copy", self.base_dimensions)
        copied.vertices = list(self.vertices)
        copied.polygons = list(self.polygons)
        copied.materials.extend(self.materials)
        return copied


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
    def __init__(
        self,
        name: str,
        data,
        *,
        allow_to_curve: bool = True,
        call_counter=None,
    ):
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
        self._call_counter = call_counter

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
        if self._call_counter is not None:
            self._call_counter.to_curve_calls += 1
            self._call_counter.to_curve_excluded.append(
                bool(self._call_counter.exclude_reader())
            )
        if not self._allow_to_curve:
            raise RuntimeError("curve conversion crashed")
        curve = _CurveData(f"{self.name}_outline", self.dimensions)
        curve.materials.extend(getattr(self.data, "materials", []))
        if str(getattr(self.data, "body", "")).isspace():
            curve.splines = []
        return curve

    def to_curve_clear(self):
        return None


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
        self.to_curve_calls = 0
        self.to_curve_excluded = []
        self.font_new_excluded = []
        self.exclude_reader = lambda: False

    def new(self, name: str, data):
        obj = _Object(
            name,
            data,
            allow_to_curve=self.allow_to_curve,
            call_counter=self,
        )
        if str(getattr(data, "type", "") or "") == "FONT":
            self.font_new_excluded.append(bool(self.exclude_reader()))
        return obj

    def remove(self, obj, do_unlink=True):
        assert do_unlink is True
        self.removed.append(obj.name)


class _Meshes:
    def __init__(self, *, fail=False, available=True):
        self.fail = fail
        self.available = available
        self.removed = []
        self.new_from_object_calls = 0
        self.new_from_object_excluded = []
        self.exclude_reader = lambda: False
        if not available:
            self.new_from_object = None

    def new_from_object(self, evaluated, depsgraph=None):
        del depsgraph
        self.new_from_object_calls += 1
        self.new_from_object_excluded.append(bool(self.exclude_reader()))
        if not self.available:
            raise AttributeError("new_from_object unavailable")
        if self.fail:
            raise RuntimeError("mesh conversion crashed")
        mesh = _MeshData(f"{evaluated.name}_mesh", evaluated.dimensions)
        mesh.materials.extend(getattr(evaluated.data, "materials", []))
        if str(getattr(evaluated.data, "body", "")).isspace():
            mesh.vertices = []
            mesh.polygons = []
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


class _LayerCollection:
    def __init__(self, collection, history):
        self.collection = collection
        self.children = []
        self._exclude = False
        self._history = history

    @property
    def exclude(self):
        return self._exclude

    @exclude.setter
    def exclude(self, value):
        self._exclude = bool(value)
        self._history.append(self._exclude)


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
        self.layer_exclude_history = []

        def update_view_layer():
            self.view_update_count += 1

        objects = _Objects(allow_to_curve=curve_available)
        meshes = _Meshes(fail=mesh_fail, available=mesh_available)
        self.app = types.SimpleNamespace(version=(5, 2, 0))
        self.data = types.SimpleNamespace(
            curves=_Curves(baseline_available=baseline_available),
            objects=objects,
            meshes=meshes,
            fonts=_Fonts(),
            images=_Images(),
            materials=_Materials(),
        )
        self.context = types.SimpleNamespace(
            evaluated_depsgraph_get=lambda: object(),
            view_layer=types.SimpleNamespace(
                update=update_view_layer,
                layer_collection=None,
            ),
        )
        objects.exclude_reader = lambda: bool(
            getattr(self.context.view_layer.layer_collection, "exclude", False)
        )
        meshes.exclude_reader = objects.exclude_reader


def _sfnt_font_bytes(*, units_per_em=1000, y_min=-250, y_max=1000):
    """Minimal single-table sfnt whose only content is a valid 'head' table.

    Blender/FreeType normalizes a loaded vector font by the head-table global
    bounding-box height (yMax - yMin), so tests choose an extent that differs
    from BOTH units_per_em and hhea ascender-descender to pin the basis.
    """
    import struct

    head = struct.pack(
        ">IIIIHHqqhhhhHHhhh",
        0x00010000,  # version
        0,  # fontRevision
        0,  # checkSumAdjustment
        0x5F0F3CF5,  # magicNumber
        0,  # flags
        units_per_em,
        0,  # created
        0,  # modified
        -100,  # xMin
        y_min,
        900,  # xMax
        y_max,
        0,  # macStyle
        0,  # lowestRecPPEM
        2,  # fontDirectionHint
        0,  # indexToLocFormat
        0,  # glyphDataFormat
    )
    header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    entry = struct.pack(">4sIII", b"head", 0, 28, len(head))
    return header + entry + head


# head bbox extent baked into the shared fixture: 1000 - (-250) = 1250 design
# units with units_per_em 1000 and hhea ascender-descender 800-(-200) = 1000.
_FIXTURE_FONT_BBOX_EXTENT = 1250


def _font_asset():
    font_bytes = _sfnt_font_bytes(units_per_em=1000, y_min=-250, y_max=1000)
    return types.SimpleNamespace(
        asset_id="sha256:abcdef",
        usable_sha256=sha256(font_bytes).hexdigest(),
        usable_format="ttf",
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


def _character_layout_repeated_a():
    first, second = _character_layout()
    return (
        first,
        TextCharLayout(
            text="A",
            glyph_id=37,
            source_origin_pdf=second.source_origin_pdf,
            source_bbox_pdf=second.source_bbox_pdf,
            source_quad_pdf=second.source_quad_pdf,
            target_origin=second.target_origin,
            target_quad=second.target_quad,
            advance_width=second.advance_width,
            glyph_height=second.glyph_height,
        ),
    )


def _character_layout_with_space():
    first, last = _character_layout()
    whitespace = TextCharLayout(
        text=" ",
        glyph_id=None,
        source_origin_pdf=(16.0, 20.0),
        source_bbox_pdf=(16.0, 10.0, 18.0, 22.0),
        source_quad_pdf=((16.0, 10.0), (18.0, 10.0), (18.0, 22.0), (16.0, 22.0)),
        target_origin=(18.0, 24.0),
        target_quad=((18.0, 30.0), (20.0, 30.0), (20.0, 24.0), (18.0, 24.0)),
        advance_width=2.0,
        glyph_height=6.0,
    )
    return first, whitespace, last


def _install(monkeypatch, **kwargs):
    fake = _FakeBpy(**kwargs)
    collection = _Collection()
    fake.context.view_layer.layer_collection = _LayerCollection(
        collection, fake.layer_exclude_history
    )
    monkeypatch.setattr(bl_text_builder, "bpy", fake)
    monkeypatch.setattr(
        bl_text_builder,
        "_exact_font_glyph_has_visible_ink",
        lambda _asset, _glyph_id: True,
    )
    bl_text_builder._FONT_CACHE.clear()
    if hasattr(bl_text_builder, "_VERIFIED_FONT_ASSET_BYTES"):
        bl_text_builder._VERIFIED_FONT_ASSET_BYTES.clear()
    if hasattr(bl_text_builder, "_VERIFIED_PACKED_FONTS"):
        bl_text_builder._VERIFIED_PACKED_FONTS.clear()
    return fake, collection


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


def test_synthetic_page_number_contract_relabels_page_1_fixture_truthfully(
    monkeypatch,
):
    _fake, collection = _install(monkeypatch)
    selected_page_numbers = [
        index + 1 for index in bl_import_engine._parse_pages("2,4", total_pages=4)
    ]
    records = []

    for page_number in selected_page_numbers:
        opts = types.SimpleNamespace(import_mode="vector", text_mode="text")
        item = _item(span_id=40 + page_number)
        font_asset = types.SimpleNamespace(**vars(item.font_asset))
        assert item.page_number == 2
        font_asset.page_number = page_number
        item = replace(item, page_number=page_number, font_asset=font_asset)
        obj = bl_text_builder.build_text(
            item,
            collection,
            page_number=page_number,
            text_mode="text",
            provenance_opts=opts,
        )
        assert obj is not None
        assert obj["pdf_source_item_id"] == f"page:{page_number}:text:{40 + page_number}"
        records.append(opts._text_delivery_records[-1])

    assert selected_page_numbers == [2, 4]
    assert {
        "source_items": len(selected_page_numbers),
        "delivered_items": sum(record["status"] == "delivered" for record in records),
        "failed_items": sum(record["status"] != "delivered" for record in records),
    } == {"source_items": 2, "delivered_items": 2, "failed_items": 0}


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
    assert evidence["dependency_graph_updates"] == 0
    assert evidence["metric_host_update_skipped"] is True
    assert fake.view_update_count == 0
    assert verification_update_counts == [0, 0]


def test_positioned_native_text_omits_proven_zero_ink_character_objects(monkeypatch):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda _obj, text_item: (
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
        ),
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="text")
    item = _item()
    item.text = "A B"
    item.normalized = "A B"
    item.source_char_layout = _character_layout_with_space()
    item.requires_individual_positioning = True

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode="text",
        provenance_opts=opts,
    )

    assert obj is not None
    assert [candidate.data.body for candidate in collection.objects.items] == ["A", "B"]
    evidence = opts._text_delivery_records[-1]["attempts"][-1]["evidence"]
    whitespace = evidence["character_entities"][1]
    assert whitespace["entity_ids"] == []
    assert whitespace["verification"]["zero_ink_identity"] is True
    assert whitespace["verification"]["visible_geometry_omitted"] is True
    assert whitespace["verification"]["advance_preserved"] is True
    assert fake.view_update_count == 0


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
    # Placement is certified on the delivered curve/mesh after the final
    # page update. Temporary FONT vehicles are identity-checked only.
    assert verification_updates == [
        (expected_type, 2),
        (expected_type, 2),
    ]


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_page_positioned_conversion_shares_two_dependency_graph_updates(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: (
            [],
            {"evaluated_bounds_verified": True},
        ),
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    items = []
    for span_id in (41, 42):
        item = _item(span_id)
        item.text = "AB"
        item.normalized = "AB"
        item.source_char_layout = _character_layout()
        item.requires_individual_positioning = True
        items.append(item)

    count = bl_text_builder.build_all_text(
        items,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert count == 2
    assert len(collection.objects.items) == 4
    assert {candidate.type for candidate in collection.objects.items} == {
        expected_type
    }
    assert fake.view_update_count == 2
    records = opts._text_delivery_records
    assert len(records) == 2
    assert {record["final_representation"] for record in records} == {mode}
    assert all(record["fallback_used"] is False for record in records)
    assert all(
        record["attempts"][-1]["evidence"].get("page_shared_dependency_graph") is True
        for record in records
    )
    assert all(
        record["attempts"][-1]["evidence"].get("dependency_graph_updates") == 2
        for record in records
    )


def _patch_positioned_conversion_fakes(monkeypatch):
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: (
            [],
            {"evaluated_bounds_verified": True},
        ),
    )


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_repeated_glyph_converts_once_and_instances_each_span(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    _patch_positioned_conversion_fakes(monkeypatch)
    item = _item()
    item.text = "AA"
    item.normalized = "AA"
    item.source_char_layout = _character_layout_repeated_a()
    item.requires_individual_positioning = True

    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
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
    assert [candidate["pdf_text_source"] for candidate in collection.objects.items] == [
        "A",
        "A",
    ]
    if mode == "glyphs":
        assert fake.data.objects.to_curve_calls == 1
    else:
        assert fake.data.meshes.new_from_object_calls == 1
    entities = opts._text_delivery_records[-1]["attempts"][-1]["evidence"][
        "character_entities"
    ]
    assert not bool(entities[0].get("verification", {}).get("converted_template_reused"))
    assert entities[1].get("verification", {}).get("converted_template_reused") is True
    assert collection.objects.items[0].data is collection.objects.items[1].data
    assert "evaluated_affine_matrix" not in entities[1]["verification"]
    assert "intended_affine_matrix" not in entities[1]["verification"]


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_converted_affine_applies_only_to_final_curve_or_mesh(
    monkeypatch,
    mode,
    expected_type,
):
    """Temporary FONT vehicles are not placement-transformed; instances still are."""
    _fake, collection = _install(monkeypatch)
    affine_types = []

    def record_affine(obj, *_args, **_kwargs):
        affine_types.append(str(getattr(obj, "type", "")))

    monkeypatch.setattr(bl_text_builder, "_apply_target_quad_affine", record_affine)
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {"evaluated_bounds_verified": True}),
    )
    item = _item()
    item.text = "AA"
    item.normalized = "AA"
    item.source_char_layout = _character_layout_repeated_a()
    item.requires_individual_positioning = True

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=types.SimpleNamespace(import_mode="vector", text_mode=mode),
    )

    assert obj is not None
    assert affine_types == [expected_type, expected_type]


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_page_repeated_glyphs_convert_once_per_unique_outline(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    _patch_positioned_conversion_fakes(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    items = []
    for span_id in (41, 42):
        item = _item(span_id)
        item.text = "AB"
        item.normalized = "AB"
        item.source_char_layout = _character_layout()
        item.requires_individual_positioning = True
        items.append(item)

    count = bl_text_builder.build_all_text(
        items,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert count == 2
    assert len(collection.objects.items) == 4
    assert {candidate.type for candidate in collection.objects.items} == {
        expected_type
    }
    assert fake.view_update_count == 2
    assert all(record["fallback_used"] is False for record in opts._text_delivery_records)
    if mode == "glyphs":
        assert fake.data.objects.to_curve_calls == 2
    else:
        assert fake.data.meshes.new_from_object_calls == 2
    reused_flags = [
        bool(entry.get("verification", {}).get("converted_template_reused"))
        for record in opts._text_delivery_records
        for entry in record["attempts"][-1]["evidence"]["character_entities"]
    ]
    assert reused_flags.count(True) == 2
    assert reused_flags.count(False) == 2
    objects = collection.objects.items
    assert objects[0].data is objects[2].data
    assert objects[1].data is objects[3].data
    assert objects[0].data is not objects[1].data


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_same_glyph_at_two_source_sizes_converts_once(
    monkeypatch,
    mode,
    expected_type,
):
    """Source size lives in the target quad affine, not a second FONT conversion."""
    fake, collection = _install(monkeypatch)
    _patch_positioned_conversion_fakes(monkeypatch)
    small = _character_layout()[0]
    large = TextCharLayout(
        text="A",
        glyph_id=37,
        source_origin_pdf=(10.0, 20.0),
        source_bbox_pdf=(10.0, 10.0, 22.0, 34.0),
        source_quad_pdf=((10.0, 10.0), (22.0, 10.0), (22.0, 34.0), (10.0, 34.0)),
        target_origin=(12.0, 24.0),
        target_quad=((12.0, 36.0), (24.0, 36.0), (24.0, 24.0), (12.0, 24.0)),
        advance_width=12.0,
        glyph_height=12.0,
    )
    items = []
    for span_id, layout, size in ((41, small, 6.0), (42, large, 12.0)):
        item = _item(span_id)
        item.text = "A"
        item.normalized = "A"
        item.font_size = size
        item.source_char_layout = (layout,)
        item.requires_individual_positioning = True
        items.append(item)

    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    count = bl_text_builder.build_all_text(
        items,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert count == 2
    assert len(collection.objects.items) == 2
    assert {candidate.type for candidate in collection.objects.items} == {
        expected_type
    }
    if mode == "glyphs":
        assert fake.data.objects.to_curve_calls == 1
    else:
        assert fake.data.meshes.new_from_object_calls == 1
    reused_flags = [
        bool(entry.get("verification", {}).get("converted_template_reused"))
        for record in opts._text_delivery_records
        for entry in record["attempts"][-1]["evidence"]["character_entities"]
    ]
    assert reused_flags.count(True) == 1
    assert reused_flags.count(False) == 1
    assert collection.objects.items[0].data is collection.objects.items[1].data


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_page_glyph_instances_are_created_off_the_evaluated_view_layer(
    monkeypatch,
    mode,
    expected_type,
):
    """Unique FONT sources and mass instance linking stay off the evaluated view layer."""
    fake, collection = _install(monkeypatch)
    _patch_positioned_conversion_fakes(monkeypatch)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)
    items = []
    for span_id in (41, 42):
        item = _item(span_id)
        item.text = "AB"
        item.normalized = "AB"
        item.source_char_layout = _character_layout()
        item.requires_individual_positioning = True
        items.append(item)

    count = bl_text_builder.build_all_text(
        items,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert count == 2
    assert True in fake.layer_exclude_history
    assert fake.layer_exclude_history[-1] is False
    assert fake.data.objects.font_new_excluded
    assert all(fake.data.objects.font_new_excluded)
    if mode == "glyphs":
        assert fake.data.objects.to_curve_excluded
        assert not any(fake.data.objects.to_curve_excluded)
    else:
        assert fake.data.meshes.new_from_object_excluded
        assert not any(fake.data.meshes.new_from_object_excluded)
    objects = collection.objects.items
    assert len(objects) == 4
    assert {candidate.type for candidate in objects} == {expected_type}
    assert objects[0].data is objects[2].data
    assert objects[1].data is objects[3].data
    assert fake.view_update_count == 2


@pytest.mark.parametrize("mode", ["text", "3d_text", "glyphs", "geometry"])
def test_positioned_characters_share_one_attempt_owned_material(monkeypatch, mode):
    """Catch per-character shader construction returning to dense text paths."""
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {"evaluated_bounds_verified": True}),
    )
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
        provenance_opts=types.SimpleNamespace(import_mode="vector", text_mode=mode),
    )

    assert obj is not None
    assert len(collection.objects.items) == 2
    assigned = [candidate.data.materials[0] for candidate in collection.objects.items]
    assert assigned[0] is assigned[1]
    assert assigned[0].use_nodes is True
    assert len(fake.data.materials.items) == 1


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_positioned_conversion_preserves_whitespace_advance_without_visible_geometry(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )

    def verify_positioned_transform(obj, text_item):
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
    item.text = "A B"
    item.normalized = "A B"
    item.source_char_layout = _character_layout_with_space()
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
    evidence = record["attempts"][-1]["evidence"]
    assert evidence["character_count"] == 3
    assert [entry["text"] for entry in evidence["character_entities"]] == [
        "A",
        " ",
        "B",
    ]
    whitespace = evidence["character_entities"][1]
    assert whitespace["entity_ids"] == []
    assert whitespace["verification"]["zero_ink_identity"] is True
    assert whitespace["verification"]["visible_geometry_omitted"] is True
    assert whitespace["verification"]["advance_preserved"] is True
    assert whitespace["target_origin_model"] == [18.0, 24.0]
    assert evidence["character_entities"][2]["target_origin_model"] == [20.0, 24.0]
    assert not any("_c0001_" in name for name in fake.data.curves.removed)
    assert fake.view_update_count == 2


@pytest.mark.parametrize(
    ("mode", "expected_type", "geometry_attr"),
    [("glyphs", "CURVE", "splines"), ("geometry", "MESH", "vertices")],
)
def test_positioned_conversion_delivers_whitespace_only_span_as_empty_typed_carrier(
    monkeypatch,
    mode,
    expected_type,
    geometry_attr,
):
    """Catch zero-ink source items being rejected or replaced with invented ink."""
    _fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {"evaluated_bounds_verified": True}),
    )
    item = _item()
    item.text = " "
    item.normalized = ""
    item.source_char_layout = (_character_layout_with_space()[1],)
    item.requires_individual_positioning = True
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    obj = bl_text_builder.build_text(
        item,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is not None and obj.type == expected_type
    assert len(collection.objects.items) == 1
    assert len(getattr(obj.data, geometry_attr)) == 0
    assert obj["pdf_zero_ink_identity"] is True
    assert obj["pdf_visible_geometry_omitted"] is True
    assert obj["pdf_advance_preserved"] is True
    record = opts._text_delivery_records[-1]
    assert record["status"] == "delivered"
    assert record["final_representation"] == mode
    assert len(record["entity_ids"]) == 1
    evidence = record["attempts"][-1]["evidence"]
    assert evidence["zero_ink_identity"] is True
    assert evidence["visible_geometry_omitted"] is True
    assert evidence["advance_preserved"] is True


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("glyphs", "CURVE"), ("geometry", "MESH")],
)
def test_positioned_conversion_preserves_exact_font_empty_glyph_advance(
    monkeypatch,
    mode,
    expected_type,
):
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_apply_target_quad_affine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bl_text_builder,
        "_exact_font_glyph_has_visible_ink",
        lambda _asset, glyph_id: int(glyph_id) != 91,
        raising=False,
    )

    def verify_positioned_transform(obj, text_item):
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
    assert len(collection.objects.items) == 1
    assert collection.objects.items[0].type == expected_type
    record = opts._text_delivery_records[-1]
    assert record["final_representation"] == mode
    assert len(record["entity_ids"]) == 1
    characters = record["attempts"][-1]["evidence"]["character_entities"]
    assert [entry["text"] for entry in characters] == ["A", "B"]
    assert characters[1]["entity_ids"] == []
    assert characters[1]["verification"]["zero_ink_identity"] is True
    assert characters[1]["verification"]["zero_ink_reason"] == (
        "exact_font_glyph_has_no_visible_bounds"
    )
    assert characters[1]["verification"]["source_glyph_visible_ink"] is False
    assert characters[1]["verification"]["advance_preserved"] is True
    assert characters[1]["target_origin_model"] == [20.0, 24.0]
    assert fake.view_update_count == 2


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


def test_exact_font_glyph_visibility_distinguishes_ink_from_empty_advance():
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A", "B"])
    empty_pen = TTGlyphPen(None)
    empty = empty_pen.glyph()
    visible_pen = TTGlyphPen(None)
    visible_pen.moveTo((50, 0))
    visible_pen.lineTo((550, 0))
    visible_pen.lineTo((550, 700))
    visible_pen.lineTo((50, 700))
    visible_pen.closePath()
    builder.setupGlyf({".notdef": empty, "A": visible_pen.glyph(), "B": empty})
    builder.setupHorizontalMetrics(
        {".notdef": (500, 0), "A": (600, 50), "B": (600, 0)}
    )
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupCharacterMap({65: "A", 66: "B"})
    builder.setupOS2()
    builder.setupNameTable(
        {
            "familyName": "Glyph visibility fixture",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Glyph visibility fixture Regular",
            "fullName": "Glyph visibility fixture Regular",
            "psName": "GlyphVisibilityFixture-Regular",
        }
    )
    builder.setupPost()
    builder.setupMaxp()
    output = BytesIO()
    builder.font.save(output, reorderTables=False)
    font_bytes = output.getvalue()
    asset = types.SimpleNamespace(
        usable_bytes=font_bytes,
        usable_sha256=sha256(font_bytes).hexdigest(),
    )
    bl_text_builder._FONT_GLYPH_INK_CACHE.clear()

    assert bl_text_builder._exact_font_glyph_has_visible_ink(asset, 1) is True
    assert bl_text_builder._exact_font_glyph_has_visible_ink(asset, 2) is False


def test_positioned_glyph_metrics_use_blender_font_bbox_normalization():
    # R-B private-fixture regression contract (v1.0.66..v1.0.68):
    # Blender/FreeType normalizes a loaded vector font so one data.size spans
    # the font's GLOBAL bounding-box height (head.yMax - head.yMin), not the
    # hhea ascender-descender line box. The disproved assumption
    # (local_unit_scale = size / (ascender - descender)) rendered every
    # positioned character at (asc-desc)/bbox_extent of its true size —
    # measured 2288/2794 = 0.819 with the drawing's embedded Arial subset,
    # hot cells 379 -> 497 on the visual-parity harness.
    child = bl_text_builder._character_text_item(_item(), _character_layout()[0])
    data = _FontData("BboxNormalization")
    # host-calibrated size: 6 mm source em * (1250 / 1000) bbox calibration
    data.size = 0.0075
    obj = _Object("BboxNormalization", data)

    metrics = bl_text_builder._positioned_font_axis_metrics(obj, child)

    assert metrics["metric_source"] == "embedded_font_glyph_metrics"
    assert metrics["font_normalization_units"] == _FIXTURE_FONT_BBOX_EXTENT
    # advance_units(500) rendered meters: 500 * data.size / bbox_extent(1250)
    assert metrics["local_advance"] == pytest.approx(500 * 0.0075 / 1250)
    assert metrics["advance_units"] == 500


def test_positioned_glyph_metrics_vertical_axis_is_neutral_quad_edge():
    # The PyMuPDF character quad's vertical edge is NOT a reliable
    # ascender-descender box (measured 0.937 x em on the owner drawing, an
    # ascender-descender box would be 1.117 x em). Mapping the font line box
    # onto it under-scales glyph ink. The quad's vertical edge supplies
    # DIRECTION only: local_line_height must equal the quad edge length so the
    # matrix's vertical column is a unit vector and the rendered vertical
    # scale stays exactly the calibrated source em scale.
    child = bl_text_builder._character_text_item(_item(), _character_layout()[0])
    data = _FontData("NeutralVertical")
    data.size = 0.0075
    obj = _Object("NeutralVertical", data)

    metrics = bl_text_builder._positioned_font_axis_metrics(obj, child)

    # layout target_quad UL(12,30) LL(12,24) mm -> vertical edge 6 mm = 0.006 m
    assert metrics["local_line_height"] == pytest.approx(0.006)
    matrix = bl_text_builder._metric_character_matrix_values(
        local_advance=metrics["local_advance"],
        local_line_height=metrics["local_line_height"],
        local_baseline_y=metrics["local_baseline_y"],
        target_origin=child.insertion,
        target_quad=child.target_quad_model,
        z=0.0,
    )
    # vertical column must be a unit vector: rendered vertical scale 1.0
    assert math.hypot(matrix[0][1], matrix[1][1]) == pytest.approx(1.0)


def _large_a_layout():
    return TextCharLayout(
        text="A",
        glyph_id=37,
        source_origin_pdf=(10.0, 20.0),
        source_bbox_pdf=(10.0, 10.0, 22.0, 34.0),
        source_quad_pdf=((10.0, 10.0), (22.0, 10.0), (22.0, 34.0), (10.0, 34.0)),
        target_origin=(12.0, 24.0),
        target_quad=((12.0, 36.0), (24.0, 36.0), (24.0, 24.0), (12.0, 24.0)),
        advance_width=12.0,
        glyph_height=12.0,
    )


def _metric_vertical_scale(obj, child) -> float:
    metrics = bl_text_builder._positioned_font_axis_metrics(obj, child)
    matrix = bl_text_builder._metric_character_matrix_values(
        local_advance=metrics["local_advance"],
        local_line_height=metrics["local_line_height"],
        local_baseline_y=metrics["local_baseline_y"],
        target_origin=child.insertion,
        target_quad=child.target_quad_model,
        z=0.0,
    )
    return math.hypot(matrix[0][1], matrix[1][1])


def test_reused_glyph_template_scales_vertical_axis_by_size_ratio():
    # 1011 visual-oracle regression (v1.0.89, #30 'share glyph outlines across
    # size and color'): a template converted for a 6 mm span is instanced for
    # a 12 mm span. Its local outline is still at the 6 mm em, so the metric
    # matrix must scale the vertical axis by 12/6 = 2.0 (the advance axis is
    # already scaled through local_advance/target width). Leaving the vertical
    # column a unit vector renders every reused glyph at the FIRST size seen
    # (mixed tall/tiny letters in 'TOWER', '531-25', 'SHOP BOLTS').
    parent = _item()
    parent.font_size = 12.0
    child = bl_text_builder._character_text_item(parent, _large_a_layout())
    reused = _Object("ReusedTemplate", _CurveData("Template_outline"))
    # template converted from a 6 mm source: host size 6 mm * (1250 / 1000)
    reused["pdf_font_data_size"] = 0.0075
    reused["pdf_converted_template_reused"] = True

    metrics = bl_text_builder._positioned_font_axis_metrics(reused, child)

    # advance is measured in the TEMPLATE outline's local units (6 mm em)
    assert metrics["local_advance"] == pytest.approx(500 * 0.0075 / 1250)
    assert _metric_vertical_scale(reused, child) == pytest.approx(2.0)

    # the same object at the template's own size keeps the neutral unit column
    same_size_parent = _item()
    same_size_parent.font_size = 6.0
    same_size_child = bl_text_builder._character_text_item(
        same_size_parent, _character_layout()[0]
    )
    assert _metric_vertical_scale(reused, same_size_child) == pytest.approx(1.0)


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_two_sizes_sharing_a_glyph_get_proportional_vertical_extents(
    monkeypatch, mode
):
    """Both axes of a reused outline follow the item size, not the template's."""
    fake, collection = _install(monkeypatch)
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {"evaluated_bounds_verified": True}),
    )
    placements = []

    def record_affine(obj, text_item, *_args, **_kwargs):
        metrics = bl_text_builder._positioned_font_axis_metrics(obj, text_item)
        matrix = bl_text_builder._metric_character_matrix_values(
            local_advance=metrics["local_advance"],
            local_line_height=metrics["local_line_height"],
            local_baseline_y=metrics["local_baseline_y"],
            target_origin=text_item.insertion,
            target_quad=text_item.target_quad_model,
            z=0.0,
        )
        # local outline em height (m) x rendered vertical scale = world height
        outline_em_m = float(obj.get("pdf_font_data_size", 0.0) or 0.0)
        vertical_scale = math.hypot(matrix[0][1], matrix[1][1])
        horizontal_scale = math.hypot(matrix[0][0], matrix[1][0])
        placements.append(
            {
                "font_size": float(text_item.font_size),
                "reused": bool(obj.get("pdf_converted_template_reused")),
                "world_em_height_m": outline_em_m * vertical_scale,
                "world_em_width_m": outline_em_m * horizontal_scale,
            }
        )
        return None

    monkeypatch.setattr(bl_text_builder, "_apply_target_quad_affine", record_affine)
    items = []
    for span_id, layout, size in (
        (41, _character_layout()[0], 6.0),
        (42, _large_a_layout(), 12.0),
    ):
        item = _item(span_id)
        item.text = "A"
        item.normalized = "A"
        item.font_size = size
        item.source_char_layout = (layout,)
        item.requires_individual_positioning = True
        items.append(item)

    count = bl_text_builder.build_all_text(
        items,
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=types.SimpleNamespace(import_mode="vector", text_mode=mode),
    )

    assert count == 2
    if mode == "glyphs":
        assert fake.data.objects.to_curve_calls == 1
    else:
        assert fake.data.meshes.new_from_object_calls == 1
    by_size = {entry["font_size"]: entry for entry in placements}
    assert set(by_size) == {6.0, 12.0}
    assert by_size[6.0]["reused"] is False
    assert by_size[12.0]["reused"] is True
    ratio_h = by_size[12.0]["world_em_height_m"] / by_size[6.0]["world_em_height_m"]
    ratio_w = by_size[12.0]["world_em_width_m"] / by_size[6.0]["world_em_width_m"]
    assert ratio_h == pytest.approx(2.0)
    assert ratio_w == pytest.approx(2.0)


def test_positioned_font_candidate_calibrates_host_size_to_font_bbox(monkeypatch):
    # data.size must be source_em_size * (bbox_extent / units_per_em) so the
    # rendered em equals the source em after Blender's bbox normalization.
    fake, collection = _install(monkeypatch)
    captured_sizes = []

    def capture_affine(obj, *_args, **_kwargs):
        captured_sizes.append(float(obj.data.size))
        return None

    monkeypatch.setattr(bl_text_builder, "_apply_target_quad_affine", capture_affine)
    monkeypatch.setattr(
        bl_text_builder,
        "_verify_transform_and_dimensions",
        lambda *_args, **_kwargs: ([], {}),
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
    assert captured_sizes, "positioned characters must reach the affine stage"
    # 6 mm source em * (1250/1000) = 7.5 mm host size
    for size in captured_sizes:
        assert size == pytest.approx(0.0075)


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


def test_metric_placement_properties_exclude_font_table_dumps() -> None:
    obj = {}
    bl_text_builder._write_metric_placement_properties(
        obj,
        matrix_values=(
            (1.0, 0.0, 0.0, 0.2),
            (0.0, 1.0, 0.0, 0.3),
            (0.0, 0.0, 1.0, 0.4),
            (0.0, 0.0, 0.0, 1.0),
        ),
        metric_evidence={
            "local_advance": 0.01,
            "local_line_height": 0.02,
            "local_baseline_y": 0.0,
            "glyph_id": 37,
            "ascender": 800,
            "descender": -200,
            "units_per_em": 1000,
            "advance_units": 500,
            "metric_source": "embedded_font_glyph_metrics",
        },
        positioned_character=True,
        target_quad=((0.0, 20.0), (10.0, 20.0), (10.0, 0.0), (0.0, 0.0)),
        origin=(0.0, 0.0),
    )

    assert obj["pdf_metric_affine_applied"] is True
    assert obj["pdf_metric_local_advance"] == 0.01
    assert "pdf_metric_glyph_id" not in obj
    assert "pdf_metric_ascender" not in obj
    assert "pdf_metric_units_per_em" not in obj
    assert "pdf_target_quad_model" not in obj
    assert "pdf_metric_metric_source" not in obj


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


@pytest.mark.parametrize(
    ("mode", "install_kwargs", "failure_reason"),
    [
        (
            "glyphs",
            {"curve_available": False},
            "glyph_curve_conversion_failed_not_impossibility_proof",
        ),
        (
            "geometry",
            {"mesh_fail": True},
            "geometry_mesh_conversion_failed_not_impossibility_proof",
        ),
    ],
)
def test_present_conversion_api_failure_stays_failed_without_fallback(
    monkeypatch,
    mode,
    install_kwargs,
    failure_reason,
):
    _fake, collection = _install(monkeypatch, **install_kwargs)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode=mode,
        provenance_opts=opts,
    )

    assert obj is None
    record = opts._text_delivery_records[-1]
    assert record["status"] == "failed"
    assert record["final_representation"] is None
    assert record["fallback_attempted"] is False
    assert [attempt["attempted_representation"] for attempt in record["attempts"]] == [
        mode
    ]
    attempt = record["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["reason"] == failure_reason
    assert attempt["evidence"]["exception_type"] == "RuntimeError"
    assert "capability_present" not in attempt["evidence"]
    assert attempt["cleanup"]["status"] == "complete"
    assert collection.objects.items == []


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
    expected_transparent=False,
    transparent_normalized=False,
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
        plane["pdf_raster_expected_transparent"] = expected_transparent
        plane["pdf_raster_transparent_normalized"] = transparent_normalized
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
    assert attempt["evidence"]["expected_transparent"] is False
    assert attempt["evidence"]["transparent_normalized"] is False
    assert attempt["owned_artifacts"][0]["file_path"] == str(clip)


def test_whitespace_raster_delivery_reports_canonical_normalization(monkeypatch, tmp_path):
    _fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(bl_import_engine._BLENDER_SAFE_TRANSPARENT_PNG)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        replace(_item(), text=" ", normalized=""),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            expected_transparent=True,
            transparent_normalized=True,
        ),
    )

    assert obj is not None
    evidence = opts._text_delivery_records[-1]["attempts"][0]["evidence"]
    assert evidence["expected_transparent"] is True
    assert evidence["transparent_normalized"] is True


def test_visible_raster_rejects_whitespace_normalization_metadata(monkeypatch, tmp_path):
    _fake, collection = _install(monkeypatch)
    clip = tmp_path / "item-41.png"
    clip.write_bytes(bl_import_engine._BLENDER_SAFE_TRANSPARENT_PNG)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="raster")

    obj = bl_text_builder.build_text(
        _item(),
        collection,
        page_number=2,
        text_mode="raster",
        provenance_opts=opts,
        terminal_raster_callback=_raster_callback_for_test(
            clip,
            expected_transparent=True,
            transparent_normalized=True,
        ),
    )

    assert obj is None
    attempt = opts._text_delivery_records[-1]["attempts"][0]
    assert attempt["status"] == "failed"
    assert "raster_transparency_metadata_mismatch" in attempt["evidence"]["failures"]


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
    cache_path = tmp_path / "bc_bl_pdf_exact_fonts" / f"{digest}.ttf"
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


def test_exact_font_integrity_is_hashed_and_packed_once_per_verified_asset(
    monkeypatch,
    tmp_path,
):
    _fake, _collection = _install(monkeypatch)
    item = _item()
    monkeypatch.setattr(bl_text_builder.tempfile, "gettempdir", lambda: str(tmp_path))
    original_sha256 = bl_text_builder.sha256
    original_verify = bl_text_builder.verify_packed_sha256
    source_hash_calls = []
    packed_verify_calls = []

    def counting_sha256(payload=b""):
        if bytes(payload) == item.font_asset.usable_bytes:
            source_hash_calls.append(1)
        return original_sha256(payload)

    def counting_verify(font, expected_sha):
        packed_verify_calls.append((font, expected_sha))
        return original_verify(font, expected_sha)

    monkeypatch.setattr(bl_text_builder, "sha256", counting_sha256)
    monkeypatch.setattr(bl_text_builder, "verify_packed_sha256", counting_verify)

    first, first_failure = bl_text_builder._load_exact_font(
        item, "page:2:text:41", 2
    )
    second, second_failure = bl_text_builder._load_exact_font(
        item, "page:2:text:42", 2
    )

    assert first_failure is None and second_failure is None
    assert first is second
    assert len(source_hash_calls) == 1
    assert packed_verify_calls == []


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
    cache_path = tmp_path / "bc_bl_pdf_exact_fonts" / f"{digest}.ttf"
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


def test_disk_font_sha_memo_avoids_reread_when_identity_unchanged(
    monkeypatch,
    tmp_path,
):
    item = _item()
    digest = item.font_asset.usable_sha256
    cache_path = tmp_path / "bc_bl_pdf_exact_fonts" / f"{digest}.ttf"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(item.font_asset.usable_bytes)
    bl_text_builder._DISK_FONT_SHA_MEMO.clear()
    reads = {"count": 0}
    original = bl_text_builder.Path.read_bytes

    def counting_read(self):
        if self.resolve() == cache_path.resolve():
            reads["count"] += 1
        return original(self)

    monkeypatch.setattr(bl_text_builder.Path, "read_bytes", counting_read)

    assert bl_text_builder._disk_font_cache_matches(cache_path, digest) is True
    assert bl_text_builder._disk_font_cache_matches(cache_path, digest) is True
    assert reads["count"] == 1


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
