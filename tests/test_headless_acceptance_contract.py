from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest


DRIVER_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "headless_text_representation_acceptance.py"
)


def _load_driver(monkeypatch):
    fake_bpy = types.ModuleType("bpy")
    fake_bpy.app = types.SimpleNamespace(version=(5, 2, 0))
    fake_mathutils = types.ModuleType("mathutils")
    fake_mathutils.Vector = lambda values: values
    monkeypatch.setitem(sys.modules, "bpy", fake_bpy)
    monkeypatch.setitem(sys.modules, "mathutils", fake_mathutils)
    spec = importlib.util.spec_from_file_location(
        "bc_test_headless_text_representation_acceptance",
        DRIVER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_modes():
    return {
        mode: {
            "requested_representation": mode,
            "final_representation": "text" if mode == "labels" else mode,
            "status": "delivered",
            "source_items": 1,
            "delivered_items": 1,
            "failed_items": 0,
        }
        for mode in ("labels", "text", "3d_text", "glyphs", "geometry", "raster")
    }


def _package_identity():
    return {
        "repo_root": "C:/synthetic/repo",
        "importer_version": "1.0.74",
        "modules": {
            "pdf_vector_importer.bl_import_engine": {
                "path": "C:/synthetic/repo/pdf_vector_importer/bl_import_engine.py",
                "sha256": "a" * 64,
            },
            "pdf_vector_importer.bl_text_builder": {
                "path": "C:/synthetic/repo/pdf_vector_importer/bl_text_builder.py",
                "sha256": "b" * 64,
            },
        },
    }


def _nonfirst_page_delivery():
    return {
        "requested_pages": "2,4",
        "selected_page_numbers": [2, 4],
        "source_items": 2,
        "delivered_items": 2,
        "failed_items": 0,
        "pages": {
            "2": {"source_items": 1, "delivered_items": 1, "failed_items": 0},
            "4": {"source_items": 1, "delivered_items": 1, "failed_items": 0},
        },
    }


def test_second_owner_is_refused_and_first_owner_lock_is_preserved(
    monkeypatch,
    tmp_path,
):
    driver = _load_driver(monkeypatch)
    lock_path = tmp_path / "blender-acceptance.lock"
    first = driver._AcceptanceLease(
        lock_path,
        owner="first",
        pid=101,
        process_ids=lambda: (101,),
    )
    first.acquire()
    first_payload = json.loads(lock_path.read_text(encoding="utf-8"))

    second = driver._AcceptanceLease(
        lock_path,
        owner="second",
        pid=202,
        process_ids=lambda: (202,),
    )
    with pytest.raises(driver.AcceptanceOwnershipError, match="already owned"):
        second.acquire()

    assert json.loads(lock_path.read_text(encoding="utf-8")) == first_payload
    first.release()
    assert not lock_path.exists()


def test_other_blender_process_refuses_before_creating_lock(monkeypatch, tmp_path):
    driver = _load_driver(monkeypatch)
    lock_path = tmp_path / "blender-acceptance.lock"
    lease = driver._AcceptanceLease(
        lock_path,
        owner="candidate",
        pid=202,
        process_ids=lambda: (101, 202),
    )

    with pytest.raises(driver.AcceptanceOwnershipError, match="other Blender process"):
        lease.acquire()

    assert not lock_path.exists()


def test_release_does_not_remove_lock_replaced_by_another_owner(monkeypatch, tmp_path):
    driver = _load_driver(monkeypatch)
    lock_path = tmp_path / "blender-acceptance.lock"
    lease = driver._AcceptanceLease(
        lock_path,
        owner="first",
        pid=101,
        process_ids=lambda: (101,),
    )
    lease.acquire()
    replacement = {
        "owner": "second",
        "pid": 202,
        "token": "replacement-token",
        "heartbeat_at": "later",
    }
    lock_path.write_text(json.dumps(replacement), encoding="utf-8")

    lease.release()

    assert json.loads(lock_path.read_text(encoding="utf-8")) == replacement


def test_incomplete_acceptance_returns_nonzero(monkeypatch):
    driver = _load_driver(monkeypatch)

    class IncompleteImportError(RuntimeError):
        pass

    def incomplete():
        raise IncompleteImportError("delivery failed")

    assert driver._run_main_fail_closed(incomplete, print_failure=lambda: None) == 1


def test_result_gate_requires_complete_per_mode_metrics_and_identity(monkeypatch):
    driver = _load_driver(monkeypatch)
    results = {
        "modes": _complete_modes(),
        "package_identity": {
            "repo_root": "C:/synthetic/repo",
            "importer_version": "1.0.74",
            "modules": {
                "pdf_vector_importer.bl_import_engine": {
                    "path": "C:/synthetic/repo/pdf_vector_importer/bl_import_engine.py",
                    "sha256": "a" * 64,
                },
                "pdf_vector_importer.bl_text_builder": {
                    "path": "C:/synthetic/repo/pdf_vector_importer/bl_text_builder.py",
                    "sha256": "b" * 64,
                }
            },
        },
    }
    results["modes"]["glyphs"]["failed_items"] = 1
    results["modes"]["glyphs"]["delivered_items"] = 0

    with pytest.raises(driver.AcceptanceResultError, match="glyphs"):
        driver._finalize_results(results)


def test_result_gate_emits_exact_per_mode_counts_and_requires_page_2_plus_delivery(
    monkeypatch,
):
    driver = _load_driver(monkeypatch)
    results = {
        "modes": _complete_modes(),
        "package_identity": _package_identity(),
        "nonfirst_page_delivery": _nonfirst_page_delivery(),
    }

    finalized = driver._finalize_results(results)

    assert finalized["per_mode_counts"] == {
        mode: {"source_items": 1, "delivered_items": 1, "failed_items": 0}
        for mode in ("3d_text", "geometry", "glyphs", "labels", "raster", "text")
    }
    invalid = dict(results)
    invalid["nonfirst_page_delivery"] = _nonfirst_page_delivery()
    invalid["nonfirst_page_delivery"]["pages"]["4"]["delivered_items"] = 0
    with pytest.raises(driver.AcceptanceResultError, match="non-first-page"):
        driver._finalize_results(invalid)


def test_result_gate_rejects_cross_representation_delivery(monkeypatch):
    driver = _load_driver(monkeypatch)
    results = {
        "modes": _complete_modes(),
        "package_identity": {
            "repo_root": "C:/synthetic/repo",
            "importer_version": "1.0.74",
            "modules": {
                "pdf_vector_importer.bl_import_engine": {
                    "path": "C:/synthetic/repo/pdf_vector_importer/bl_import_engine.py",
                    "sha256": "a" * 64,
                },
                "pdf_vector_importer.bl_text_builder": {
                    "path": "C:/synthetic/repo/pdf_vector_importer/bl_text_builder.py",
                    "sha256": "b" * 64,
                },
            },
        },
    }
    results["modes"]["glyphs"]["final_representation"] = "raster"

    with pytest.raises(driver.AcceptanceResultError, match="glyphs"):
        driver._finalize_results(results)


def test_result_gate_requires_both_critical_module_identities(monkeypatch):
    driver = _load_driver(monkeypatch)
    results = {
        "modes": _complete_modes(),
        "package_identity": {
            "repo_root": "C:/synthetic/repo",
            "importer_version": "1.0.74",
            "modules": {
                "pdf_vector_importer.bl_import_engine": {
                    "path": "C:/synthetic/repo/pdf_vector_importer/bl_import_engine.py",
                    "sha256": "a" * 64,
                }
            },
        },
    }

    with pytest.raises(driver.AcceptanceResultError, match="critical module"):
        driver._finalize_results(results)


def test_package_identity_rejects_module_loaded_outside_repo(monkeypatch, tmp_path):
    driver = _load_driver(monkeypatch)
    repo_root = tmp_path / "repo"
    source_root = repo_root / "pdf_vector_importer"
    source_root.mkdir(parents=True)
    inside = source_root / "bl_import_engine.py"
    inside.write_text("VERSION = 1\n", encoding="utf-8")
    outside = tmp_path / "installed" / "bl_text_builder.py"
    outside.parent.mkdir()
    outside.write_text("VERSION = 2\n", encoding="utf-8")

    with pytest.raises(driver.AcceptanceResultError, match="outside expected root"):
        driver._package_identity(
            repo_root,
            importer_version="1.0.74",
            modules={
                "pdf_vector_importer.bl_import_engine": types.SimpleNamespace(
                    __file__=str(inside)
                ),
                "pdf_vector_importer.bl_text_builder": types.SimpleNamespace(
                    __file__=str(outside)
                ),
            },
        )
