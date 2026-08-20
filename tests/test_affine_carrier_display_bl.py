"""Affine carrier empties must not obscure the drawing in the viewport.

Blender's default empty display is PLAIN_AXES at 1.0 m, and one carrier is created PER
GLYPH. On 1011 that is 4182 carriers over a 0.887 x 0.591 m sheet: each draws a 2 m
axis-cross, and the viewport becomes a solid black starburst with the drawing buried
inside it (owner report, Blender 5.2, v1.0.94).

Empties do NOT render. Every automated check missed this: camera renders never show them,
and the visual oracle's bounds pass explicitly skips `obj.type == "EMPTY"`. It took a human
opening the file. These locks are the regression guard.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


def _install_blender_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same stub shape the remediation-contract tests use: bl_text_builder imports bpy."""
    fake_bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(version=(5, 2, 0)),
        ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(redraw_timer=lambda **_kwargs: None),
        ),
        types=types.SimpleNamespace(
            Collection=object, Material=object, Object=object, VectorFont=object,
        ),
    )
    monkeypatch.setitem(sys.modules, "bpy", fake_bpy)
    monkeypatch.setitem(sys.modules, "bmesh", types.SimpleNamespace())


@pytest.fixture()
def btb(monkeypatch: pytest.MonkeyPatch):
    _install_blender_stubs(monkeypatch)
    return importlib.import_module("pdf_vector_importer.bl_text_builder")

SHEET_M = 0.887  # the 1011 sheet width, for scale comparisons


def test_carrier_display_size_is_glyph_scaled_not_default_metre(btb):
    quad = ((0.0, 0.0), (0.004, 0.0), (0.004, 0.006), (0.0, 0.006))
    size = btb._carrier_display_size(quad)
    assert size < 0.01, "a carrier may never approach Blender's 1.0 m default"
    assert size < SHEET_M / 100.0, "must be negligible against the sheet"
    assert size > 0.0


def test_larger_glyphs_get_proportionally_larger_carriers(btb):
    small = btb._carrier_display_size(((0.0, 0.0), (0.002, 0.0), (0.002, 0.002), (0.0, 0.002)))
    large = btb._carrier_display_size(((0.0, 0.0), (0.008, 0.0), (0.008, 0.008), (0.0, 0.008)))
    assert large > small


def test_size_is_clamped_so_a_huge_quad_cannot_restore_the_starburst(btb):
    huge = ((0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0))
    assert btb._carrier_display_size(huge) <= 0.01


def test_degenerate_or_missing_quads_fall_back_to_a_tiny_size(btb):
    for bad in (None, (), ((0.0, 0.0),), ((0.0, 0.0), (0.0, 0.0)), "nonsense"):
        size = btb._carrier_display_size(bad)
        assert 0.0 < size <= 0.01, f"{bad!r} produced {size}"


def test_the_creation_site_sets_both_display_properties(btb):
    import inspect
    src = inspect.getsource(btb)
    assert "carrier.empty_display_size = _carrier_display_size(target_quad)" in src
    assert 'carrier.empty_display_type = "PLAIN_AXES"' in src
