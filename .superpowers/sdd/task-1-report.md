# Task 1 report: exact-font ink in the correct metric domain

## Scope and preflight

- Worktree: `C:\TMP\blender-week-defect-sweep`
- Branch: `codex/blender-week-defect-sweep`
- Starting HEAD: `fc485a8 docs(blender): plan truthful text representation [skip release]`
- Starting status: clean; `git status --short --branch --untracked-files=all` printed only:

  ```text
  ## codex/blender-week-defect-sweep...origin/main [ahead 1]
  ```

- Isolation evidence:

  ```text
  GIT_DIR=C:/1PDF-Importer-Blender/.git/worktrees/blender-week-defect-sweep
  GIT_COMMON=C:/1PDF-Importer-Blender/.git
  SUPERPROJECT=
  BRANCH=codex/blender-week-defect-sweep
  ```

- Git rejected the first audit because the sandbox-owned linked worktree triggered dubious-ownership protection. Every later Git command used the non-persistent per-command override `-c safe.directory=C:/TMP/blender-week-defect-sweep`; no global Git configuration was changed.
- The initial baseline command using pytest's default temp root reached 41 passing tests and 10 setup errors, all caused by `PermissionError` on `C:\Users\Rowdy Payton\AppData\Local\Temp\pytest-of-Rowdy Payton`. The corrected baseline command was:

  ```powershell
  python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-baseline
  ```

  ```text
  ...................................................                      [100%]
  51 passed in 0.36s
  ```

## RED evidence

Only `tests/test_representation_fidelity_blender.py` was modified before the authoritative RED run. The focused regressions covered the UPEM metric domain, source-derived expected world bounds, the observed 1.5x evaluated over-width false green, independent expected/actual bound evidence, and explicit zero-ink verification.

Command:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-red2 -k "positioned_font_axis_metrics_use_upem_for_horizontal_glyph_domain or positioned_metric_affine_stores_exact_source_glyph_world_ink_bounds or positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds or positioned_metric_verification_preserves_explicit_zero_ink_identity"
```

Output (failure causes preserved verbatim; traceback framing omitted):

```text
FFFF                                                                     [100%]
test_positioned_font_axis_metrics_use_upem_for_horizontal_glyph_domain
E       KeyError: 'design_unit_scale'

test_positioned_metric_affine_stores_exact_source_glyph_world_ink_bounds
E       AssertionError: assert None == approx((0.013...68 +/- 2.7e-08))

test_positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds
E       AssertionError: assert 'evaluated_ink_bounds_outside_exact_source_glyph_bounds' in []

test_positioned_metric_verification_preserves_explicit_zero_ink_identity
E       KeyError: 'zero_ink_identity'

4 failed, 51 deselected in 0.70s
```

An earlier provisional RED invocation was discarded because two tests accidentally used fixture glyph id 37 against a three-glyph test font and therefore failed on fixture setup rather than the defect. The fixture was corrected to glyph id 1, and the authoritative RED above then failed only on the missing Task 1 behaviors. No production file had been edited.

## Implementation

- Corrected positioned design scaling to `font size / units-per-em` for glyph coordinates and horizontal advance.
- Kept ascent/descent intentional for line height and fallback-bottom baseline offset, expressed in the same UPEM-derived design scale.
- Parsed the selected glyph from `font_asset.usable_bytes` with FontTools `BoundsPen`, verified the byte digest and UPEM metadata, and cached the immutable design-space result by exact font digest and glyph id.
- Transformed the source design bounds through the intended metric affine and stored only the expected world-space bounds needed by post-evaluation verification.
- Derived actual world-space ink bounds independently from `evaluated.bound_box` and `evaluated.matrix_world`.
- Added a documented 0.05 mm Blender curve-tessellation tolerance. The verifier reports `evaluated_ink_bounds_outside_exact_source_glyph_bounds` for overrun and does not change representation or broaden a scale-relative tolerance.
- Preserved explicit zero-ink identity: no expected or actual ink bounds are fabricated, while baseline, advance, line axis, and matrix checks still run.
- A parsed empty glyph is accepted as zero ink only for whitespace. A non-whitespace source character with no exact source glyph ink bounds fails construction.
- Copied the minimal metric-bound metadata to evaluated CURVE/MESH candidates so later requested-rung verification remains source-derived.

## GREEN and verification evidence

Focused regressions:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-final-focused -k "positioned_font_axis_metrics_use_upem_for_horizontal_glyph_domain or positioned_metric_affine_stores_exact_source_glyph_world_ink_bounds or positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds or positioned_metric_verification_preserves_explicit_zero_ink_identity"
```

