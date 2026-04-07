# -*- coding: utf-8 -*-
# primitive_extractor.py — PyMuPDF -> normalized Primitives
# BlueCollar Systems — BUILT. NOT BOUGHT.
"""
THE SEAM: converts PyMuPDF page data into host-neutral Primitives.
Rule 1: Parser modules must not know about domain-specific logic.
"""
from __future__ import annotations
import math
import re
from typing import List, Optional, Tuple

from .primitives import (
    Primitive, NormalizedText, PageData, next_id
)

MM_PER_PT = 25.4 / 72.0
_VALID_DIM_DENOMS = (64, 32, 16, 8, 4, 2)


def _xy(obj) -> Tuple[float, float]:
    if hasattr(obj, "x") and hasattr(obj, "y"):
        return float(obj.x), float(obj.y)
    if isinstance(obj, (tuple, list)) and len(obj) >= 2:
        return float(obj[0]), float(obj[1])
    return 0.0, 0.0


def _norm_color(col) -> Optional[Tuple[float, float, float]]:
    if col is None:
        return None
    try:
        if isinstance(col, (int, float)):
            g = max(0.0, min(1.0, float(col)))
            return (g, g, g)
        vals = [max(0.0, min(1.0, float(c))) for c in col]
        if len(vals) >= 4:
            c, m, y, k = vals[0], vals[1], vals[2], vals[3]
            r = (1.0 - c) * (1.0 - k)
            g = (1.0 - m) * (1.0 - k)
            b = (1.0 - y) * (1.0 - k)
            return (
                max(0.0, min(1.0, r)),
                max(0.0, min(1.0, g)),
                max(0.0, min(1.0, b)),
            )
        while len(vals) < 3:
            vals.append(vals[-1] if vals else 0.0)
        return (vals[0], vals[1], vals[2])
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_dashes(raw, scale: float = 1.0) -> list | None:
    """Parse PyMuPDF dash patterns into a numeric list.

    PyMuPDF returns dashes as strings like ``'[ 6 6 ] 0'`` (array + phase)
    or as actual lists/tuples.  Returns ``None`` for solid lines.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s.startswith("[]") or s == "() 0":
            return None
        # Extract numbers between brackets: "[ 6 6 ] 0" -> [6.0, 6.0]
        bracket = s.find("[")
        bracket_end = s.find("]")
        if bracket >= 0 and bracket_end > bracket:
            inner = s[bracket + 1:bracket_end].strip()
            if not inner:
                return None
            try:
                nums = [float(x) for x in inner.split()]
                vals = [n * MM_PER_PT * scale for n in nums if n > 0]
                return vals if vals else None
            except ValueError:
                return None
        return None
    if isinstance(raw, (list, tuple)):
        if not raw:
            return None
        # Could be ([6,6], 0) tuple or flat [6,6]
        if len(raw) == 2 and isinstance(raw[0], (list, tuple)):
            vals = [float(x) * MM_PER_PT * scale for x in raw[0] if float(x) > 0]
            return vals if vals else None
        try:
            nums = [float(x) for x in raw]
            vals = [n * MM_PER_PT * scale for n in nums if n > 0]
            return vals if vals else None
        except (TypeError, ValueError):
            return None
    return None


def _expand_compact_fraction_digits(
    digits: str,
    prefer_inches: bool = True,
    had_slash: bool = False,
) -> str | None:
    """
    Expand compact fraction tail strings like:
      12    -> 1/2
      34    -> 3/4
      5316  -> 5 3/16
      1012  -> 10 1/2
      91516 -> 9 15/16
      278   -> 2 7/8
    Returns None when no reliable split is found.
    """
    s = re.sub(r"\D", "", (digits or ""))
    if len(s) < 2:
        return None

    candidates: list[tuple[int, str]] = []
    for den in _VALID_DIM_DENOMS:
        # Without an explicit trailing slash, tiny denominators are too
        # ambiguous and can produce bad rewrites (e.g., 6112 -> 61 1/2).
        if not had_slash and den in (2, 4):
            continue
        den_s = str(den)
        if not s.endswith(den_s):
            continue
        rem = s[:-len(den_s)]
        if len(rem) < 1:
            continue

        # Candidate A: pure fraction (e.g., 1516 -> 15/16).
        try:
            frac_num = int(rem)
        except ValueError:
            frac_num = -1
        if 0 < frac_num < den:
            g = math.gcd(frac_num, den)
            frac_num_r = frac_num // g
            den_r = den // g
            score = 2 if prefer_inches else 8
            candidates.append((score, f"{frac_num_r}/{den_r}"))

        if len(rem) < 2:
            continue
        for num_len in (1, 2):
            if len(rem) <= num_len:
                continue
            inch_s = rem[:-num_len]
            num_s = rem[-num_len:]
            if not inch_s:
                continue
            try:
                inches = int(inch_s)
                numerator = int(num_s)
            except ValueError:
                continue
            if numerator <= 0 or numerator >= den:
                continue

            g = math.gcd(numerator, den)
            numerator_r = numerator // g
            den_r = den // g

            # Prefer practical shop-drawing inches and compact numerator.
            score = 4 if prefer_inches else 3
            if 0 <= inches <= 24:
                score += 6
            if num_len == 1:
                score += 2
            if len(inch_s) >= 2:
                score += 1
            # Two-digit remainder with a one-digit split (e.g. 1516) is
            # inherently ambiguous between "15/16" and "1 5/16".
            if len(rem) == 2 and num_len == 1:
                score -= 5

            candidates.append((score, f"{inches} {numerator_r}/{den_r}"))

    if not candidates:
        return None
    best_score = max(score for score, _ in candidates)
    near_best = sorted({txt for score, txt in candidates if score >= (best_score - 1)})
    if len(near_best) != 1:
        # Ambiguous parse; keep original text unchanged.
        return None
    return near_best[0]


def _canonicalize_text_symbols(text: str) -> str:
    """
    Normalize common Unicode punctuation variants to ASCII CAD-style symbols.
    Keeps semantic content intact and is safe for strict-fidelity mode.
    """
    t = (text or "")
    return (
        t.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("´", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("″", '"')
        .replace("‶", '"')
        .replace("Ų", '"')
        .replace("\u2044", "/")
        .replace("\u2212", "-")
        .replace("\u00A0", " ")
        .replace("—", "-")
        .replace("–", "-")
    )


def _normalize_numeric_token_ocr_noise(text: str) -> str:
    """
    Clean OCR confusions only inside numeric/dimension-like tokens.
    This avoids rewriting plain words while stabilizing dimension strings.
    """
    if not text:
        return text

    # OCR often maps trailing size digits in callouts to letters:
    # PIPEI-I/2 -> PIPE1-1/2. Limit this to hyphen+fraction contexts.
    text = re.sub(r"(?<=[A-Za-z])[Il](?=\s*-\s*[0-9Il|]+\s*/)", "1", text)
    text = re.sub(r"(?<=[A-Za-z])[Oo](?=\s*-\s*[0-9Oo0]+\s*/)", "0", text)
    text = re.sub(r"(?:(?<=\A)|(?<=\s))[Il](?=\s*')", "1", text)
    text = re.sub(r"(-\s*)[Il](?=\s*/\s*[0-9Il|])", r"\g<1>1", text)

    token_re = re.compile(r"(?<![A-Za-z])[\dIl|Oo/'\".\-\s]+(?![A-Za-z])")

    def _fix_token(match):
        tok = match.group(0)
        if not re.search(r"\d", tok):
            return tok
        if not re.search(r"[/'\"\-]", tok):
            return tok

        fixed = (
            tok.replace("|", "1")
            .replace("¦", "1")
            .replace("‖", "1")
            .replace("Ⅰ", "1")
            .replace("ⅼ", "1")
            .replace("Ｉ", "1")
        )
        # Leading OCR-I in a numeric run: "I5/16" -> "15/16", "I3'-3" -> "13'-3"
        fixed = re.sub(r"(?:(?<=\s)|\A)[Il](?=\d)", "1", fixed)
        # Common OCR confusions around dimension punctuation.
        fixed = re.sub(r"(?<=[0-9/'\"\-])[Il](?=[0-9/'\"\-])", "1", fixed)
        # Fraction-local fixes around slash boundaries.
        fixed = re.sub(r"(?<=\d)\s*[Il](?=\s*/)", "1", fixed)
        fixed = re.sub(r"(?<=/)\s*[Il](?=\s*\d)", "1", fixed)
        # O/0 swaps also show up in scanned dimensions.
        fixed = re.sub(r"(?:(?<=\s)|\A)[Oo](?=\d)", "0", fixed)
        fixed = re.sub(r"(?<=[0-9/'\"\-])[Oo](?=[0-9/'\"\-])", "0", fixed)
        fixed = re.sub(r"(?<=[0-9/'\"\-])[Oo](?=\s*\d)", "0", fixed)
        # Collapse repeated slash artifacts in fractions: 15//16 -> 15/16
        fixed = re.sub(r"/{2,}", "/", fixed)
        # Remove space between slash and denominator when split by OCR.
        fixed = re.sub(r"/\s+(\d)", r"/\1", fixed)
        return fixed

    return token_re.sub(_fix_token, text)


def _normalize_dimension_text(text: str, aggressive: bool = True) -> str:
    """
    Normalize common compact/garbled dimension tokens into readable forms.
    This is intentionally conservative and only rewrites dimension-like patterns.
    """
    original = (text or "").strip()
    if not original:
        return original

    # Guardrail: only apply dimension OCR cleanup when the token carries
    # dimension-like signals. This prevents rewriting general prose labels.
    has_digit = bool(re.search(r"\d", original))
    has_dim_punct = bool(re.search(r"[/'\"′″\-xX\u2044\u2212]", original))
    if not (has_digit and has_dim_punct):
        return original

    t = _canonicalize_text_symbols(original)
    t = _normalize_numeric_token_ocr_noise(t)
    if not t:
        return t

    # Strip OCR-leading dot/bullet artifacts before feet-inch dimensions.
    t = re.sub(r"^[\s\.\u00B7\u2022\u2024\u22C5]+(?=\d+\s*')", "", t)

    # Rewrite compact feet-inch fractions:
    #   4'-5316/  -> 4'-5 3/16
    #   13'-338/  -> 13'-3 3/8
    # Also handles common no-slash compact forms when unambiguous:
    #   1'-5116   -> 1'-5 1/16
    def _feet_repl(m):
        feet = m.group(1)
        compact = m.group(2)
        had_slash = bool(m.group(3))
        expanded = _expand_compact_fraction_digits(
            compact,
            prefer_inches=True,
            had_slash=had_slash,
        )
        return f"{feet}'-{expanded}" if expanded else m.group(0)

    t = re.sub(
        r"(\d+)\s*'\s*-\s*([0-9]{3,6})(/)?(?=(?:\D|$))",
        _feet_repl,
        t,
    )

    # Normalize split feet-inch compact fractions:
    #   13'-3 38/ -> 13'-3 3/8
    #   2'-9 14/  -> 2'-9 1/4
    #   4'-7 116  -> 4'-7 1/16
    def _compact_fraction_only(compact: str, had_slash: bool) -> str | None:
        expanded = _expand_compact_fraction_digits(
            compact,
            prefer_inches=False,
            had_slash=had_slash,
        )
        if not expanded:
            return None
        if " " in expanded:
            return expanded.split(" ", 1)[1]
        return expanded

    def _feet_split_slash_repl(m):
        feet = m.group(1)
        inches = m.group(2)
        compact = m.group(3)
        frac = _compact_fraction_only(compact, had_slash=True)
        if not frac:
            return m.group(0)
        return f"{feet}'-{inches} {frac}"

    t = re.sub(
        r"(\d+)\s*'\s*-\s*(\d+)\s+([0-9]{2,4})/(?=(?:\D|$))",
        _feet_split_slash_repl,
        t,
    )

    def _feet_split_no_slash_repl(m):
        feet = m.group(1)
        inches = m.group(2)
        compact = m.group(3)
        frac = _compact_fraction_only(compact, had_slash=False)
        if not frac:
            return m.group(0)
        return f"{feet}'-{inches} {frac}"

    t = re.sub(
        r"(\d+)\s*'\s*-\s*(\d+)\s+([0-9]{3,4})(?=(?:\D|$))",
        _feet_split_no_slash_repl,
        t,
    )

    # Clean stray trailing slash after already-valid fractions:
    #   3/8/ -> 3/8
    t = re.sub(
        r"(\d+\s*/\s*\d+)\s*/(?=(?:\D|$))",
        r"\1",
        t,
    )

    # Convert compact slash tokens that appear as standalone words:
    #   38/ -> 3/8
    #   1516/ -> 15/16
    # Keep strict guards to avoid touching dates/IDs.
    def _compact_slash_token_repl(m):
        compact = m.group(1)
        expanded = _expand_compact_fraction_digits(
            compact,
            prefer_inches=False,
            had_slash=True,
        )
        return expanded if expanded else m.group(0)

    t = re.sub(
        r"(?<![\d/])([0-9]{2,4})/(?!\d)",
        _compact_slash_token_repl,
        t,
    )

    # Restore readable spacing around parenthesized notes after normalized dims.
    # Example: 6'-9 15/16(PIPE...) -> 6'-9 15/16 (PIPE...)
    t = re.sub(r"((?:\d['\"]|\d+/\d+|\d+\s+\d+/\d+))\(", r"\1 (", t)

    # Rewrite compact mixed-number tails used in nominal sizes/callouts:
    #   1-12/ -> 1-1/2
    #   1-1516/ -> 1-15/16
    # Keep this slash-gated to avoid touching part numbers like A-101.
    def _mixed_tail_repl(m):
        whole = m.group(1)
        compact = m.group(2)
        expanded = _expand_compact_fraction_digits(
            compact,
            prefer_inches=False,
            had_slash=True,
        )
        if not expanded:
            return m.group(0)
        if " " in expanded:
            # compact tail already contains an inches component; strip it to keep
            # the whole number on the left side of the hyphen authoritative.
            expanded = expanded.split(" ", 1)[1]
        return f"{whole}-{expanded}"

    t = re.sub(
        r"(?<!\d)(\d+)\s*-\s*([0-9]{2,6})/(?!\d)",
        _mixed_tail_repl,
        t,
    )

    if not aggressive:
        return t.strip()

    t = re.sub(r"\s+", " ", t).strip()

    # Rewrite standalone compact fraction tokens only when the entire string
    # is that token and a trailing slash is present (avoids corrupting
    # labels/dates like 10/28/2024 or IDs like 1516).
    m = re.fullmatch(r"([0-9]{2,6})(/)", t)
    if m:
        expanded = _expand_compact_fraction_digits(
            m.group(1),
            prefer_inches=False,
            had_slash=True,
        )
        if expanded:
            t = expanded

    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def extract_page(
    page,
    page_num: int,
    scale: float = 1.0,
    flip_y: bool = True,
    strict_text_fidelity: bool = True,
) -> PageData:
    """Extract normalized primitives from a PyMuPDF page."""
    page_h = page.rect.height
    page_w_mm = page.rect.width * MM_PER_PT * scale
    page_h_mm = page.rect.height * MM_PER_PT * scale

    primitives = []
    drawings = page.get_drawings()

    for path_group in drawings:
        items = path_group.get("items", [])
        if not items:
            continue

        stroke = _norm_color(path_group.get("color") or path_group.get("stroke"))
        fill = _norm_color(path_group.get("fill"))
        width_raw = path_group.get("width")
        try:
            width = float(width_raw) * MM_PER_PT * scale if width_raw is not None else None
        except (TypeError, ValueError):
            width = None
        dashes = _parse_dashes(path_group.get("dashes"), scale=scale)
        close_path = path_group.get("closePath", False)
        layer_name = path_group.get("oc") or path_group.get("layer")

        current_pts: List[Tuple[float, float]] = []
        sub_paths: List[Tuple[List[Tuple[float, float]], bool]] = []

        def flush(closed: bool, _sub_paths=sub_paths):
            nonlocal current_pts
            if len(current_pts) >= 2:
                _sub_paths.append((current_pts[:], closed))
            current_pts = []

        for item in items:
            kind = item[0]
            data = item[1:]

            if kind == "m":
                flush(False)
                x, y = _parse_point(data)
                px, py = _to_mm(x, y, page_h, flip_y, scale)
                current_pts = [(px, py)]

            elif kind == "l":
                if len(data) >= 2 and hasattr(data[0], "x") and hasattr(data[1], "x"):
                    x0, y0 = _xy(data[0])
                    x1, y1 = _xy(data[1])
                    p0 = _to_mm(x0, y0, page_h, flip_y, scale)
                    p1 = _to_mm(x1, y1, page_h, flip_y, scale)
                    if not current_pts:
                        current_pts.append(p0)
                    current_pts.append(p1)
                else:
                    x, y = _parse_point(data)
                    current_pts.append(_to_mm(x, y, page_h, flip_y, scale))

            elif kind == "c":
                if len(data) == 4 and all(hasattr(d, "x") for d in data):
                    pts = [_xy(d) for d in data]
                else:
                    pts = _parse_cubic(data)
                p0 = _to_mm(pts[0][0], pts[0][1], page_h, flip_y, scale)
                p1 = _to_mm(pts[1][0], pts[1][1], page_h, flip_y, scale)
                p2 = _to_mm(pts[2][0], pts[2][1], page_h, flip_y, scale)
                p3 = _to_mm(pts[3][0] if len(pts) > 3 else pts[2][0],
                            pts[3][1] if len(pts) > 3 else pts[2][1],
                            page_h, flip_y, scale)
                if not current_pts:
                    current_pts.append(p0)
                N = max(4, min(32, int(math.ceil(_dist(p0, p3) / 0.5))))
                for i in range(1, N + 1):
                    t = i / float(N)
                    q = _bezier_pt(p0, p1, p2, p3, t)
                    current_pts.append(q)

            elif kind == "re":
                flush(False)
                x, y, w, h = _parse_rect(data)
                c1 = _to_mm(x, y, page_h, flip_y, scale)
                c2 = _to_mm(x + w, y, page_h, flip_y, scale)
                c3 = _to_mm(x + w, y + h, page_h, flip_y, scale)
                c4 = _to_mm(x, y + h, page_h, flip_y, scale)
                sub_paths.append(([c1, c2, c3, c4, c1], True))

            elif kind == "h":
                flush(True)

            elif kind == "v":
                if len(data) >= 2:
                    cx, cy = _xy(data[0])
                    ex, ey = _xy(data[1])
                    ctrl = _to_mm(cx, cy, page_h, flip_y, scale)
                    end = _to_mm(ex, ey, page_h, flip_y, scale)
                    if current_pts:
                        p0 = current_pts[-1]
                        cp1 = (p0[0] + 2/3*(ctrl[0]-p0[0]), p0[1] + 2/3*(ctrl[1]-p0[1]))
                        cp2 = (end[0] + 2/3*(ctrl[0]-end[0]), end[1] + 2/3*(ctrl[1]-end[1]))
                        N = 8
                        for i in range(1, N + 1):
                            t = i / float(N)
                            current_pts.append(_bezier_pt(p0, cp1, cp2, end, t))

        flush(close_path)

        for pts, is_closed in sub_paths:
            if len(pts) < 2:
                continue
            cleaned = [pts[0]]
            for p in pts[1:]:
                if _dist(p, cleaned[-1]) > 0.01:
                    cleaned.append(p)
            if len(cleaned) < 2:
                continue

            xs = [p[0] for p in cleaned]
            ys = [p[1] for p in cleaned]
            bbox = (min(xs), min(ys), max(xs), max(ys))

            area = None
            if is_closed and len(cleaned) >= 3:
                area = _polygon_area(cleaned)

            ptype = "line" if len(cleaned) == 2 else ("closed_loop" if is_closed else "polyline")

            primitives.append(Primitive(
                id=next_id(), type=ptype, points=cleaned,
                bbox=bbox, stroke_color=stroke, fill_color=fill,
                dash_pattern=dashes, line_width=width,
                layer_name=layer_name, closed=is_closed,
                area=area, page_number=page_num
            ))

    text_items = _extract_text(
        page,
        page_h,
        page_num,
        flip_y,
        scale,
        strict_text_fidelity=strict_text_fidelity,
    )

    return PageData(
        page_number=page_num,
        width=page_w_mm, height=page_h_mm,
        primitives=primitives, text_items=text_items,
        layers=[], xobject_names=[]
    )


def _extract_text(
    page,
    page_h,
    page_num,
    flip_y,
    scale,
    strict_text_fidelity: bool = True,
) -> List[NormalizedText]:
    items = []
    try:
        tdict = page.get_text("dict")
    except (RuntimeError, TypeError, ValueError):
        return items

    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            raw_text = "".join(s.get("text", "") for s in spans)
            if not raw_text or not raw_text.strip():
                continue

            # Preserve intentional leading/trailing spacing from PDF text runs.
            # Some drawings split one visual sentence into multiple runs on the
            # same baseline and rely on a leading space in the next run.
            lead = " " if raw_text[:1].isspace() else ""
            tail = " " if raw_text[-1:].isspace() else ""

            core_text = raw_text.strip()
            if strict_text_fidelity:
                # Preserve source text structure, but still normalize proven
                # OCR-style compact dimension tokens (e.g. 3'-1012/ -> 3'-10 1/2).
                core_text = _canonicalize_text_symbols(core_text)
                core_text = _normalize_numeric_token_ocr_noise(core_text)
                core_text = _normalize_dimension_text(
                    core_text,
                    aggressive=False,
                )
            else:
                core_text = _normalize_dimension_text(
                    core_text,
                    aggressive=True,
                )
            text = f"{lead}{core_text}{tail}"
            if not text.strip():
                continue

            bbox_mm = None
            try:
                lb = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                if lb and len(lb) >= 4:
                    bx0, by0, bx1, by1 = float(lb[0]), float(lb[1]), float(lb[2]), float(lb[3])
                    p0 = _to_mm(bx0, by0, page_h, flip_y, scale)
                    p1 = _to_mm(bx1, by1, page_h, flip_y, scale)
                    bbox_mm = (
                        min(p0[0], p1[0]),
                        min(p0[1], p1[1]),
                        max(p0[0], p1[0]),
                        max(p0[1], p1[1]),
                    )
            except (TypeError, ValueError):
                bbox_mm = None

            # Use the first non-empty span as baseline source to reduce
            # placement drift from empty/whitespace spans.
            base_span = next(
                (s for s in spans if str(s.get("text", "")).strip()),
                spans[0],
            )

            origin = base_span.get("origin")
            if origin:
                x, y = float(origin[0]), float(origin[1])
            else:
                bb = line.get("bbox", (0, 0, 0, 0))
                # bbox y0 is top; y1 is closer to baseline/descender.
                x, y = float(bb[0]), float(bb[3])

            px, py = _to_mm(x, y, page_h, flip_y, scale)
            size = max(float(base_span.get("size", 3)), 1.0) * MM_PER_PT * scale
            font = str(base_span.get("font", ""))

            text_dir = line.get("dir", (1.0, 0.0))
            dx = float(text_dir[0]) if text_dir else 1.0
            dy = float(text_dir[1]) if text_dir else 0.0
            # Snap tiny floating jitter to axis to improve text/line alignment.
            if abs(dx) < 1e-7:
                dx = 0.0
            if abs(dy) < 1e-7:
                dy = 0.0
            angle = -math.degrees(math.atan2(dy, dx))

            normalized = text.upper().replace("  ", " ").strip()
            generic_tags = _classify_generic(text)

            items.append(NormalizedText(
                id=next_id(), text=text, normalized=normalized,
                insertion=(px, py), font_size=size,
                bbox=bbox_mm,
                rotation=angle, font_name=font,
                page_number=page_num, generic_tags=generic_tags
            ))
    return _suppress_redundant_slash_items(
        items,
        enabled=not strict_text_fidelity,
    )


def _rotation_diff_mod_180(a_deg: float, b_deg: float) -> float:
    """Smallest angular difference treating 180-deg flips as equivalent."""
    diff = abs((float(a_deg) - float(b_deg)) % 180.0)
    return min(diff, 180.0 - diff)


def _suppress_redundant_slash_items(
    items: List[NormalizedText],
    enabled: bool = True,
) -> List[NormalizedText]:
    """
    Remove slash-only text runs that duplicate an already complete nearby
    fraction token. This avoids visual artifacts like "15/6" from stacked
    duplicate slash glyphs while preserving legitimate text.
    """
    if not items or not enabled:
        return items

    def _is_slash_only(s: str) -> bool:
        t = (s or "").strip()
        return bool(t) and all(ch in {"/", "\u2044"} for ch in t)

    def _try_expand_compact_fraction_token(token: str) -> str | None:
        t = (token or "").strip()
        if not re.fullmatch(r"\d{2,6}", t):
            return None
        return _expand_compact_fraction_digits(
            t,
            prefer_inches=False,
            had_slash=True,
        )

    keep = [True] * len(items)

    for i, item in enumerate(items):
        if not _is_slash_only(item.text):
            continue

        sx, sy = item.insertion
        if item.bbox:
            sx = (item.bbox[0] + item.bbox[2]) * 0.5
            sy = (item.bbox[1] + item.bbox[3]) * 0.5

        rot_i = float(item.rotation or 0.0)
        size_i = max(float(item.font_size or 0.0), 0.5)

        for j, other in enumerate(items):
            if i == j:
                continue
            if _is_slash_only(other.text):
                continue
            if _rotation_diff_mod_180(rot_i, float(other.rotation or 0.0)) > 4.0:
                continue

            # Convert compact OCR-style fraction tails when a nearby slash token
            # makes the intent explicit (e.g. "1516" + "/" -> "15/16").
            expanded = _try_expand_compact_fraction_token(other.text or "")
            if expanded:
                other.text = expanded
                other.normalized = expanded.upper().replace("  ", " ").strip()
                if "dimension_like" not in (other.generic_tags or []):
                    other.generic_tags.append("dimension_like")

            # Only suppress when a nearby token already contains a slash.
            if "/" not in (other.text or "") and "\u2044" not in (other.text or ""):
                continue

            ox, oy = other.insertion
            if other.bbox:
                bx0, by0, bx1, by1 = other.bbox
                pad = max(size_i * 0.65, 0.8)
                if (bx0 - pad) <= sx <= (bx1 + pad) and (by0 - pad) <= sy <= (by1 + pad):
                    keep[i] = False
                    break
                ox = (bx0 + bx1) * 0.5
                oy = (by0 + by1) * 0.5

            if math.hypot(sx - ox, sy - oy) <= max(size_i * 1.25, 2.0):
                keep[i] = False
                break

    return [it for idx, it in enumerate(items) if keep[idx]]


def _classify_generic(text: str) -> list:
    tags = []
    t = text.strip()
    tu = t.upper()
    if re.search(r"\d+['']\s*[-\u2013]?\s*\d", t) or re.search(r"\d+\s*/\s*\d+", t):
        tags.append("dimension_like")
    if re.search(r'\d+\.?\d*\s*(?:"|mm|cm|in|ft)', t, re.I):
        tags.append("dimension_like")
    if re.search(r"SCALE[:\s]*\d", tu) or re.search(r"\d+\s*:\s*\d+", t):
        tags.append("scale_like")
    if re.search(r"\b(DRAWN|CHECKED|DATE|SCALE|REV|SHEET|PROJECT|DWG|TITLE)\b", tu):
        tags.append("titleblock_like")
    if re.search(r"\u00D8|\bDIA\b|\bRAD\b|\bR\d", t, re.I):
        tags.append("callout_like")
    if re.search(r"\b(DETAIL|SECTION|SEC|VIEW|ELEVATION)\s+[A-Z]", tu):
        tags.append("detail_reference")
    if len(t) > 1 and len(t) < 60 and re.search(r"[A-Z]{2,}", tu):
        tags.append("label_like")
    return tags


# ── Coordinate helpers ──

def _to_mm(x, y, page_h, flip_y, scale):
    if flip_y:
        y = page_h - y
    return x * MM_PER_PT * scale, y * MM_PER_PT * scale


def _parse_point(data):
    if len(data) >= 1 and hasattr(data[0], "x"):
        return _xy(data[0])
    if len(data) >= 2:
        return float(data[0]), float(data[1])
    return 0.0, 0.0


def _parse_cubic(data):
    if len(data) == 3 and all(hasattr(d, "x") for d in data):
        return [_xy(d) for d in data]
    if len(data) >= 6:
        return [(float(data[0]), float(data[1])),
                (float(data[2]), float(data[3])),
                (float(data[4]), float(data[5]))]
    if len(data) == 4:
        return [_xy(d) for d in data]
    return [(0, 0), (0, 0), (0, 0)]


def _parse_rect(data):
    if len(data) >= 1 and hasattr(data[0], "x0"):
        r = data[0]
        return float(r.x0), float(r.y0), float(r.x1) - float(r.x0), float(r.y1) - float(r.y0)
    if len(data) >= 4:
        return float(data[0]), float(data[1]), float(data[2]), float(data[3])
    return 0.0, 0.0, 0.0, 0.0


def _bezier_pt(p0, p1, p2, p3, t):
    u = 1.0 - t
    return (u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0],
            u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1])


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _polygon_area(pts):
    n = len(pts)
    a = 0.0
    for i in range(n):
        j = (i + 1) % n
        a += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
    return abs(a) / 2.0
