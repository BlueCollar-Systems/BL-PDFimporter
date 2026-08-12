# -*- coding: utf-8 -*-
"""Gate 0 REMEDIATE: circle_fit must preserve reviewed arc-promotion decisions."""
from __future__ import annotations

import base64
import struct
from types import SimpleNamespace

from pdf_vector_importer.pdfcadcore.geometry_cleanup import promote_circular_primitives

# Bit-exact seed-81011 case-143 points (little-endian float64 pairs).
_CASE_143_POINTS_B64 = (
    "lfMuTrI9oUCWFivhM+uoQOF7ds0oPaFAAxBFi1rvqEAQIimZ6TuhQIPKJhpW86hAxjQkaPs5oUDq9OmgDfeoQN17NYshN6FAfa+kQh76qEAMihdx1zOhQD2pFjiu/KhApIMTqSMwoUCR0ak8pP6oQMs82IQMLKFABaZkFI7/qEBIcc7I3iehQBAtGg/b/6hAUDL6kbwjoUCKq25AJf+oQPo01vrJH6FAW+dPZrn9qEA92/JzSRyhQOiVZFpq+6hAUCNFFFAZoUAZHIb1efioQN2m4IzYFqFAY0yyvxb1qEAX3YTHOhWhQBC5Oh028ahA"
)


def _case_143_points():
    raw = base64.b64decode(_CASE_143_POINTS_B64)
    return [
        struct.unpack_from("<dd", raw, offset) 
        for offset in range(0, len(raw), 16)
    ]


def test_circle_fit_seed_81011_case_143_promotes_reviewed_arc():
    points = _case_143_points()
    assert len(points) == 15
    primitive = SimpleNamespace(
        type="polyline",
        points=list(points),
        center=None,
        radius=None,
        start_angle=None,
        end_angle=None,
        closed=False,
        generic_tags=[],
    )
    stats = promote_circular_primitives(
        [primitive],
        arc_fit_tol_mm=0.05,
        min_arc_angle_deg=5.0,
        max_arc_segments=64,
    )
    assert stats == {"arcs": 1, "circles": 0}, stats
    assert primitive.type == "arc"
    assert primitive.generic_tags == ["arc_fit"]
    assert primitive.center == (2196.483897339929, 3189.614339443953)
    assert primitive.radius == 10.308404119477688
    assert (primitive.start_angle, primitive.end_angle) == (
        359.92806671875985,
        163.13778455352616,
    )
