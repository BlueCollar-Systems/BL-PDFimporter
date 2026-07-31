from __future__ import annotations

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
