# Blender Truthful Text Representation Implementation Plan

> **Required execution discipline:** execute one task at a time with a fresh implementation agent, require a failing regression test before each production edit, commit only the task's authorized files, and run an independent spec/code review before advancing.

**Goal:** Make Blender deliver the requested text representation with exact-font visual geometry, preserve legitimate zero-ink characters without failing visible siblings, and reject false-green host evidence.

**Non-negotiable contract:** A request for Text, Labels, 3D Text, Glyphs, Geometry, or Raster must deliver that representation. Alignment, rotation, scaling, or style defects may not be hidden by changing representation. A fallback is allowed only after the requested rung is affirmatively impossible for the specific item and host, follows the nearest valid rung, leaves no failed-rung artifacts, and is recorded truthfully.

**Architecture:** Keep the existing per-item representation ladder and ownership cleanup. Correct exact-font metric domains, add a source-derived ink-bound oracle to the metric affine verifier, treat whitespace as an explicit verified zero-ink identity during positioned conversion, then widen the real-host acceptance gate from one convenient sample to the whole mixed corpus. Do not use raster comparison to excuse a wrong representation.

**Evidence that this plan addresses:** On Blender 5.2, Welding-Symbol-Chart item 240 renders about 1.504 times the source ink width in Text and 3D Text while the current metric verifier passes it. Glyphs and Geometry abort mixed positioned spans at their first space because an empty spline/vertex set is treated as failure. The current headless gate samples only the first exact-font item for most modes.

---

## Task 1: Verify exact-font ink in the correct metric domain

**Files:**

- Modify: `pdf_vector_importer/bl_text_builder.py`
- Modify: `tests/test_representation_fidelity_blender.py`
- Create or modify only if a focused helper is necessary: `tests/test_blender_font_metric_bounds.py`

### Step 1: Add failing unit regressions

Add focused tests proving all of the following:

1. `_positioned_font_axis_metrics` does not use the ascender-minus-descender line-height domain as the font's horizontal glyph coordinate domain. Horizontal advance and glyph design bounds use units-per-em (or the embedded asset's documented glyph coordinate scale); line height and baseline use the vertical metrics intentionally.
2. `_verify_metric_character_transform` rejects an evaluated glyph whose affine baseline, synthetic advance endpoint, line endpoint, and matrix all match but whose actual evaluated ink bounds are wider than the exact embedded glyph's transformed source bounds. Use the observed 1.5x over-width case as the regression shape.
3. The verifier records independent expected and actual world-space ink bounds and never derives the expected bounds from the evaluated Blender object it is validating.
4. A legitimate zero-ink character remains verifiable without fabricated ink bounds.

Name the central regression `test_positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds`.

Run the focused tests and save the RED output in the task report before touching production code.

### Step 2: Implement the smallest source-derived ink oracle

In `bl_text_builder.py`:

- Derive the selected glyph's exact design-space ink bounds from the same embedded font asset used to create the Blender FONT object. If the parsed asset does not yet expose per-glyph bounds, extend the existing immutable asset metadata locally; do not infer expected ink from `obj.bound_box`, `obj.dimensions`, or the evaluated conversion.
- Keep metric domains explicit: UPEM/design units for horizontal glyph coordinates and advances; ascent/descent for baseline and line-height semantics.
- Store only the minimal source-bound metadata needed to verify after Blender evaluation.
- Transform the exact design-space ink bounds through the intended metric affine into a world-space expected ink quad/bounds.
- Independently obtain evaluated Blender ink bounds and compare using a small documented tessellation tolerance. A visible overscale must fail with a stable visual-verification reason and must prevent requested-rung delivery.
- Preserve zero-ink identity as an explicit exception: there is no expected ink, but baseline/advance/line/matrix still must verify.

Do not solve the defect by switching the item to another representation or by broadening the tolerance.

### Step 3: Verify and commit

Run the focused tests, the complete `tests/test_representation_fidelity_blender.py`, relevant contract/cleanup tests, and static compilation. Commit only Task 1 files with `[skip release]`.

---

## Task 2: Preserve zero-ink spaces in positioned Glyphs and Geometry

**Files:**

- Modify: `pdf_vector_importer/bl_text_builder.py`
- Modify: `tests/test_representation_fidelity_blender.py`

