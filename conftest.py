"""Pytest bootstrap: repo root on sys.path; cache under %TEMP% when needed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
root_s = str(ROOT)
if root_s not in sys.path:
    sys.path.insert(0, root_s)

# Avoid permission errors on a read-only or locked .pytest_cache in the repo.
if os.environ.get("PYTEST_CACHE_DIR") is None:
    cache = Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "pytest-cache-bl-pdfimporter"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_CACHE_DIR"] = str(cache)


# --- offline guarantee: fail any test that opens a socket -------------------
# An independent review demonstrated the gap this closes: a live
# subprocess.check_call inserted into ensure_pymupdf_runtime left the whole
# suite green (315 passed), because the two tests named "...stays_offline..."
# monkeypatch the very function whose offline-ness they assert. A per-test
# forbid-list cannot catch that; an autouse guard can.
#
# Sockets are banned outright. subprocess is NOT banned here because several
# suites legitimately spawn sys.executable (release-zip smoke, CLI checks);
# the pip call this product must never make is pinned separately by the AST
# allowlist test over install_pymupdf's call sites.
import socket as _socket

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _forbid_network(request, monkeypatch):
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(*_a, **_k):
        raise AssertionError(
            "network access attempted during a test: the importer must work on a "
            "clean offline machine. Mark the test with @pytest.mark.allow_network "
            "only if the network use is genuinely intended."
        )

    monkeypatch.setattr(_socket.socket, "connect", _blocked, raising=False)
    monkeypatch.setattr(_socket.socket, "connect_ex", _blocked, raising=False)
    monkeypatch.setattr(_socket, "create_connection", _blocked, raising=False)
