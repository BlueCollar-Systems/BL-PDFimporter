# -*- coding: utf-8 -*-
"""Gate 0 REMEDIATE: circle_fit must preserve reviewed arc-promotion decisions."""
from __future__ import annotations

from types import SimpleNamespace

from pdf_vector_importer.pdfcadcore.geometry_cleanup import (
    _closed_enough,
    _dedupe_closing_point,
    _midpoint_matches_minor_sweep,
    _polyline_run_is_smooth,
    _unwrap_angles,
    circle_fit,
    promote_circular_primitives,
)


# Frozen seed-81011 case-143 points (platform-independent fixture).
_CASE_143_POINTS = [
    (2206.848252742037, 3189.6013272728987),
    (2206.5796925569744, 3191.676843794064),
    (2205.956246648224, 3193.6681682703215),
    (2204.9910289110585, 3195.526618300578),
    (2203.5655152047825, 3197.0591021980495),
    (2201.920784698106, 3198.3402716714468),
    (2200.069649324245, 3199.320775324661),
    (2198.024451024457, 3199.77749933745),
    (2195.9351257813723, 3199.9278495960243),
    (2193.868301218645, 3199.5727572044752),
    (2191.894491857497, 3198.862108704559),
    (2190.143462743056, 3197.7077208931914),
    (2188.656404648362, 3196.2382013234424),
    (2187.42294981038, 3194.5444312780614),
    (2186.6148034591565, 3192.60569175252),
]


def test_circle_fit_seed_81011_case_143_promotes_reviewed_arc():
    points = list(_CASE_143_POINTS)
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
    fit_pts = _dedupe_closing_point(points)
    fit = circle_fit(fit_pts)
    assert fit is not None, "circle_fit returned None for reviewed case 143"
    cx, cy, radius, rms = fit
    tol = max(0.05, radius * 0.005)
    max_err = max(abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - radius) for x, y in fit_pts)
    angles = [__import__("math").degrees(__import__("math").atan2(y - cy, x - cx)) for x, y in points]
    unwrapped = _unwrap_angles(angles)
    span = abs(unwrapped[-1] - unwrapped[0]) if unwrapped else None
    stats = promote_circular_primitives(
        [primitive],
        arc_fit_tol_mm=0.05,
        min_arc_angle_deg=5.0,
        max_arc_segments=64,
    )
    if stats != {"arcs": 1, "circles": 0}:
        raise AssertionError(
            "promotion failed: stats=%r fit=%r rms=%r tol=%r max_err=%r span=%r "
            "closed=%r smooth=%r mid=%r len_fit=%r"
            % (
                stats,
                fit,
                rms,
                tol,
                max_err,
                span,
                _closed_enough(points, radius),
                _polyline_run_is_smooth(fit_pts),
                _midpoint_matches_minor_sweep(fit_pts, cx, cy),
                len(fit_pts),
            )
        )
    assert primitive.type == "arc"
    assert primitive.generic_tags == ["arc_fit"]
    assert primitive.center == (2196.483897339929, 3189.614339443953)
    assert primitive.radius == 10.308404119477688
    assert (primitive.start_angle, primitive.end_angle) == (
        359.92806671875985,
        163.13778455352616,
    )
