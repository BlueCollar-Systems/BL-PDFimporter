"""Canonical visually-empty assets shared by Blender delivery and verification."""
from __future__ import annotations

import base64
from hashlib import sha256


BLENDER_SAFE_TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4//8/"
    "AwAI/AL+p5qgoAAAAABJRU5ErkJggg=="
)
BLENDER_SAFE_TRANSPARENT_PNG_SHA256 = sha256(
    BLENDER_SAFE_TRANSPARENT_PNG
).hexdigest()


__all__ = [
    "BLENDER_SAFE_TRANSPARENT_PNG",
    "BLENDER_SAFE_TRANSPARENT_PNG_SHA256",
]
