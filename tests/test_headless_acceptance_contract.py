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
        "schema": "bcs.blender.package_identity/1.0",
        "status": "verified",
        "repo_root": "C:/synthetic/repo",
        "importer_version": "1.0.74",
        "source_commit": "c" * 40,
        "source_tag": "v1.0.74",
        "package_sha256": "d" * 64,
        "package_hash_kind": "installed_content_manifest_sha256",
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


def _synthetic_page_number_contract():
    return {
        "kind": "synthetic_page_number_contract",
        "actual_page_extraction": False,
        "source_fixture_page_number": 1,
        "requested_pages": "2,4",
        "synthetic_page_numbers": [2, 4],
        "source_items": 2,
        "delivered_items": 2,
        "failed_items": 0,
        "pages": {
            "2": {"source_items": 1, "delivered_items": 1, "failed_items": 0},
            "4": {"source_items": 1, "delivered_items": 1, "failed_items": 0},
        },
    }


def _metric_verification(**extra):
    return {
        "metric_affine_applied": True,
        "actual_baseline_anchor_m": [0.0, 0.0],
        "expected_location_m": [0.0, 0.0],
        "actual_advance_endpoint_m": [1.0, 0.0],
        "expected_advance_endpoint_m": [1.0, 0.0],
        "actual_line_axis_endpoint_m": [0.0, 1.0],
        "expected_line_axis_endpoint_m": [0.0, 1.0],
        "evaluated_affine_matrix": [1.0],
        "intended_affine_matrix": [1.0],
        **extra,
    }


def _positioned_delivery_record(character_entities):
    return {
        "entity_ids": ["Char_A", "Char_B"],
        "attempts": [
            {
                "status": "delivered",
                "evidence": {"character_entities": character_entities},
            }
        ],
    }


def test_acceptance_args_do_not_echo_missing_local_paths(monkeypatch, tmp_path):
    driver = _load_driver(monkeypatch)
    first = tmp_path / "outlined-text.pdf"
    second = tmp_path / "font-text.pdf"
    monkeypatch.setattr(driver.sys, "argv", ["blender", "--", str(first), str(second)])

    with pytest.raises(SystemExit) as exc_info:
        driver._args()

    message = str(exc_info.value)
    assert "missing acceptance input" in message
    assert tmp_path.as_posix() not in message
    assert first.name not in message
    assert second.name not in message


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


