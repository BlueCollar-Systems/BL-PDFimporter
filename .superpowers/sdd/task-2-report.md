# Task 2 report: preserve positioned zero-ink spaces

## Scope and root cause

- Worktree: `C:\TMP\blender-week-defect-sweep`
- Branch: `codex/blender-week-defect-sweep`
- Starting HEAD: `6c8852e fix(blender): address glyph metric review findings [skip release]`
- Authorized tracked files only:
  - `pdf_vector_importer/bl_text_builder.py`
  - `tests/test_representation_fidelity_blender.py`
- Root cause: the positioned Glyphs/Geometry batch verified the whitespace FONT as
  an explicit zero-ink metric identity, but `_prepare_positioned_converted_candidate`
  then treated the expected empty spline/vertex conversion exactly like an empty
  visible glyph. That aborted the whole mixed span and removed its visible siblings.

## RED evidence

Only `tests/test_representation_fidelity_blender.py` was modified before the
authoritative RED run. The central regression exercised positioned `"A B"` in
both requested Glyphs and requested Geometry. Separate visible-glyph-empty
regressions remained green, proving the existing hard-failure invariant before
the production change.

Command:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-red2 -k "positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings or positioned_non_whitespace_empty_conversion_hard_fails_without_zero_ink_relabel"
```

Output:

```text
FF..                                                                     [100%]
FAILED tests/test_representation_fidelity_blender.py::test_positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings[glyphs-CURVE]
FAILED tests/test_representation_fidelity_blender.py::test_positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings[geometry-MESH]
2 failed, 2 passed, 57 deselected in 0.41s
```

Both central cases failed because `build_text` returned `None` after the space's
empty conversion aborted the requested rung. No production file had been edited.

## Implementation

- Required three independent facts before accepting an empty conversion as zero
  ink: source FONT metadata marks zero ink, evaluated source verification records
  both zero-ink identity and verified bounds, and the source character is
  whitespace. Text alone cannot grant the exception.
- Represented that character as a verified delivery record with no Blender
  entity, rather than manufacturing an empty CURVE/MESH object.
- Removed the zero-ink source FONT object, its FONT curve, its owned material,
  affine carrier if present, and the temporary empty conversion datablock before
  completing the batch. Cleanup status is retained in character evidence.
- Kept visible characters as requested CURVE/MESH entities and kept an empty
  non-whitespace conversion on the existing hard-failure path.
- Recorded all character identities and layouts, requested/delivered rungs,
  advance width, zero-ink proof, and cleanup while reporting physical entity
  counts separately from attempted/visible/zero-ink character counts.
- Added a hard failure for an all-zero-ink converted item because the outer
  delivery contract requires at least one verified host entity; no placeholder
  geometry is created to make that contract appear green.

## GREEN and final verification evidence

Focused mixed-span and visible-empty invariants:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-final-focused -k "positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings or positioned_non_whitespace_empty_conversion_hard_fails_without_zero_ink_relabel"
```

```text
....                                                                     [100%]
4 passed, 57 deselected in 0.21s
```

