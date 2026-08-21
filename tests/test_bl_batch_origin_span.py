"""Regression: batched PDF paths must keep page coordinates, not a origin starburst.

Blender 3.2 ignores ``spline.points[i].co = (x, y, z, w)`` on the default
first spline point (the RNA setter is a no-op). Every polyline then starts at
world origin, so Top Orthographic at the grid looks like an ink-blot starburst
instead of the sheet. Object ``location`` may still be (0,0,0); the span lives
in curve data. Frame-All must also read those spline points when ``bound_box``
is stale/collapsed.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


MM_TO_M = 0.001


class _Co32(list):
    """Stand-in for Blender 3.2 ``bpy_float[4]``: item writes work, replacement does not."""


class _Point32:
    def __init__(self) -> None:
        self._co = _Co32([0.0, 0.0, 0.0, 1.0])

    @property
    def co(self):
        return self._co

    @co.setter
    def co(self, _value) -> None:
        return


class _Points32(list):
    def add(self, count: int) -> None:
        for _ in range(int(count)):
            self.append(_Point32())

    def foreach_set(self, attr: str, data) -> None:
        if attr != "co":
            raise ValueError(attr)
        values = list(data)
        for index, point in enumerate(self):
            base = index * 4
            point._co[:] = [float(values[base + axis]) for axis in range(4)]


class _Spline32:
    def __init__(self, kind: str) -> None:
        self.type = kind
        self.points = _Points32([_Point32()])
        self.bezier_points = []
        self.use_cyclic_u = False
        self.order_u = 4


class _Splines32:
    def __init__(self) -> None:
        self._items: list[_Spline32] = []

    def new(self, kind: str) -> _Spline32:
        spline = _Spline32(kind)
        self._items.append(spline)
        return spline

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


class _Curve32:
    def __init__(self, name: str) -> None:
        self.name = name
        self.dimensions = "3D"
        self.resolution_u = 12
        self.bevel_depth = 0.0
        self.materials: list = []
        self.splines = _Splines32()


class _Object32:
    def __init__(self, name: str, data) -> None:
        self.name = name
        self.data = data
        self.type = "CURVE"
        self.location = [0.0, 0.0, 0.0]
        self.matrix_world = _Identity()
        self.bound_box = [(0.0, 0.0, 0.0)] * 8


class _Identity:
    translation = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)

    def __matmul__(self, vec):
        return vec


class _LinkedObjects:
    def __init__(self) -> None:
        self.items: list = []

    def link(self, obj) -> None:
        self.items.append(obj)


class _Collection32:
    def __init__(self) -> None:
        self.objects = _LinkedObjects()


class _NewFactory:
    def __init__(self, factory) -> None:
        self._factory = factory
        self.created: list = []

    def new(self, *args, **kwargs):
        obj = self._factory(*args, **kwargs)
        self.created.append(obj)
        return obj


class _Vector:
    def __init__(self, values) -> None:
        seq = list(values) + [0.0, 0.0, 0.0]
        self.x = float(seq[0])
        self.y = float(seq[1])
        self.z = float(seq[2])


def _install_blender32_curve_host() -> types.SimpleNamespace:
    fake_bpy = types.SimpleNamespace(
        types=types.SimpleNamespace(Collection=object, Material=object, Object=object),
        data=types.SimpleNamespace(
            curves=_NewFactory(lambda name, type="CURVE": _Curve32(name)),
            objects=_NewFactory(lambda name, data: _Object32(name, data)),
        ),
    )
    sys.modules["bpy"] = fake_bpy
    sys.modules.setdefault("bmesh", types.SimpleNamespace())
    fake_mathutils = types.ModuleType("mathutils")
    fake_mathutils.Vector = _Vector
    sys.modules["mathutils"] = fake_mathutils
    return fake_bpy


def _reload_builder():
    _install_blender32_curve_host()
    module_name = "pdf_vector_importer.bl_geometry_builder"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def _spline_coords(obj) -> list[tuple[float, float, float]]:
    coords = []
    for spline in obj.data.splines:
        for point in spline.points:
            coords.append((float(point.co[0]), float(point.co[1]), float(point.co[2])))
    return coords


def test_batched_polylines_keep_page_span_when_tuple_co_assignment_is_noop():
    """Two distant source runs must not both grow a spoke from (0,0,0)."""
    builder = _reload_builder()
    collection = _Collection32()
    left_border = [(12.0, 12.0), (12.0, 600.0)]
    right_border = [(880.0, 12.0), (880.0, 600.0)]

    obj = builder._create_multi_poly_curve(
        "P1_batch_001",
        [left_border, right_border],
        collection,
        0.25,
        object(),
        z_offset_m=0.0001,
    )

    assert obj is not None
    assert list(obj.location) == [0.0, 0.0, 0.0]
    coords = _spline_coords(obj)
    assert len(coords) == 4
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    assert min(xs) == pytest.approx(12.0 * MM_TO_M)
    assert max(xs) == pytest.approx(880.0 * MM_TO_M)
    assert min(ys) == pytest.approx(12.0 * MM_TO_M)
    assert max(ys) == pytest.approx(600.0 * MM_TO_M)
    first_points = [
        (float(spline.points[0].co[0]), float(spline.points[0].co[1]))
        for spline in obj.data.splines
    ]
    assert first_points[0] == pytest.approx((12.0 * MM_TO_M, 12.0 * MM_TO_M))
    assert first_points[1] == pytest.approx((880.0 * MM_TO_M, 12.0 * MM_TO_M))
    origin_starts = [
        point
        for point in first_points
        if abs(point[0]) < 1e-9 and abs(point[1]) < 1e-9
    ]
    assert origin_starts == []
    origin_any = [
        coord
        for coord in coords
        if abs(coord[0]) < 1e-9 and abs(coord[1]) < 1e-9
    ]
    assert origin_any == [], origin_any


def test_no_spline_point_at_world_origin_when_source_paths_do_not():
    """1011 page ink never passes through (0,0); a leftover default point draws the X."""
    builder = _reload_builder()
    collection = _Collection32()
    runs = [
        [(12.0, 12.0), (12.0, 40.0), (80.0, 40.0)],
        [(880.0, 12.0), (898.0, 12.0)],
        [(400.0, 300.0), (410.0, 310.0), (420.0, 300.0)],
    ]
    obj = builder._create_multi_poly_curve(
        "P1_batch_1011like",
        runs,
        collection,
        0.25,
        object(),
        z_offset_m=0.0001,
    )
    origin_xy = []
    for spline in obj.data.splines:
        assert len(spline.points) >= 2
        for point in spline.points:
            x, y = float(point.co[0]), float(point.co[1])
            if abs(x) < 1e-9 and abs(y) < 1e-9:
                origin_xy.append((x, y, float(point.co[2])))
    assert origin_xy == []


def test_single_poly_curve_writes_the_source_start_not_the_default_origin():
    builder = _reload_builder()
    collection = _Collection32()
    obj = builder._create_poly_curve(
        "P1_line_1",
        [(250.0, 400.0), (250.0, 500.0)],
        False,
        collection,
        0.25,
        object(),
        z_offset_m=0.0,
    )
    first = next(iter(obj.data.splines)).points[0].co
    assert (float(first[0]), float(first[1])) == pytest.approx(
        (250.0 * MM_TO_M, 400.0 * MM_TO_M)
    )


def test_world_bounds_use_curve_spline_points_when_bound_box_is_collapsed():
    builder = _reload_builder()
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    engine = importlib.reload(engine)
    collection = _Collection32()
    obj = builder._create_multi_poly_curve(
        "P1_batch_002",
        [[(12.0, 12.0), (898.0, 12.0)], [(12.0, 602.0), (898.0, 602.0)]],
        collection,
        None,
        object(),
    )
    obj.bound_box = [(0.0, 0.0, 0.0)] * 8

    min_v, max_v = engine._world_bounds_for_objects([obj])
    assert min_v is not None and max_v is not None
    assert max_v.x - min_v.x == pytest.approx(886.0 * MM_TO_M, abs=1e-9)
    assert max_v.y - min_v.y == pytest.approx(590.0 * MM_TO_M, abs=1e-9)
    assert abs(min_v.x) > 0.001
    assert abs(min_v.y) > 0.001