### Step 1: Add failing mixed-span regressions

Add regressions for a positioned `"A B"` span in requested Glyphs and requested Geometry. Prove:

- `A` and `B` deliver visible CURVE/MESH children in the requested representation.
- The space is retained as an explicit verified zero-ink identity with its item/character identity, advance, source layout, requested rung, and delivered rung recorded.
- No empty curve/mesh is manufactured for the space.
- The zero-ink source FONT and temporary datablock are cleaned.
- Attempt coverage includes all three characters while physical entity counts count only the two visible entities (or a separately documented zero-ink count), so the report does not lie.
- A real non-whitespace glyph that unexpectedly converts to no splines/vertices still fails and cannot be relabeled as zero ink.

Name the central regression `test_positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings`.

Run it RED before production edits.

### Step 2: Implement explicit zero-ink conversion outcomes

In `_prepare_positioned_converted_candidate` and its batch caller, branch on already-proven `pdf_metric_zero_ink_identity` (or equivalent source metadata) before treating empty spline/vertex output as an error. Return a verified zero-ink delivery outcome/record that owns and cleans the temporary FONT artifacts and carries no visible Blender entity. Aggregate it without aborting visible siblings and without inflating physical object counts.

Keep the existing hard failure for empty visible glyph conversions.

### Step 3: Verify and commit

Run the focused tests, complete fidelity tests, ownership/cleanup tests, and compilation. Commit only Task 2 files with `[skip release]`.

---

## Task 3: Make the real-host acceptance gate representative

**Files:**

- Modify: `tools/headless_text_representation_acceptance.py`
- Modify: the focused tests for that tool under `tests/`
- Modify only if required: existing acceptance launcher/documentation

### Step 1: Add failing gate tests

Add tests showing the current one-item selection can pass while a later exact-font item is over-wide and while a mixed span containing spaces fails Glyphs/Geometry. Require the gate to fail in both cases.

### Step 2: Widen the gate without hiding failures

Make the Blender headless acceptance gate:

- exercise every requested text mode over a representative deterministic corpus that includes every embedded font face/size/rotation/affine class plus whitespace-bearing positioned spans;
- run the source-derived ink verifier for every visible exact-font item;
- assert zero-ink coverage separately;
- validate requested/delivered type, item coverage, failed-rung cleanup, saved `.blend` reopen identity, and report consistency;
- retain the full Welding-Symbol-Chart corpus 3D Text gate and add full-corpus Glyphs/Geometry if runtime remains practical; otherwise use deterministic class coverage plus a separately recorded full-corpus preflight, never the first convenient item.

No pixel similarity score may override a failed representation or exact-bound check.

### Step 3: Verify and commit

Run the acceptance-tool tests, static compilation, and a real Blender headless targeted acceptance. Commit only Task 3 files with `[skip release]`.

---

## Task 4: Full live-host verification and release truth

**Files:**

- Modify only after all gates pass: version/changelog/readme files already used by this repository
- Create evidence outside git under `C:\TMP\blender-week-defect-sweep-evidence`

### Step 1: Run the complete automated suite

Run all pytest suites, repository preflight/sync checks, build checks, and Python compilation. Any unrelated failure must be diagnosed rather than waived.

### Step 2: Run Blender 5.2 real-host matrix

Using the isolated worktree source, import both protected PDFs in all requested modes. For Welding-Symbol-Chart, verify at minimum:

- all 372 semantic text items are covered;
- MyriadPro items 240-243 no longer overrun their exact source ink bounds;
- Glyphs and Geometry complete whitespace-bearing spans;
- Text remains FONT, Labels remain the documented label representation, 3D Text remains extruded FONT, Glyphs remain CURVE, Geometry remains MESH, and Raster remains image-based;
- save/reopen preserves representation, placement, style, report identity, and cleanup guarantees.

For image-only AWSWeldSymbolchart, verify truthful per-request impossibility/fallback evidence and the existing accurate image orientation. Do not call a silent raster substitution a native success.

### Step 3: Inspect artifacts and truthfully version

Render deterministic orthographic evidence for the source, fixed Text/3D Text item 240, mixed Glyphs/Geometry spans, Raster, and reopened files. Inspect them visually. Only then bump the repository's patch version/changelog, build the distributable, rerun release checks, commit, and push.

