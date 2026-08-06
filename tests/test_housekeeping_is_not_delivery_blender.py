"""Housekeeping failures must not discard a delivered import.

`_terminal_import_failures` decides whether `import_pdf` raises
`IncompleteImportError` and throws the whole import away. Its docstring says it
returns "mandatory delivery failures", but two of its five checks are not
delivery at all:

  * `import_report_error`  -- writing the report file failed
  * `temp_cleanup_error`   -- deleting our scratch directory failed

Both are evaluated *after* every page has been delivered, raster floor included,
so no rung of the representation ladder can rescue them. A transient antivirus
lock on the temp directory therefore destroys a completed import -- and the
`finally` block immediately retries the same `shutil.rmtree` and swallows the
error, so the cleanup that "failed" has usually already succeeded by the time
the user sees the failure.

The add-on's own operator already treats both as non-fatal: operators.py reports
"PDF delivery completed, but owned temporary files could not be cleaned" and
then falls through to the success summary. That branch is dead code today,
because the exception is raised before `stats` ever reaches it. This aligns the
engine with the author's stated intent.

A third path reaches the same place: `runtime_package_identity()` raises
`BuildIdentityError` when the installed tree does not hash to its release
manifest, that escapes `write_import_report`, and it lands in
`import_report_error`. Because the add-on can modify its own tree (pip
installing PyMuPDF adds `dist-info/RECORD` and `lib/bin/**`, neither of which
the runtime hasher skips although the packager strips both), an install can
brick itself permanently -- every later import fails on a stale hash.

Both directions are covered: genuine delivery failures must still fail closed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace()
if not hasattr(sys.modules["bpy"], "app"):
    sys.modules["bpy"].app = types.SimpleNamespace(version=(4, 1, 0))
if not hasattr(sys.modules["bpy"], "types"):
    sys.modules["bpy"].types = types.SimpleNamespace(Collection=object, Object=object)
if "bmesh" not in sys.modules:
    sys.modules["bmesh"] = types.SimpleNamespace()

from pdf_vector_importer import bl_import_engine  # noqa: E402
from pdf_vector_importer import build_identity  # noqa: E402


class _Provenance:
    """Minimal stand-in carrying the per-item text delivery ledger."""

    def __init__(self, records=None):
        self._text_delivery_records = list(records or [])


def _delivered(item_id="p1:s0", representation="3d_text"):
    return {
        "item_id": item_id,
        "requested_representation": representation,
        "final_representation": representation,
        "status": "delivered",
    }


def _config(**overrides):
    config = {"text_mode": "3d_text", "import_text": True}
    config.update(overrides)
    return config


def _failures(stats, *, config=None, records=None):
    return bl_import_engine._terminal_import_failures(
        config if config is not None else _config(),
        stats,
        _Provenance(records if records is not None else [_delivered()]),
    )


# --- housekeeping must NOT be terminal ---------------------------------------


def test_temp_cleanup_failure_is_not_a_delivery_failure():
    stats = {"text_source_spans": 1, "temp_cleanup_error": "PermissionError: locked"}
    assert _failures(stats) == [], (
        "a scratch directory we could not delete does not un-deliver content "
        "that is already in the scene"
    )


def test_import_report_failure_is_not_a_delivery_failure():
    stats = {"text_source_spans": 1, "import_report_error": "OSError: disk full"}
    assert _failures(stats) == [], (
        "failing to write the report does not un-deliver the geometry the "
        "report would have described"
    )


def test_both_housekeeping_errors_together_are_still_not_terminal():
    stats = {
        "text_source_spans": 1,
        "import_report_error": "OSError: disk full",
        "temp_cleanup_error": "PermissionError: locked",
    }
    assert _failures(stats) == []


# --- real delivery failures MUST still be terminal ---------------------------


def test_text_delivery_failure_is_still_terminal():
    stats = {"text_source_spans": 2}
    failures = _failures(stats, records=[_delivered()])
    assert failures, "one span required but none recorded must still fail closed"
    assert any("text delivery failed" in f for f in failures)


def test_raster_delivery_failure_is_still_terminal():
    stats = {"text_source_spans": 1, "raster_delivery_failures": [{"page": 1}]}
    failures = _failures(stats)
    assert any("raster delivery failed" in f for f in failures), (
        "raster is the terminal rung; if it fails there is no floor left"
    )


def test_geometry_delivery_failure_is_still_terminal():
    stats = {
        "text_source_spans": 1,
        "geometry_delivery_issues": [{"status": "unverified"}],
    }
    failures = _failures(stats)
    assert any("geometry delivery failed" in f for f in failures)


def test_housekeeping_error_does_not_mask_a_real_delivery_failure():
    stats = {
        "text_source_spans": 1,
        "temp_cleanup_error": "PermissionError: locked",
        "raster_delivery_failures": [{"page": 1}],
    }
    failures = _failures(stats)
    assert any("raster delivery failed" in f for f in failures), (
        "downgrading housekeeping must not also downgrade genuine failures"
    )


def test_cancelled_import_reports_no_failures():
    assert _failures({"cancelled": True, "raster_delivery_failures": [{}]}) == []


# --- a modified install must degrade, not brick ------------------------------


def test_modified_install_is_reported_not_raised(tmp_path, monkeypatch):
    """A tree that no longer matches its release manifest must still import.

    Today `runtime_package_identity()` raises BuildIdentityError, which escapes
    write_import_report, becomes `import_report_error`, and is terminal -- so a
    single stray file makes every subsequent import fail forever.
    """

    def _boom():
        raise build_identity.BuildIdentityError(
            "installed package content hash does not match release identity"
        )

    monkeypatch.setattr(bl_import_engine, "runtime_package_identity", _boom)
    identity = bl_import_engine.safe_runtime_package_identity()
    assert isinstance(identity, dict)
    assert identity.get("status") == "modified_install", (
        "a modified tree is a reportable state, not an import-destroying error"
    )
    assert identity.get("error"), "the reason must be recorded for diagnosis"


def test_intact_install_still_reports_verified(monkeypatch):
    monkeypatch.setattr(
        bl_import_engine,
        "runtime_package_identity",
        lambda: {"status": "verified", "package_sha256": "a" * 64},
    )
    identity = bl_import_engine.safe_runtime_package_identity()
    assert identity.get("status") == "verified"
    assert "error" not in identity


# --- the packager and the runtime hasher must agree on what counts -----------


# --- a resume checkpoint is a convenience, not a deliverable -----------------


def test_checkpoint_write_failure_does_not_stop_the_page_loop(monkeypatch):
    """Checkpointing runs after every page, inside the loop.

    An unguarded OSError there discarded every page already imported, to
    protect a file whose only purpose is making a *future* run resumable.
    """
    stats = {}

    def _explode(path, state):
        raise PermissionError("checkpoint locked")

    monkeypatch.setattr(bl_import_engine, "write_resume_checkpoint", _explode, raising=False)

    ok = bl_import_engine._write_resume_checkpoint_guarded(
        "C:/tmp/checkpoint.json", {"pages": [1]}, stats
    )

    assert ok is False
    assert stats.get("resume_unavailable"), (
        "losing resume support must be recorded so the user is told why "
        "Resume Interrupted Import will not work"
    )
    assert "checkpoint locked" in str(stats["resume_unavailable"])


def test_successful_checkpoint_records_nothing(monkeypatch):
    stats = {}
    monkeypatch.setattr(
        bl_import_engine, "write_resume_checkpoint", lambda p, s: None, raising=False
    )

    ok = bl_import_engine._write_resume_checkpoint_guarded(
        "C:/tmp/checkpoint.json", {"pages": [1]}, stats
    )

    assert ok is True
    assert "resume_unavailable" not in stats


def test_checkpoint_failure_is_not_a_terminal_failure():
    stats = {
        "text_source_spans": 1,
        "resume_unavailable": "PermissionError: checkpoint locked",
    }
    assert _failures(stats) == []


@pytest.mark.parametrize(
    "relative",
    [
        "lib/bin/pymupdf.exe",
        "pymupdf-1.24.0.dist-info/RECORD",
    ],
)
def test_runtime_hasher_skips_whatever_the_packager_strips(tmp_path, relative):
    """Asymmetry here is what lets an install brick itself.

    build_release._should_exclude drops these from the published ZIP, so they
    were never part of the released identity. If the runtime hasher counts them
    when the add-on's own PyMuPDF installer later creates them, the hash can
    never match again.
    """
    package_root = tmp_path / "pdf_vector_importer"
    (package_root / Path(relative).parent).mkdir(parents=True, exist_ok=True)
    (package_root / relative).write_bytes(b"payload")
    (package_root / "__init__.py").write_text("# addon\n", encoding="utf-8")

    entries = build_identity._package_entries(package_root)
    counted = [name for name, _ in entries]
    assert not any(name.endswith(Path(relative).name) for name in counted), (
        f"{relative} is excluded from the release ZIP but counted at runtime; "
        "the two exclusion rules must come from one definition"
    )
    assert any(name.endswith("__init__.py") for name in counted), (
        "real package content must still be hashed"
    )