Complete representation-fidelity file:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-final-full
```

```text
.............................................................            [100%]
61 passed in 0.42s
```

Ownership, cleanup, shared contract, and Blender source-fidelity gates:

```powershell
python -m pytest tests\test_clean_break.py tests\test_pdfcadcore_shared_contract.py tests\test_blender_source_fidelity.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-final-contract-cleanup
```

```text
...........................................                    [100%]
43 passed, 10 subtests passed in 7.17s
```

Static gates:

```powershell
python -m py_compile pdf_vector_importer\bl_text_builder.py tests\test_representation_fidelity_blender.py
python -m ruff check pdf_vector_importer\bl_text_builder.py tests\test_representation_fidelity_blender.py
git -c safe.directory=C:/TMP/blender-week-defect-sweep diff --check
```

```text
py_compile: exit 0; no output
ruff: All checks passed!
diff --check: exit 0; no output
```

The staged scope contained exactly the two authorized files. `.superpowers` was
not staged.

## Commit

- `2082147 fix(blender): preserve positioned zero-ink characters [skip release]`
- `2 files changed, 385 insertions(+), 17 deletions(-)`

## Residual gate

The fake-host tests exercise the exact batch accounting and cleanup contract.
Task 3's independent headless Blender acceptance gate remains responsible for
proving Blender 5.2 returns empty conversion datablocks for the protected spaces
and preserves these identities through save/reopen over the representative PDF
corpus.

## Independent-review correction and anti-roadblock amendment

The first independent review rejected Task 2 for two blockers:

1. An all-whitespace positioned Glyphs/Geometry item reached the post-batch
   `not outcomes` branch and force-failed even though every character had already
   been verified as zero ink and completely cleaned.
2. The internal zero-ink cleanup outcome owned the source and conversion
   datablocks, but not the owned text material explicitly. If source/data removal
   succeeded and material removal failed, invalid host references could be
   filtered before the outer retry, allowing cleanup to appear complete while the
   material remained live.

The generic delivery controller required every success to have a non-`None`
entity and at least one positive entity ID. A string/sentinel would only have
disguised this roadblock as a fake entity. The parent controller therefore
authorized a narrow scope amendment for `pdf_vector_importer/text_delivery.py`:
accept an entity-less delivery only when it is the originally requested Glyphs
or Geometry rung and complete, item-bound zero-ink proof validates. Ordinary
positive-ID delivery remains unchanged.

### Review-correction RED evidence

Before any review-correction production edit, the all-whitespace, partial
material cleanup, valid entity-less contract, malformed/forged proof, and
fallback-rung tests produced:

```text
FFFFFFFFFFFFFFFFF                                                        [100%]
17 failed, 61 deselected in 1.68s
```

The material test's first fake-host liveness model compared object names and
therefore confused the source and converted fake objects, which share a name in
the fixture. The test was corrected to track removed host references by identity
without changing production. Its authoritative RED then reached the intended
defect in both requested modes:

```text
FF                                                                       [100%]
2 failed, 76 deselected in 0.50s
```

Both failures showed `PDF_Text_source.001` still live while the attempt cleanup
record said `complete`.

Additional fail-closed tests were added one at a time and observed RED before
their production changes:

- fractional source counts were accepted as integers: 1 failed;
- fractional page/character identities were accepted: 2 failed;
- physical Raster was falsely authorizable by positioned zero-ink proof: 1 failed;
- an entity-less claim retaining owned artifacts was accepted: 1 failed;
- zero-ink provenance claimed a physical CURVE/MESH type: 2 failed.

### Review-correction implementation

- Added explicit, deterministic item and character identities and a
  `positioned_zero_ink_delivery_v1` proof record.
- Added a narrow controller proof gate requiring requested==delivered, no
  fallback, Glyphs/Geometry only, zero physical entities, zero visible
  characters, full source-character coverage, finite source layout/advance,
  verified per-character zero ink, complete per-character cleanup ledgers, and
  no remaining owned objects/datablocks/artifacts.
- Rejects missing, fractional, partial, cross-rung, wrong-representation, visible
  text, forged entity ID, failed cleanup, and retained-ownership evidence.
- Returns `None` for the host entity while recording a successful logical
  delivery identity; no placeholder object or entity ID is fabricated.
- Does not increment `_text_delivered_entity_counts` for a zero-physical-entity
  delivery and records logical provenance as
  `blender_zero_ink_<mode>_identity`.
- Explicitly owns the zero-ink text material in the cleanup outcome. If its first
  removal fails after source/data removal, the remaining material survives
  ownership filtering and the outer cleanup retry removes it.

### Review-correction GREEN evidence

Focused Task 2 and controller proof gates:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-review-final-focused -k "positioned_conversion_preserves_zero_ink_space_and_delivers_visible_siblings or positioned_non_whitespace_empty_conversion_hard_fails_without_zero_ink_relabel or positioned_whitespace_only_delivers_verified_zero_ink_without_host_entity or zero_ink_material_cleanup_failure_remains_owned_for_outer_retry or entityless_zero_ink_delivery or zero_ink_conversion_proof_cannot_authorize_raster_delivery"
```