```text
....                                                                     [100%]
4 passed, 51 deselected in 0.24s
```

Complete representation-fidelity file:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-final-full
```

```text
.......................................................                  [100%]
55 passed in 0.57s
```

Relevant cleanup, shared-contract, and exact-font source tests:

```powershell
python -m pytest tests\test_clean_break.py tests\test_pdfcadcore_shared_contract.py tests\test_blender_source_fidelity.py -q --basetemp C:\TMP\blender-week-defect-sweep-final-contract-cleanup
```

```text
...........................................                    [100%]
43 passed, 10 subtests passed in 9.92s
```

Static compilation:

```powershell
python -m py_compile pdf_vector_importer\bl_text_builder.py tests\test_representation_fidelity_blender.py
```

```text
(exit 0; no output)
```

Lint:

```powershell
python -m ruff check pdf_vector_importer\bl_text_builder.py tests\test_representation_fidelity_blender.py
```

```text
All checks passed!
```

Whitespace/error diff gate:

```powershell
git -c safe.directory=C:/TMP/blender-week-defect-sweep diff --check
```

```text
(exit 0; no output)
```

Final pre-commit scope check:

```powershell
git -c safe.directory=C:/TMP/blender-week-defect-sweep status --short --untracked-files=all
```

```text
 M pdf_vector_importer/bl_text_builder.py
 M tests/test_representation_fidelity_blender.py
```

Git also warned that the current user could not read its global excludes file under `.config`; this did not alter the exit status or the two-file scope shown above.

## Authorized files changed

- `pdf_vector_importer/bl_text_builder.py`
- `tests/test_representation_fidelity_blender.py`

No focused helper file was necessary. This report is under the ignored `.superpowers` directory and is intentionally not part of the Task 1 commit.

## Commit

- `545f270 fix(blender): verify exact glyph ink bounds [skip release]`
- Commit output:

  ```text
  [codex/blender-week-defect-sweep 545f270] fix(blender): verify exact glyph ink bounds [skip release]
   2 files changed, 433 insertions(+), 5 deletions(-)
  ```

## Assumptions

- `source_glyph_id` indexes the usable embedded font's glyph order; this is the same order used to construct `glyph_advances` in `pdfcadcore.embedded_fonts`.
- Blender FONT `size` expresses the design em scale, so glyph coordinates and horizontal advance use UPEM; ascent/descent then intentionally determine line height and fallback-bottom baseline within that domain.
- The existing 0.05 mm host font-tessellation allowance is a small absolute noise tolerance. It is not increased for larger glyphs and is far below the observed 1.5x over-width defect.
- The exact usable font bytes were already accepted by the embedded-font pipeline. Task 1 re-verifies their digest and UPEM before deriving bounds but does not change the shared immutable asset schema, keeping edits inside the authorized files.

## Residual concern / next gate

- Task 1's CPython fake-host tests prove source/actual independence and rejection semantics, but a real Blender 5.2 evaluation remains the later plan gate for confirming host tessellation stays inside the documented 0.05 mm tolerance across the protected corpus.
- Per the parent controller's direction, the independent spec/code review is a post-commit gate and will be scheduled before Task 2 advances.

## Independent-review correction: RED evidence

The review correction first strengthened the fake-host regressions so the central
ink test returns a distinct evaluated object and the zero-ink path proves normal
baseline, advance, line-axis, and matrix checks still reject a corrupt evaluated
transform. Those two tests passed against the existing production paths. The new
cache-safety regression failed for the intended missing behavior before any
production edit: after a valid lookup primed the glyph-bound cache, the same font
bytes and glyph with inconsistent UPEM metadata returned the cached bounds instead
of rejecting the mismatch.

Command:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-review-red -k "exact_glyph_design_bounds_rejects_cached_upem_mismatch or positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds or zero_ink_metric_verification_still_rejects_corrupt_evaluated_transform"
```

