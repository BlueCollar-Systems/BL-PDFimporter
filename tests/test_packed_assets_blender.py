from __future__ import annotations

from hashlib import sha256
import types

import pytest

from pdf_vector_importer.packed_assets import (
    PackedAssetError,
    pack_and_verify_bytes,
    verify_packed_sha256,
)


class _Packable:
    def __init__(self, source: bytes, *, packed: bytes | None = None):
        self.source = source
        self.pack_calls = 0
        self.packed_file = (
            types.SimpleNamespace(data=packed)
            if packed is not None
            else None
        )

    def pack(self):
        self.pack_calls += 1
        self.packed_file = types.SimpleNamespace(data=self.source)


def test_pack_and_verify_bytes_requires_exact_embedded_payload():
    payload = b"durable-owned-asset"
    block = _Packable(payload)

    actual = pack_and_verify_bytes(block, payload)

    assert actual == sha256(payload).hexdigest()
    assert block.pack_calls == 1
    assert verify_packed_sha256(block, actual) == actual


def test_existing_wrong_packed_payload_fails_closed_without_repacking():
    block = _Packable(b"expected", packed=b"wrong")

    with pytest.raises(PackedAssetError, match="packed asset hash mismatch"):
        pack_and_verify_bytes(block, b"expected")

    assert block.pack_calls == 0


def test_missing_pack_capability_is_not_verified():
    block = types.SimpleNamespace(packed_file=None)

    with pytest.raises(PackedAssetError, match="pack capability unavailable"):
        pack_and_verify_bytes(block, b"expected")