```text
..........................                                               [100%]
26 passed, 57 deselected in 0.17s
```

Complete representation-fidelity file:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-review-final-full
```

```text
........................................................................ [ 86%]
...........                                                              [100%]
83 passed in 0.41s
```

Cleanup, shared-contract, source-fidelity, and terminal-delivery gates:

```powershell
python -m pytest tests\test_clean_break.py tests\test_pdfcadcore_shared_contract.py tests\test_blender_source_fidelity.py tests\test_terminal_raster_delivery_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-task2-review-final-contract-cleanup
```

```text
.............................................................. [ 88%]
........                                                                 [100%]
70 passed, 10 subtests passed in 7.20s
```

Static gates included `text_delivery.py`, `bl_text_builder.py`, and the fidelity
test: `py_compile` exited 0, Ruff reported `All checks passed!`, and
`git diff --check` exited 0.

Review-fix commit:

```text
c1aad44 fix(blender): close zero-ink review gaps [skip release]
3 files changed, 781 insertions(+), 13 deletions(-)
```

The report remains untracked and was not staged.

### Next-task compatibility note

Task 3's acceptance code currently assumes one physical entity per character
around `tools/headless_text_representation_acceptance.py:134`. That gate must be
updated in Task 3 to count zero-ink logical identities separately from physical
entities. It was intentionally not edited in this task.

## Second independent-review correction: source binding and final-state truth

The second independent review rejected the first correction at two integration
boundaries:

1. `_reverify_text_delivery_after_stack` unconditionally required a positive
   host entity ID, so it rewrote a valid, requested Glyphs/Geometry logical
   zero-ink delivery to failed after page placement.
2. The controller proved that submitted zero-ink evidence was internally
   consistent, but did not compare it with source-character truth captured
   independently before the host attempt. Altered advances/origins/layouts or a
   replayed nested proof could therefore be made self-consistent.

### Second-review RED evidence

Only tests were changed before the authoritative boundary run. The run covered
the all-whitespace builder-to-page-stack path, source-manifest binding,
partial/altered/replayed proofs, mutation of the caller's manifest during the
attempt callback, Raster rejection, and the ordinary positive-entity guard:

```powershell
python -m pytest tests/test_representation_fidelity_blender.py -k "positioned_whitespace_only or entityless_zero_ink or zero_ink_expected_manifest" tests/test_terminal_raster_delivery_blender.py -k "positioned_whitespace_only or entityless_zero_ink or zero_ink_expected_manifest or post_stack_zero_ink_claim or ordinary_delivery_still_requires" -q
```

```text
31 failed, 1 passed, 90 deselected in 2.98s
```

The failures were the intended missing manifest API, a valid builder record
lacking an independently persisted source manifest, the post-stack verifier's
`final_entity_identity_missing`, and acceptance of a zero-ink proof with no
independent source manifest. During self-review, a separate malformed-manifest
test was observed RED with `AttributeError` before the fail-closed fix.

### Second-review implementation

- Captures an exact positioned source-character manifest before any host
  attempt: item/page/span/requested mode, full source text, character IDs and
  order, exact text/glyph identity, advance and glyph height, source origin,
  source bbox/quad, and target origin/quad.
- Canonicalizes that manifest as deterministic JSON and binds it with SHA-256.
  The delivery controller freezes its own snapshot before invoking the attempt,
  so callback mutation cannot redefine expected source truth.
- Reconstructs the same manifest from delivery evidence and requires both exact
  canonical equality and digest equality. Missing, malformed, partial, altered,
  or replayed proof fails closed.
- Binds each nested conversion proof to the source manifest, parent item,
  page/span, character ID/index, exact character/glyph, and requested mode.
- Persists the independently verified manifest separately from delivery
  evidence for final-state verification.
- Permits zero physical IDs after page stacking only when the record, attempt,
  runtime outcome, canonical source manifest, and requested Glyphs/Geometry
  proof all revalidate. Raster and all other modes remain ineligible; ordinary
  deliveries still require positive host IDs and retain their type/location
  checks.
- Removes the persisted source manifest if a failed final verification is
  completely cleaned, keeping runtime truth and cleanup accounting aligned.

### Second-review GREEN evidence

Complete representation-fidelity gate:

```text
97 passed in 0.58s
```

Terminal/post-stack gate:

```text
29 passed in 0.60s
```

Cleanup/ownership gate:

```text
23 passed, 10 subtests passed in 5.82s
```

Additional Blender source, packed-asset, and shared-contract gates:

```text
16 passed in 2.58s
3 passed in 0.04s
4 passed in 0.52s
```

`py_compile`, Ruff, and `git diff --check` all completed successfully. The
tracked correction is confined to the Blender controller/builder/final verifier
and their two regression files. This report remains untracked and is not staged.

Second-review commit:

```text
200ffc6 fix(blender): bind zero-ink delivery to source truth [skip release]
5 files changed, 794 insertions(+), 13 deletions(-)
```

## Third independent-review correction: affine and nested mixed-delivery truth

The third independent review rejected the second correction at three remaining
boundaries:

1. The canonical source manifest did not bind the intended/evaluated 4x4 affine
   matrix.
2. Logical zero-ink proof was only verified for an entirely entity-less item;
   a mixed positioned batch such as `A<space>B` bypassed it as soon as the two
   visible children supplied positive physical IDs.
3. Post-stack failure rollback always subtracted one physical delivery count,
   including all-logical-zero items which had never incremented that count.

### Third-review RED evidence

Tests were changed before production. The authoritative focused RED covered
missing/altered affine matrices, missing/corrupt nested child manifests, mixed
positive-ID controller and final-state bypasses, and preexisting count rollback:

```text
14 failed, 26 passed, 70 deselected in 1.77s
```

Additional one-at-a-time RED cases found during self-review proved that:

- deleting the mixed child count suppressed the final audit (`1 failed`);
- deleting both the independent manifest and count suppressed the controller
  audit (`1 failed`);
- a malformed persisted manifest raised `TypeError` instead of failing closed
  (`1 failed`);
- final failure records did not expose actual count contribution (`5 failed`);
- the first contribution-aware page-total change broke legacy failure records
  lacking the new field (`1 failed` in the repository-wide run).

### Third-review implementation

- Added a finite canonical 16-value intended affine matrix to every positioned
  source character before host mutation. Logical-zero verification requires the
  submitted intended matrix to match canonical source truth within `1e-12` and
  the evaluated matrix to match intended host truth within `1e-6`; missing,
  non-finite, or altered matrices fail closed.
- Added deterministic `positioned_zero_ink_character_manifest_v1` manifests and
  SHA-256 digests for every logical-zero child. Each child manifest is derived
  from the independently frozen whole-item manifest and binds item/page/span,
  requested mode, character ID/index/text/glyph/layout, and affine matrix.
- Generalized zero-ink proof verification to mixed physical/logical batches.
  Positive visible entity IDs no longer bypass validation of whitespace children.
  Detection is redundant across expected source truth, submitted child evidence,
  submitted counts, and final records so deleting one indicator cannot suppress
  the controller or post-stack audit.
- Persisted canonical manifests for mixed batches, revalidated them after page
  stacking, and tied the runtime outcome and delivered attempt IDs to the final
  record. Missing/corrupt child or item manifests fail closed even when visible
  CURVE/MESH entities remain live.
- Preserved requested Glyphs/Geometry. Mixed batches may advance to the next rung
  only after affirmative item-scoped impossibility proof; Raster and ordinary
  empty-ID rejection remain unchanged.
- Added explicit `delivered_count_contribution`: zero for all-logical-zero
  deliveries and one for physical/mixed deliveries. Runtime representation
  counts and page text totals now roll back by that contribution. Legacy failure
  records without the field retain their historical one-item default.
- Malformed persisted manifests now produce verification failures rather than
  exceptions, and cleanup removes the canonical manifest only after complete
  owned-artifact cleanup.

### Third-review final verification

Focused adversarial suite after implementation:

```text
40 passed, 70 deselected in 0.57s
```

Complete representation-fidelity file during the correction:

```text
110 passed in 0.57s
```

Final repository-wide gate after the self-review amendments:

```text
255 passed, 1 skipped, 10 subtests passed in 21.10s
```

Static gates:

```text
python -m ruff check ...: All checks passed!
python -m py_compile ...: exit 0; no output
git diff --check: exit 0; no output
```

Tracked scope is exactly:

- `pdf_vector_importer/text_delivery.py`
- `pdf_vector_importer/bl_text_builder.py`
- `pdf_vector_importer/bl_import_engine.py`
- `tests/test_representation_fidelity_blender.py`

The `.superpowers` report remains untracked and is not staged.

Third-review correction commit:

```text
e92a64a fix(blender): bind nested zero-ink affine truth [skip release]
4 files changed, 1233 insertions(+), 67 deletions(-)
```

## Fourth independent-review correction: exact delivery identity and immutable reconciliation

The fourth review supplied eleven unchanged adversarial tests covering the
remaining mutable trust inputs:

- fractional page/span/font-failure identities unlocking fallback;
- mixed/all-zero runtime IDs not bound exactly to character identities;
- failed or incomplete top-level cleanup ledgers;
- source-manifest affine axes diverging from real embedded whitespace metrics;
- mutable record representation/entity IDs suppressing final nested proof; and
- mutable count contribution decrementing a preexisting physical bucket.

### Fourth-review RED evidence

Before production changes, the exact reviewer selection produced:

```text
11 failed, 115 deselected in 1.29s
```

Every failure reached the intended behavioral assertion; there were no setup or
collection failures. The reviewer-owned test changes were preserved unchanged.

### Fourth-review implementation

- Replaced lossy `int(...)` proof-identity coercion with exact non-boolean
  integer checks for page, source span, and font-failure page identities.
- Requires runtime physical IDs to equal the ordered union of visible character
  IDs exactly. Logical-zero characters cannot own physical IDs, all-logical-zero
  deliveries require an empty physical ledger, and malformed/duplicate/ghost
  IDs fail closed.
- Requires affirmative top cleanup status and an exact, duplicate-free removal
  ledger equal to the union of the logical child cleanup ledgers.
- Factored positioned font-axis computation so canonical zero-ink matrices and
  runtime application use the same embedded-font units-per-em, ascender,
  descender, glyph advance, object size, and baseline logic. Real whitespace
  glyph IDs therefore use their exact embedded metrics; source-layout fallback
  remains limited to whitespace without a glyph identity.
- Creates a separately frozen and SHA-256-bound delivery manifest inside the
  delivery controller after proof succeeds. It binds requested/delivered mode,
  exact physical IDs, source/zero character counts, logical-zero status, source
  manifest digest, and delivered-count contribution.
- Final verification derives representation, entity IDs, logical/physical
  branch, zero-child count, contribution, and counter bucket from that canonical
  delivery manifest. Mutable record and attempt fields are exact cross-checks
  only and cannot suppress proof or redirect rollback.
- Invalid/missing canonical count state contributes zero during failure cleanup,
  preventing an unrelated preexisting count from being decremented. Canonical
  source and delivery manifests are removed only after complete owned-artifact
  cleanup.

### Fourth-review final verification

Authoritative eleven-test selection:

```text
11 passed, 115 deselected in 0.28s
```

Complete representation-fidelity file:

```text
126 passed in 0.66s
```

Repository-wide gate:

```text
266 passed, 1 skipped, 10 subtests passed in 17.19s
```

Static gates:

```text
python -m py_compile ...: exit 0; no output
python -m ruff check ...: All checks passed!
git diff --check: exit 0; no output
```

Tracked scope remains exactly the three Blender delivery files plus the
reviewer-owned representation-fidelity test file. `.superpowers` remains
untracked and must not be staged.