Output:

```text
F..                                                                      [100%]
================================== FAILURES ===================================
_________ test_exact_glyph_design_bounds_rejects_cached_upem_mismatch _________
E       Failed: DID NOT RAISE RuntimeError

tests\test_representation_fidelity_blender.py:888: Failed
=========================== short test summary info ===========================
FAILED tests/test_representation_fidelity_blender.py::test_exact_glyph_design_bounds_rejects_cached_upem_mismatch
1 failed, 2 passed, 54 deselected in 0.66s
```

## Independent-review correction: implementation and GREEN evidence

- Strengthened the central 1.5x regression with separate source and evaluated
  objects whose matrices and bounds deliberately differ. The recorded actual
  bounds and affine matrix must come from the evaluated object.
- Added a zero-ink regression whose evaluated transform is translated while the
  intended transform remains unchanged. Baseline, advance, line-axis, and affine
  mismatch reasons all remain enforced without fabricated ink bounds.
- Added a cache-safety regression that primes a valid glyph-bound entry and then
  requires the same font/glyph with inconsistent UPEM metadata to fail closed.
- Extended the glyph-bound cache identity from `(digest, glyph_id)` to
  `(digest, glyph_id, validated_upem)`. No representation, fallback, or tolerance
  logic changed.

Review-focused GREEN command:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-review-green -k "exact_glyph_design_bounds_rejects_cached_upem_mismatch or positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds or zero_ink_metric_verification_still_rejects_corrupt_evaluated_transform"
```

```text
...                                                                      [100%]
3 passed, 54 deselected in 0.21s
```

Complete Task 1 focused regressions:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-review-final-focused -k "positioned_font_axis_metrics_use_upem_for_horizontal_glyph_domain or exact_glyph_design_bounds_rejects_cached_upem_mismatch or positioned_metric_affine_stores_exact_source_glyph_world_ink_bounds or positioned_metric_verification_rejects_evaluated_ink_bounds_outside_exact_source_glyph_bounds or positioned_metric_verification_preserves_explicit_zero_ink_identity or zero_ink_metric_verification_still_rejects_corrupt_evaluated_transform"
```

```text
......                                                                   [100%]
6 passed, 51 deselected in 0.21s
```

Complete representation-fidelity file:

```powershell
python -m pytest tests\test_representation_fidelity_blender.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-review-final-full
```

```text
.........................................................                [100%]
57 passed in 0.53s
```

Relevant cleanup, shared-contract, and exact-font source tests:

```powershell
python -m pytest tests\test_clean_break.py tests\test_pdfcadcore_shared_contract.py tests\test_blender_source_fidelity.py -q --basetemp C:\TMP\blender-week-defect-sweep-pytest-review-final-contract-cleanup
```

```text
...........................................                    [100%]
43 passed, 10 subtests passed in 8.88s
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

The staged scope contained only:

```text
pdf_vector_importer/bl_text_builder.py
tests/test_representation_fidelity_blender.py
```

Review-fix commit:

```text
6c8852e fix(blender): address glyph metric review findings [skip release]
2 files changed, 94 insertions(+), 5 deletions(-)
```

This report remains untracked and was not staged or committed.
