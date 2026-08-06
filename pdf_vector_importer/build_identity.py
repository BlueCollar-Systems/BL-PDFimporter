"""Deterministic distributable and runtime package identity."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence, Tuple


IDENTITY_FILENAME = "_release_identity.json"
IDENTITY_SCHEMA = "bcs.blender.package_identity/1.0"
_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class BuildIdentityError(RuntimeError):
    """Raised when distributable identity is absent, malformed, or mismatched."""


# THE single definition of what is not part of the distributed package.
#
# build_release.py strips these from the published ZIP, and the runtime identity
# hasher below must skip exactly the same things. When the two lists disagreed,
# anything the packager dropped but the hasher counted could never hash equal --
# and because the add-on installs PyMuPDF into its own directory (creating
# lib/bin/** and *.dist-info/RECORD, both stripped from the ZIP), an install
# could permanently brick itself: every later import failed on a stale hash.
# Keep both callers pointed at this function; do not re-inline either list.
_EXCLUDE_DIR_NAMES = frozenset({"__pycache__", "tests", ".pytest_cache", "_archived"})
_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})


def is_excluded_package_member(relative_path: str | Path) -> bool:
    """True if this path is stripped from the release and must not be hashed.

    Accepts an absolute or package-relative path; every rule is an "any part"
    test, so both forms agree for paths inside the package.
    """
    path = Path(str(relative_path).replace("\\", "/"))
    parts = path.parts
    if any(part in _EXCLUDE_DIR_NAMES for part in parts):
        return True
    folded = tuple(part.casefold() for part in parts)
    # Vendored native binaries are re-created by pip, never shipped.
    if any(folded[index : index + 2] == ("lib", "bin")
           for index in range(len(folded) - 1)):
        return True
    # pip rewrites RECORD on every (re)install, so its bytes are not stable.
    if path.name.casefold() == "record" and any(
        part.endswith(".dist-info") for part in folded
    ):
        return True
    if path.suffix in _EXCLUDE_SUFFIXES:
        return True
    return False


def content_manifest_sha256(entries: Iterable[Tuple[str, bytes]]) -> str:
    """Hash sorted package paths and per-file bytes without self-reference."""
    digest = hashlib.sha256()
    for name, data in sorted(entries, key=lambda item: item[0]):
        normalized = str(name).replace("\\", "/")
        if normalized.endswith("/" + IDENTITY_FILENAME):
            continue
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(bytes(data)).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def create_release_identity(
    *,
    importer_version: str,
    source_commit: str,
    source_tag: str,
    package_entries: Sequence[Tuple[str, bytes]],
) -> dict[str, Any]:
    commit = str(source_commit or "").strip().lower()
    if not _HEX40.fullmatch(commit):
        raise BuildIdentityError("release source commit must be a full 40-character SHA")
    entries = list(package_entries)
    by_name = {str(name).replace("\\", "/"): bytes(data) for name, data in entries}
    modules = {}
    for module, path in (
        ("pdf_vector_importer.bl_import_engine", "pdf_vector_importer/bl_import_engine.py"),
        ("pdf_vector_importer.bl_text_builder", "pdf_vector_importer/bl_text_builder.py"),
    ):
        data = by_name.get(path)
        if data is None:
            raise BuildIdentityError(f"required identity module is missing: {path}")
        modules[module] = {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return {
        "schema": IDENTITY_SCHEMA,
        "status": "verified",
        "importer_version": str(importer_version),
        "source_commit": commit,
        "source_tag": str(source_tag),
        "package_sha256": content_manifest_sha256(entries),
        "package_hash_kind": "installed_content_manifest_sha256",
        "modules": modules,
    }


def _package_entries(package_root: Path) -> list[tuple[str, bytes]]:
    parent = package_root.parent
    entries = []
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root)
        # IDENTITY_FILENAME is in the ZIP but cannot hash itself; everything
        # else defers to the shared packaging rule so the two can never drift.
        if path.name == IDENTITY_FILENAME or is_excluded_package_member(relative):
            continue
        entries.append((path.relative_to(parent).as_posix(), path.read_bytes()))
    return entries


def _development_source_commit(package_root: Path) -> str:
    for key in ("BC_RELEASE_SOURCE_SHA", "GITHUB_SHA"):
        value = str(os.environ.get(key) or "").strip()
        if _HEX40.fullmatch(value):
            return value.lower()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(package_root.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        value = proc.stdout.strip()
        if _HEX40.fullmatch(value):
            return value.lower()
    except (OSError, subprocess.CalledProcessError):
        pass
    return "0" * 40


def runtime_package_identity(package_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(package_root) if package_root is not None else Path(__file__).resolve().parent
    manifest_path = root / IDENTITY_FILENAME
    entries = _package_entries(root)
    if manifest_path.is_file():
        identity = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        init_text = (root / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'"version"\s*:\s*\((\d+),\s*(\d+),\s*(\d+)\)', init_text)
        version = ".".join(match.groups()) if match else "0.0.0"
        identity = create_release_identity(
            importer_version=version,
            source_commit=_development_source_commit(root),
            source_tag=f"v{version}",
            package_entries=entries,
        )
        identity["status"] = "computed_unmanifested_tree"

    if identity.get("schema") != IDENTITY_SCHEMA:
        raise BuildIdentityError("unsupported package identity schema")
    for key, pattern in (("source_commit", _HEX40), ("package_sha256", _HEX64)):
        if not pattern.fullmatch(str(identity.get(key) or "")):
            raise BuildIdentityError(f"invalid package identity field: {key}")
    actual_package = content_manifest_sha256(entries)
    if actual_package != str(identity["package_sha256"]).lower():
        raise BuildIdentityError("installed package content hash does not match release identity")
    for module in identity.get("modules", {}).values():
        rel = Path(str(module.get("path") or ""))
        path = root.parent / rel
        if not path.is_file():
            raise BuildIdentityError(f"identity module is missing: {rel.as_posix()}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(module.get("sha256") or "").lower():
            raise BuildIdentityError(f"identity module hash mismatch: {rel.as_posix()}")
        module["path"] = str(path.resolve())
    return identity


__all__ = [
    "BuildIdentityError",
    "IDENTITY_FILENAME",
    "content_manifest_sha256",
    "create_release_identity",
    "runtime_package_identity",
]
