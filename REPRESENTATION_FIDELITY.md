# Blender requested-representation fidelity contract

This is the host-specific contract for the Blender importer. A requested
representation is an invariant. Delivery requires both the actual Blender
entity type and the source item's verified text, page, anchor, rotation,
width, height, and stable entity identity. A different type is a fallback,
never a repair for alignment, rotation, or scale.

## Finite item-scoped ladders

| Requested representation | Ordered attempts |
|---|---|
| **Labels** | Labels -> Text -> 3D Text -> Glyphs -> Geometry -> Raster |
| **Text** | Text -> 3D Text -> Glyphs -> Geometry -> Raster |
| **3D Text** | 3D Text -> Text -> Glyphs -> Geometry -> Raster |
| **Glyphs** | Glyphs -> Geometry -> Raster |
| **Geometry** | Geometry -> Glyphs -> Raster |
| **Raster** | Raster only |

The controller advances only from an `impossible` result with item-specific
evidence and `cleanup.status == "complete"`. A generic exception, malformed
artifact, wrong type, empty conversion, visual mismatch, or cleanup failure is
`failed`; it stops that item's ladder and is reported. The ladders are finite,
acyclic, and contain no peer aliases.

## Impossibility evidence

| Attempted rung | Evidence that may authorize the next rung | What does **not** authorize fallback |
|---|---|---|
| Labels | Stable item ID plus Blender host/version capability evidence showing that Blender exposes no persistent, renderable, model-scaled Label entity for that item. | A horizontal annotation, relabeled `FONT`, or visual mismatch. |
| Text | The span's immutable extraction record proves that no exact usable source font program exists: missing, malformed, unsupported, or ambiguous embedded/Base-14 font evidence with source font name and xref. | Font-load exception, system-font substitution, wrong transform, or broken object creation. |
| 3D Text | The same item-specific exact-font evidence as Text. Blender supports extrusion; failure to create or verify positive extrusion is a failure, not impossibility. | Zero extrusion, a flat `FONT` relabeled as 3D, or a generic exception. |
| Glyphs | Exact-font evidence is unavailable, or the running Blender host lacks the evaluated `Object.to_curve` capability. The record includes item ID and capability/font evidence. | Empty splines, wrong transform/dimensions, or a conversion exception. |
| Geometry | Exact-font evidence is unavailable, or the running Blender host lacks `bpy.data.meshes.new_from_object`. The record includes item ID and capability/font evidence. | Empty vertices, wrong transform/dimensions, or a conversion exception. |
| Raster | No transition exists. Missing/empty pixels, an unwritten clip, wrong placement, missing plane identity, or rollback failure is an explicit terminal failure. | Assuming that raster is always achievable. |

An unavailable exact source program is source-item evidence: inventing a
similar operating-system font would change glyph shape and is prohibited. The
free local FontTools path may deterministically wrap/repair the exact embedded
program; it never substitutes a font by name.

## Verification oracle

| Delivered representation | Required verified Blender result |
|---|---|
| Labels | A persistent, renderable, model-scaled native label with verified source transform. Blender currently cannot satisfy this oracle, so the capability attempt is recorded as impossible per item. |
| Text | `Object.type == "FONT"`, exact body including edge whitespace, exact source font asset/hash, zero extrusion, source page/item identity, and verified anchor, rotation, width, and height. |
| 3D Text | The Text oracle plus positive extrusion. |
| Glyphs | The exact-font Text candidate first passes its oracle; conversion then yields a nonempty real `CURVE`, preserves exact source-text metadata, and independently re-verifies anchor, rotation, width, and height. |
| Geometry | The exact-font Text candidate first passes its oracle; conversion then yields a nonempty real `MESH`, preserves exact source-text metadata, and independently re-verifies anchor, rotation, width, and height. |
| Raster | The exact source span bbox renders nonempty pixels to a nonempty file; a real `MESH` image plane has stable item identity and the target bbox placement/dimensions. |

Whitespace-only source spans legitimately have no visible geometry. Text and
3D Text still verify the exact editable body and transform without inventing
visible characters. Structural conversions with no real splines/vertices do
not fabricate delivery.

## Rollback ownership

Every attempt records only objects and datablocks it created. Failed attempts
unlink and remove those exact objects and their `CURVE`/`MESH` datablocks;
conversion also removes its superseded temporary `FONT` candidate. A raster
attempt owns its item clip, plane, and mesh: failed plane or metadata creation
removes the owned scene artifacts and clip. Image/material/font caches are
content-addressed or explicitly shared and are never deleted as though they
belonged to one item. Pre-existing or unattributed user entities are never
cleanup targets. The next rung cannot start when rollback is incomplete.

## Loud reporting

`import_report.json` uses `bcs.import_report/1.1`. Its
`extra.text_delivery` payload uses `bcs.text_delivery/1.0` and records:

- stable import/page/source-span item ID;
- requested representation;
- every ordered attempt, status, reason, host/source evidence, owned artifact,
  cleanup result, and supersession;
- final verified representation and stable entity IDs, or explicit failure;
- requested/delivered/fallback/failed totals plus final-type and failed-ID
  summaries.

The Blender operator emits warnings for fallbacks and errors for failed item
IDs, with the report path. Unknown modes raise before the PDF is opened or a
root collection is created. Page strategy (Auto/Vector/Hybrid/Raster) never
erases a structural text request.

## Regression and host gates

- `tests/test_blender_source_fidelity.py` locks exact source text, transforms,
  font evidence, and explicitly configured local acceptance PDFs.
  Set `BCS_PDF_TEST_FILES` or `BCS_CORPUS_ROOT` for discovery and provide a
  `BCS_BLENDER_FIDELITY_EXPECTATIONS` JSON object that maps PDF basenames to
  expected source-span counts; the paths and expectations stay outside git.
- `tests/test_representation_fidelity_blender.py` locks ladders, distinct host
  types, transform/dimension verification, exception handling, identity, and
  exact rollback.
- `tests/test_terminal_raster_delivery_blender.py` locks real item clips,
  structural-text orthogonality, report payloads, and raster rollback.
- `tools/headless_text_representation_acceptance.py` proves the representations
  in real Blender against two explicitly supplied local acceptance PDFs.

All required paths are local and free to the user. No paid service or paid
dependency is a required representation or fallback.
