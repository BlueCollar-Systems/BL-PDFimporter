# -*- coding: utf-8 -*-
"""TEXTMODE-1 invariant lock (Blender leg, fix-list item 13).

For every text-mode x forced-failure combination the import report must
satisfy:

    (delivered mode == requested mode)
    OR (fallback.text records requested/delivered/reason)

— NEVER neither (owner directive 2026-07-13). The scenarios below drive the
production ``build_text`` path with a fake bpy whose meshify step is forced
to succeed or fail, then assert the invariant on the report produced by the
real ``write_import_report()``. Behavior locks, not source-string locks
(RB-11). Glyphs and Geometry are one peer family in every host (identical
rendering engine) — the audit's FINAL ladders treat them as a single rung.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for _path in (str(ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_text_mode_fallback_blender import (  # noqa: E402
    _FailingMeshifyBpy,
    _FakeCollection,
    _write_active_report,
)

from pdf_vector_importer import bl_text_builder  # noqa: E402
from pdf_vector_importer.pdfcadcore.primitives import NormalizedText  # noqa: E402

_TEXT_MODES = ("labels", "3d_text", "glyphs", "geometry")
_PEER_FAMILY = {"glyphs", "geometry"}
#: Which DELIVERED entity buckets honor each requested mode (peer family
#: shares the outline bucket, so glyphs<->geometry is a no-op, not a fallback).
_MODE_BUCKETS = {
    "labels": {"native_label"},
    "3d_text": {"native_3d_text"},
    "glyphs": {"outline_curve_or_mesh"},
    "geometry": {"outline_curve_or_mesh"},
}
_ALL_BUCKETS = (
    "native_label",
    "native_3d_text",
    "outline_curve_or_mesh",
    "raw_geometry_edges",
    "dxf_text",
    "fallback_geometry",
)


def assert_textmode1_invariant(report: dict) -> None:
    """(delivered == requested) OR fallback.text{requested,delivered,reason}."""
    extra = report.get("extra") or {}
    requested = str(extra.get("text_mode") or "").strip().lower()
    entity = extra.get("actual_text_entity_types") or {}
    count = int(entity.get("count") or 0)
    if not requested or requested == "none" or count <= 0:
        return  # no text requested or none delivered — nothing to pin here

    requested_buckets = _MODE_BUCKETS.get(requested, set())
    off_bucket_delivered = sum(
        int(entity.get(bucket) or 0)
        for bucket in _ALL_BUCKETS
        if bucket not in requested_buckets
    )
    if requested in _MODE_BUCKETS and off_bucket_delivered == 0:
        return  # requested mode == delivered mode

    fallback = report.get("fallback") or {}
    text_block = fallback.get("text")
    assert isinstance(text_block, dict), (
        f"TEXTMODE-1 violated: requested={requested!r} but delivered entity "
        f"buckets diverge ({entity!r}) with no fallback.text block — "
        "silent substitution"
    )
    for key in ("requested", "delivered", "reason"):
        assert str(text_block.get(key) or "").strip(), (
            f"fallback.text missing {key!r}: {text_block!r}"
        )
    assert fallback.get("used") is True
    signals = list(((extra.get("diagnostics") or {}).get("signals")) or [])
    assert "text_mode_fallback" in signals


class _FakeMesh:
    def __init__(self, name: str = "mesh"):
        self.name = name
        self.materials = []


class _WorkingMeshes:
    """Meshify succeeds — glyphs/geometry deliver the requested outline mesh."""

    def new_from_object(self, evaluated, depsgraph=None):
        del depsgraph
        return _FakeMesh(f"{getattr(evaluated, 'name', 'text')}_meshdata")


class _FlakyMeshes:
    """Meshify fails only on the configured call numbers (1-based)."""

    def __init__(self, fail_on_calls):
        self.calls = 0
        self.fail_on_calls = set(fail_on_calls)

    def new_from_object(self, evaluated, depsgraph=None):
        del depsgraph
        self.calls += 1
        if self.calls in self.fail_on_calls:
            raise RuntimeError("mesh conversion unavailable for this span")
        return _FakeMesh(f"{getattr(evaluated, 'name', 'text')}_meshdata")


def _install_builder(monkeypatch: pytest.MonkeyPatch, meshes) -> _FakeCollection:
    fake_bpy = _FailingMeshifyBpy()
    fake_bpy.data.meshes = meshes
    monkeypatch.setattr(bl_text_builder, "bpy", fake_bpy)
    monkeypatch.setattr(bl_text_builder, "_get_preferred_font", lambda: None)
    monkeypatch.setattr(
        bl_text_builder,
        "_get_or_create_text_material",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            diffuse_color=(0.1, 0.1, 0.1, 1.0),
        ),
    )
    return _FakeCollection()


def _span(span_id: int) -> NormalizedText:
    return NormalizedText(
        id=span_id,
        text=f"SPAN {span_id}",
        normalized=f"SPAN {span_id}",
        insertion=(10.0 * span_id, 20.0),
        bbox=(10.0 * span_id, 20.0, 10.0 * span_id + 30.0, 26.0),
        font_size=6.0,
        rotation=0.0,
    )


def _build_spans(collection, mode: str, opts, span_count: int = 2) -> int:
    delivered = 0
    for span_id in range(1, span_count + 1):
        obj = bl_text_builder.build_text(
            _span(span_id),
            collection,
            text_mode=mode,
            provenance_opts=opts,
        )
        if obj is not None:
            delivered += 1
    return delivered


# ── Every mode × forced meshify success/failure ─────────────────────────
@pytest.mark.parametrize("meshify_works", [True, False])
@pytest.mark.parametrize("mode", _TEXT_MODES)
def test_every_mode_by_forced_failure_satisfies_invariant(
    monkeypatch, tmp_path, mode, meshify_works
):
    meshes = _WorkingMeshes() if meshify_works else _FailingMeshifyBpy().data.meshes
    collection = _install_builder(monkeypatch, meshes)
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    delivered = _build_spans(collection, mode, opts)
    # "There will be an option" — a failed rung never drops a span.
    assert delivered == 2

    report = _write_active_report(
        tmp_path, requested_mode=mode, opts=opts, text_count=delivered
    )
    assert_textmode1_invariant(report)

    if mode in _PEER_FAMILY and not meshify_works:
        assert report["fallback"]["text"] == {
            "requested": mode,
            "delivered": "labels",
            "reason": "meshify_failed",
            "count": 2,
        }
    else:
        # Requested mode delivered — no fallback block may appear.
        assert "text" not in report["fallback"]


# ── Unknown mode strings × forced meshify success/failure ───────────────
@pytest.mark.parametrize("meshify_works", [True, False])
def test_unknown_mode_by_forced_failure_satisfies_invariant(
    monkeypatch, tmp_path, meshify_works
):
    meshes = _WorkingMeshes() if meshify_works else _FailingMeshifyBpy().data.meshes
    collection = _install_builder(monkeypatch, meshes)
    opts = types.SimpleNamespace(import_mode="vector", text_mode="bogus_mode")

    delivered = _build_spans(collection, "bogus_mode", opts)
    assert delivered == 2

    report = _write_active_report(
        tmp_path, requested_mode="bogus_mode", opts=opts, text_count=delivered
    )
    assert_textmode1_invariant(report)
    text_block = report["fallback"]["text"]
    assert text_block["requested"] == "bogus_mode"
    assert text_block["delivered"] == "3d_text"
    assert text_block["reason"] == "unknown_text_mode_normalized"
    assert report["extra"]["text_mode_normalized_from"] == ["bogus_mode"]


# ── Mixed per-span failure inside one import ────────────────────────────
@pytest.mark.parametrize("mode", sorted(_PEER_FAMILY))
def test_mixed_span_failure_reports_partial_fallback(monkeypatch, tmp_path, mode):
    collection = _install_builder(monkeypatch, _FlakyMeshes(fail_on_calls={2}))
    opts = types.SimpleNamespace(import_mode="vector", text_mode=mode)

    delivered = _build_spans(collection, mode, opts)
    assert delivered == 2

    report = _write_active_report(
        tmp_path, requested_mode=mode, opts=opts, text_count=delivered
    )
    assert_textmode1_invariant(report)
    assert report["fallback"]["text"] == {
        "requested": mode,
        "delivered": "labels",
        "reason": "meshify_failed",
        "count": 1,
    }
    entity = report["extra"]["actual_text_entity_types"]
    assert entity["outline_curve_or_mesh"] == 1
    assert entity["native_label"] == 1


# ── The invariant checker itself refuses silent substitution ────────────
def test_invariant_checker_rejects_silent_substitution(monkeypatch, tmp_path):
    collection = _install_builder(
        monkeypatch, _FailingMeshifyBpy().data.meshes
    )
    opts = types.SimpleNamespace(import_mode="vector", text_mode="glyphs")
    delivered = _build_spans(collection, "glyphs", opts)

    report = _write_active_report(
        tmp_path, requested_mode="glyphs", opts=opts, text_count=delivered
    )
    # The production report is honest...
    assert_textmode1_invariant(report)
    # ...but a hand-built silent report must FAIL the invariant.
    silent = json.loads(json.dumps(report))
    silent["fallback"] = {"used": False, "reason": None}
    with pytest.raises(AssertionError):
        assert_textmode1_invariant(silent)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
