# T-01 Field Report — Blender + FreeCAD (owner screenshots, 2026-07-03 evening)

**Source:** Owner in-host testing. Files: `1017 - Rev 0.pdf` (shop drawing) + USGS Alvord TX 7.5-min topo (large color vector map).
**Hosts:** Blender 5.1.2 (BL importer), FreeCAD 1.1.1 (FC importer v4.0.55-line). Complements the SU QUAN-rotation report earlier today (already in fix cycle).

---

## BL-1 — Lineweights render fat ("sausage" strokes) — P1, root cause located

**Symptom:** 1017 renders with heavy tube-like lines; drawing reads bolder than the PDF at every zoom.
**Root cause (verified in code):**
- `pdf_vector_importer/importer.py:161` — `primitive.line_width *= factor` scales the *paper-space* stroke width by the geometry import scale factor.
- `pdf_vector_importer/bl_geometry_builder.py:177` — `curve_data.bevel_depth = max(line_width * _LINEWIDTH_SCALE, _MIN_BEVEL_DEPTH)`.
- Net: on a 3/4"=1'-0" drawing (factor 16), a 0.18 mm hairline becomes ~2.9 mm of real 3D bevel. Lineweight is a paper-space concept; it must not scale with model-space geometry.
**Fix direction:** stop multiplying `line_width` by the geometry factor (keep paper mm); honor the existing `--lineweight-mode` (cli.py:17) in the GUI operator with default `paper`; document `hairline` mode (bevel 0 + viewport wire) for CAD-style display. Verify in-host with before/after screenshots per rule 6.

## BL-2 — Large files load slow — P1, architectural

**Symptom:** big/dense PDFs (topo-class) take a long time to appear.
**Likely dominant cost:** one Blender object per curve batch (outliner shows `P1_arc_1204`, `P1_arc_1210`, …) — thousands of objects stress the dependency graph at creation AND on every viewport update afterward.
**Fix direction (accuracy-neutral):** merge curves into far fewer objects — one curve object per (page × color × width) bucket (collections already group by `Color_r_g_b`); use batched `foreach_set` mesh/curve building instead of per-primitive ops. Pairs with the R3-10 timing instrumentation to prove the win.

## FC-1 — Dense dimension cluster alignment (same class as SU) — P1

**Symptom (1017):** stacked-fraction dimension strings crowd/overlap in dense clusters: `3 13/16` reads as `313 16`-style collisions, `13'-5 3/8` splits, `1 9/16 / 3 7/16 / 8 1/2` row under SECTION F-F crowds. BOM table text itself is correct/horizontal.
**Context:** the 2026-07-02 core fix solved footprint *width*; remaining issue is FC ShapeString **inter-span spacing/kerning** when several dimension spans sit close: adjacent spans render at reduced size but their insertion gaps still assume full-size advance (or vice versa).
**Fix direction:** in FC text placement, when a merged reduced-footprint fraction follows/precedes a whole-number span on the same baseline within ~1 glyph, recompute the gap from the *reduced* bbox, not the original span advance. Lock with a corpus vector (extend `stacked_fraction_spacing.pdf` with a mixed `3 13/16` + `13'-5 3/8` cluster) before changing placement. In-host verify.

## FC-2 — Fill color lost (topo woodland green → grey) — P1, root cause located

**Symptom:** USGS topo imports with orange contours OK (stroke color) but area fills (woodland green tint, urban grey) come in as default grey faces.
**Root cause (verified in code):** `PDFImporterCore.py` sets only `vo.LineColor = stroke_rgb` (lines 878, 3435). **`ShapeColor` (face/fill color) is never assigned** — every filled face gets FreeCAD's default. The extractor already carries fill color (LC/BL render it).
**Fix direction:** apply `obj.ViewObject.ShapeColor = fill_rgb` (and `Transparency` if alpha) wherever filled paths become Faces; keep `LineColor` for edges. Small, testable via import_report color counts + in-host screenshot.

---

**Round-trip note:** SU QUAN rotation (morning report) is mid-fix by another session (320pt window + red test). These four are unclaimed — suggested order: FC-2 (small, located), BL-1 (located), FC-1 (needs corpus vector first), BL-2 (needs profiling data).
