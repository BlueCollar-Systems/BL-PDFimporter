from __future__ import annotations

import json
import os
from pathlib import Path
from zipfile import ZipFile

import build_release


def test_release_excludes_environment_bound_runtime_metadata(tmp_path: Path) -> None:
    package = tmp_path / "pdf_vector_importer"

    assert build_release._should_exclude(package / "lib" / "bin" / "pymupdf.exe")
    assert build_release._should_exclude(
        package / "lib" / "pymupdf-1.27.2.3.dist-info" / "RECORD"
    )
    assert not build_release._should_exclude(
        package / "lib" / "pymupdf-1.27.2.3.dist-info" / "METADATA"
    )


def test_release_zip_is_identical_after_source_mtimes_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    package = root / "pdf_vector_importer"
    dist = root / "dist"
    runtime = package / "lib" / "pymupdf-1.27.2.3.dist-info"

    runtime.mkdir(parents=True)
    (package / "lib" / "bin").mkdir(parents=True)
    (package / "__init__.py").write_text(
        'bl_info = {"version": (9, 8, 7)}\n', encoding="utf-8"
    )
    module = package / "module.py"
    module.write_text("VALUE = 42\n", encoding="utf-8")
    (package / "bl_import_engine.py").write_text("ENGINE = 1\n", encoding="utf-8")
    (package / "bl_text_builder.py").write_text("TEXT = 1\n", encoding="utf-8")
    (package / "lib" / "bin" / "pymupdf.exe").write_bytes(b"local launcher")
    (runtime / "RECORD").write_text("environment-bound paths\n", encoding="utf-8")
    (runtime / "METADATA").write_text("Name: PyMuPDF\n", encoding="utf-8")
    for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "ROOT", root)
    monkeypatch.setattr(build_release, "PKG", package)
    monkeypatch.setattr(build_release, "DIST", dist)
    monkeypatch.setattr(build_release, "LIB_DIR", package / "lib")
    monkeypatch.setattr(build_release, "_VENDORED_LIB", package / "lib")
    monkeypatch.setattr(build_release, "_verify_vendored_pymupdf", lambda: None)
    monkeypatch.setattr(build_release, "_prune_vendored_pymupdf", lambda: None)
    monkeypatch.setenv("BC_RELEASE_SOURCE_SHA", "a" * 40)

    os.utime(module, (1_700_000_000, 1_700_000_000))
    assert build_release.main() == 0
    archive = dist / "Blender-PDF-Importer_v9.8.7.zip"
    first_build = archive.read_bytes()

    os.utime(module, (1_700_086_400, 1_700_086_400))
    assert build_release.main() == 0
    assert archive.read_bytes() == first_build

    with ZipFile(archive) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert "pdf_vector_importer/lib/bin/pymupdf.exe" not in names
        assert "pdf_vector_importer/lib/pymupdf-1.27.2.3.dist-info/RECORD" not in names
        assert "pdf_vector_importer/lib/pymupdf-1.27.2.3.dist-info/METADATA" in names
        assert {info.date_time for info in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert {info.create_system for info in zf.infolist()} == {3}
        assert {info.external_attr for info in zf.infolist()} == {0o100644 << 16}


def test_release_embeds_source_tag_content_and_module_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    package = root / "pdf_vector_importer"
    dist = root / "dist"
    runtime = package / "lib" / "pymupdf-1.27.2.3.dist-info"
    runtime.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'bl_info = {"version": (9, 8, 7)}\n', encoding="utf-8"
    )
    (package / "bl_import_engine.py").write_text("ENGINE = 1\n", encoding="utf-8")
    (package / "bl_text_builder.py").write_text("TEXT = 1\n", encoding="utf-8")
    (runtime / "METADATA").write_text("Name: PyMuPDF\n", encoding="utf-8")
    for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "ROOT", root)
    monkeypatch.setattr(build_release, "PKG", package)
    monkeypatch.setattr(build_release, "DIST", dist)
    monkeypatch.setattr(build_release, "LIB_DIR", package / "lib")
    monkeypatch.setattr(build_release, "_VENDORED_LIB", package / "lib")
    monkeypatch.setattr(build_release, "_verify_vendored_pymupdf", lambda: None)
    monkeypatch.setattr(build_release, "_prune_vendored_pymupdf", lambda: None)
    monkeypatch.setenv("BC_RELEASE_SOURCE_SHA", "a" * 40)

    assert build_release.main() == 0
    archive = dist / "Blender-PDF-Importer_v9.8.7.zip"
    with ZipFile(archive) as zf:
        identity = json.loads(
            zf.read("pdf_vector_importer/_release_identity.json").decode("utf-8")
        )

    assert identity["importer_version"] == "9.8.7"
    assert identity["source_tag"] == "v9.8.7"
    assert identity["source_commit"] == "a" * 40
    assert len(identity["package_sha256"]) == 64
    assert identity["package_hash_kind"] == "installed_content_manifest_sha256"
    assert set(identity["modules"]) == {
        "pdf_vector_importer.bl_import_engine",
        "pdf_vector_importer.bl_text_builder",
    }