def test_external_global_cad_host_lease_is_required_and_revalidated(
    monkeypatch,
    tmp_path,
):
    driver = _load_driver(monkeypatch)
    with pytest.raises(driver.AcceptanceOwnershipError, match="global CAD-host lease"):
        driver._ExternalCadHostLease.from_environment({})

    lock_path = tmp_path / "CAD-HOST-GLOBAL.lock"
    payload = {
        "schema_version": 1,
        "host": "Blender",
        "agent_id": "acceptance-owner",
        "owner_pid": 101,
        "purpose": "bounded representation acceptance",
        "token": "global-token",
        "started_at": "2026-08-01T11:00:00-05:00",
        "heartbeat_at": "2026-08-01T11:00:00-05:00",
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    binding = driver._ExternalCadHostLease.from_environment(
        {
            "BC_CAD_HOST_GLOBAL_LOCK": str(lock_path),
            "BC_CAD_HOST_LEASE_AGENT_ID": "acceptance-owner",
            "BC_CAD_HOST_LEASE_OWNER_PID": "101",
            "BC_CAD_HOST_LEASE_TOKEN": "global-token",
        }
    )

    assert binding.validate()["token"] == "global-token"
    payload["token"] = "replacement-token"
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(driver.AcceptanceOwnershipError, match="ownership changed"):
        binding.validate()

    payload["token"] = "global-token"
    del payload["schema_version"]
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(driver.AcceptanceOwnershipError, match="schema"):
        binding.validate()


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


def test_character_delivery_accepts_proven_zero_ink_without_invented_host_object(
    monkeypatch,
):
    driver = _load_driver(monkeypatch)
    objects = {
        name: types.SimpleNamespace(name=name, type="FONT")
        for name in ("Char_A", "Char_B")
    }
    driver.bpy.data = types.SimpleNamespace(
        objects=types.SimpleNamespace(get=objects.get)
    )
    monkeypatch.setattr(driver, "_assert_metric_entity", lambda _obj: None)
    record = _positioned_delivery_record(
        [
            {
                "text": "A",
                "positioned_character": True,
                "entity_ids": ["Char_A"],
                "verification": _metric_verification(),
            },
            {
                "text": " ",
                "positioned_character": True,
                "entity_ids": [],
                "verification": {
                    "zero_ink_identity": True,
                    "zero_ink_reason": "source_whitespace",
                    "visible_geometry_omitted": True,
                    "advance_preserved": True,
                },
            },
            {
                "text": "B",
                "positioned_character": True,
                "entity_ids": ["Char_B"],
                "verification": _metric_verification(),
            },
        ]
    )

    delivered = driver._assert_character_delivery(record, "FONT", "A B")

    assert [obj.name for obj in delivered] == ["Char_A", "Char_B"]


def test_character_delivery_rejects_omission_without_zero_ink_proof(monkeypatch):
    driver = _load_driver(monkeypatch)
    objects = {
        name: types.SimpleNamespace(name=name, type="FONT")
        for name in ("Char_A", "Char_B")
    }
    driver.bpy.data = types.SimpleNamespace(
        objects=types.SimpleNamespace(get=objects.get)
    )
    monkeypatch.setattr(driver, "_assert_metric_entity", lambda _obj: None)
    record = _positioned_delivery_record(
        [
            {
                "text": "A",
                "positioned_character": True,
                "entity_ids": ["Char_A"],
                "verification": _metric_verification(),
            },
            {
                "text": "X",
                "positioned_character": True,
                "entity_ids": [],
                "verification": {"visible_geometry_omitted": True},
            },
            {
                "text": "B",
                "positioned_character": True,
                "entity_ids": ["Char_B"],
                "verification": _metric_verification(),
            },
        ]
    )

    with pytest.raises(AssertionError):
        driver._assert_character_delivery(record, "FONT", "AXB")


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


def test_result_gate_emits_exact_counts_and_labels_page_numbers_synthetic_only(
    monkeypatch,
):
    driver = _load_driver(monkeypatch)
    results = {
        "modes": _complete_modes(),
        "package_identity": _package_identity(),
        "synthetic_page_number_contract": _synthetic_page_number_contract(),
    }

    finalized = driver._finalize_results(results)

    assert finalized["per_mode_counts"] == {
        mode: {"source_items": 1, "delivered_items": 1, "failed_items": 0}
        for mode in ("3d_text", "geometry", "glyphs", "labels", "raster", "text")
    }
    invalid = dict(results)
    invalid["synthetic_page_number_contract"] = _synthetic_page_number_contract()
    invalid["synthetic_page_number_contract"]["actual_page_extraction"] = True
    with pytest.raises(driver.AcceptanceResultError, match="synthetic page-number"):
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
    identity = _package_identity()
    identity["modules"] = {
        "pdf_vector_importer.bl_import_engine": identity["modules"][
            "pdf_vector_importer.bl_import_engine"
        ]
    }
    results = {
        "modes": _complete_modes(),
        "package_identity": identity,
    }

    with pytest.raises(driver.AcceptanceResultError, match="critical module"):
        driver._finalize_results(results)


@pytest.mark.parametrize("missing", ["source_commit", "source_tag", "package_sha256"])
def test_result_gate_requires_release_bound_identity(monkeypatch, missing):
    driver = _load_driver(monkeypatch)
    identity = _package_identity()
    identity.pop(missing)
    results = {
        "modes": _complete_modes(),
        "package_identity": identity,
        "synthetic_page_number_contract": _synthetic_page_number_contract(),
    }

    with pytest.raises(driver.AcceptanceResultError, match="release identity"):
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
