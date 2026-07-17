"""Durable Blender datablock packing with byte-for-byte verification."""
from __future__ import annotations

from hashlib import sha256


class PackedAssetError(RuntimeError):
    """A Blender datablock could not be proven to own its source bytes."""


def _packed_bytes(datablock) -> bytes:
    packed_file = getattr(datablock, "packed_file", None)
    if packed_file is None:
        raise PackedAssetError("packed asset is absent")
    try:
        data = bytes(packed_file.data)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PackedAssetError("packed asset bytes are unreadable") from exc
    if not data:
        raise PackedAssetError("packed asset bytes are empty")
    return data


def verify_packed_sha256(datablock, expected_sha256: str) -> str:
    """Return the digest only when the Blender-owned bytes match exactly."""
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise PackedAssetError("expected packed asset SHA-256 is invalid")
    actual = sha256(_packed_bytes(datablock)).hexdigest()
    if actual != expected:
        raise PackedAssetError(
            f"packed asset hash mismatch: expected {expected}, got {actual}"
        )
    return actual


def pack_and_verify_bytes(datablock, source_bytes: bytes) -> str:
    """Pack *source_bytes* into a Blender datablock and verify exact ownership."""
    payload = bytes(source_bytes or b"")
    if not payload:
        raise PackedAssetError("source asset bytes are empty")
    expected = sha256(payload).hexdigest()
    if getattr(datablock, "packed_file", None) is None:
        pack = getattr(datablock, "pack", None)
        if not callable(pack):
            raise PackedAssetError("pack capability unavailable")
        try:
            pack()
        except Exception as exc:
            raise PackedAssetError(
                f"pack operation failed: {type(exc).__name__}: {exc}"
            ) from exc
    return verify_packed_sha256(datablock, expected)


__all__ = ["PackedAssetError", "pack_and_verify_bytes", "verify_packed_sha256"]
