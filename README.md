# PDF Vector Importer for Blender

[![Version](https://img.shields.io/badge/Version-1.0.85-blue.svg)]()

**BUILT. NOT BOUGHT.** -- BlueCollar Systems

Import PDF vector drawings as native Blender geometry (Curves, Collections, Materials).
Powered by the pdfcadcore shared extraction library and PyMuPDF.

## Features

- **4 Import Modes** (BCS-ARCH-001) -- Auto (default, picks strategy per page), Vector, Raster, Hybrid
- **6 Text Representation Options** -- Labels, Text, 3D Text (default), Glyphs, Geometry, and Raster (orthogonal to page strategy)
- **Maximum fidelity by default** -- no quality tiers, no fast-mode compromises
- **Arc & Circle Detection** -- Reconstruct true arcs and circles from polyline approximations
- **OCG Layer Support** -- Map PDF Optional Content Groups to Blender sub-collections
- **Color Grouping** -- Organize geometry into sub-collections by stroke color
- **Material Assignment** -- Automatic diffuse materials from PDF stroke colors
- **Text Import** -- Import text as Blender font objects with position, size, and rotation
- **Face Generation** -- Convert closed loops and rectangles to mesh faces
- **Line Width Mapping** -- PDF stroke widths mapped to curve bevel depth
- **Dash Pattern Preservation** -- Retain PDF dash styling information

## Installation

### Blender Add-on (Recommended)

1. Download `Blender-PDF-Importer_vX.Y.Z.zip` from Releases, or build it with:
   ```bash
   python build_release.py
   ```
2. In Blender: **Edit > Preferences > Add-ons > Install...**
3. Choose `Blender-PDF-Importer_vX.Y.Z.zip`
4. Enable **PDF Vector Importer**

Release ZIPs include a private PyMuPDF runtime under `pdf_vector_importer/lib`,
so users do not need system Python, pip, or operating-system packages. Import
never runs pip or downloads packages. It may restore only the exact bundled
`extra.py` helper from the copy already inside the release. The preferences-
panel installer is hidden in packaged releases. It is an explicit
source/development tool that warns and requires confirmation before pip may use
the network and replace private package files. Customers should reinstall the
official release ZIP for any runtime damage beyond the bundled helper repair.

**Offline install:** Release ZIPs from GitHub work without internet after download.

## Upgrading / skipping versions

Install the latest release ZIP via Preferences → Add-ons (disable old version first if Blender keeps both). Skipping versions is supported — run `--preflight` or import one of your own representative PDFs after a major jump.

### Manual Install

Copy the `pdf_vector_importer/` directory into your Blender addons path:
- Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\`
- macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/`
- Linux: `~/.config/blender/<version>/scripts/addons/`

## Usage

After enabling the addon:

1. **File > Import > PDF Vector (.pdf)**
2. Select a PDF file
3. Choose import mode (Auto, Vector, Raster, or Hybrid) and adjust options in the import panel
4. Click **Import PDF Vector**

Geometry is grouped into collections by page and (optionally) by source layer or color.

Long imports publish phase/page/object progress in Blender's status area and in
`import_report.json`. The report includes a representation-aware complexity
estimate (`work_units` and `tier`) so Geometry, Glyphs, and 3D Text requests are
not presented as equivalent to flat Text. **Cancel Active PDF Import** requests
cooperative cancellation at the next object heartbeat. The incomplete page is
rolled back, completed pages remain, and an atomic source/config-bound checkpoint
records exactly which pages remain. Enable **Resume Interrupted Import** on the
same PDF and settings to continue in the existing root collection without
duplicating completed pages.

Choose the representation you actually need: **Text** is flat editable Blender
`FONT`, **3D Text** is extruded editable `FONT`, **Glyphs** is fixed `CURVE`
outline data, **Geometry** is fixed `MESH` data, and **Raster** is one aligned
image patch per source text item. **Labels** requests a persistent,
model-scaled label entity; Blender currently has no such renderable entity, so
that item-specific capability result is recorded before the closest fallback
is attempted.

### Text-mode fallback ladder (TEXTMODE-1)

**The requested text mode is the delivered text mode.** Alignment, rotation,
or scaling defects are fixed *inside* the requested mode — never by
substituting a different mode. Substitution is permitted only when the
requested mode is genuinely impossible for this importer + option + PDF, must
walk the documented ladder below (most closely related rung first), and is
always recorded. If even the terminal raster patch cannot be verified, the
item is reported as failed instead of being claimed as delivered.
(Owner directive 2026-07-13.)

FINAL Blender ladder (left rung first):

| Requested | Ladder |
|-----------|--------|
| **Labels** | Labels → Text → 3D Text → Glyphs → Geometry → Raster |
| **Text** | Text → 3D Text → Glyphs → Geometry → Raster |
| **3D Text** | 3D Text → Text → Glyphs → Geometry → Raster |
| **Glyphs** | Glyphs → Geometry → Raster |
| **Geometry** | Geometry → Glyphs → Raster |
| **Raster** | Raster only |

Notes:
- Text, 3D Text, Glyphs, and Geometry load only the exact embedded PDF font
  program associated with the source span. The importer does not search the
  operating system for a similarly named font.
- Glyphs and Geometry are distinct host types and conversion paths: Glyphs
  verifies a real Blender `CURVE`; Geometry verifies a real Blender `MESH`.
- A generic exception is a failure, not proof that the requested type is
  impossible. Fallback continues only after item-specific capability evidence
  and complete cleanup of artifacts owned by that attempt.
- Unknown `text_mode` values raise before scene mutation. They are never
  silently normalized to another requested outcome.
- Auto/Vector/Raster/Hybrid is a page strategy and does not override a text
  representation request. A raster page can still contain requested native
  Text, 3D Text, Glyphs, or Geometry objects.
- `import_report.json` stores every source item, attempted rung, result,
  evidence, cleanup record, final representation, and entity ID under
  `extra.text_delivery`; failed item IDs are surfaced in diagnostics.
- These invariants are locked by
  `tests/test_representation_fidelity_blender.py` and
  `tests/test_terminal_raster_delivery_blender.py`.
- The proof, oracle, rollback, and reporting contract is maintained in
  [REPRESENTATION_FIDELITY.md](REPRESENTATION_FIDELITY.md).

## Compatibility

See **[COMPATIBILITY.md](COMPATIBILITY.md)** for the full matrix. Summary:

| Blender Version | Bundled Python | PyMuPDF | Status |
|----------------|---------------|---------|--------|
| 5.2 LTS | 3.13 | >=1.24,<2.0 | ✅ Requested-representation host acceptance |
| 3.6 LTS | 3.10 | >=1.24,<2.0 | ⚠️ Expected |
| 4.0–4.2 | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 4.5 LTS | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 2.83–2.93 | 3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 2.79 and earlier | | | ❌ Not supported |

Evidence levels:
- `✅ Verified`: host-run validation evidence captured.
- `⚠️ Expected`: syntax/runtime compatible but no host-run evidence yet.
- `❌ Not supported`: outside maintained/tested compatibility scope.

## Requirements

- Blender 3.1 or newer (Blender 3.0 ships Python 3.9; the vendored PyMuPDF wheel requires Python >=3.10)
- Bundled Blender Python 3.10+
- PyMuPDF >=1.24,<2.0, bundled in release ZIPs

## Development

```bash
# Lint
python -m ruff check .

# Tests
python -m pytest tests/ -v
```

## Batch Import

Run batch import summaries across a folder of PDFs:

```bash
python -m blender_pdf_vector_importer.batch_cli "C:\path\to\pdfs" --recursive --mode auto --pages all --json batch_report.json
```

## Project Structure

| Directory | Purpose |
|-----------|---------|
| `pdf_vector_importer/` | Blender addon (install via Edit > Preferences > Add-ons) |
| `blender_pdf_vector_importer/` | Standalone CLI and library for headless/batch processing |

## Known Limitations

| Limitation | Details |
|-----------|---------|
| Encrypted PDFs | Password-protected PDFs must be unlocked before import |
| Compression filters | Decoding is delegated to PyMuPDF. Malformed or non-standard compressed object streams may fail to parse |
| Raster-only scans | Pure raster PDFs produce no vector geometry |
| Clipped/XObject-heavy PDFs | Complex clip stacks and deeply nested form XObjects can produce partial geometry |
| Very large PDFs | Documents with >10,000 primitives can remain slow because Blender creates/verifies native objects individually; use the reported complexity/progress, cooperative Cancel, and page-resume checkpoint |
| Missing or malformed exact font programs | The affected item walks the reported structural ladder to a verified raster patch; an unverified terminal patch is a loud item failure |
| PyMuPDF required | Release ZIPs bundle PyMuPDF; source/dev installs can use the preferences-panel installer |
| Legacy hosts | Blender/Python combinations outside the listed compatibility matrix are expected-only until verified |

## Import report / scale trust

Imports emit `import_report.json` (`bcs.import_report/1.1`) with optional `extra.resolved_scale`.

Every report also records the exact Blender host version, source-PDF SHA-256,
importer version/tag/source commit, installed-package content hash, and the
absolute paths plus SHA-256 hashes of the critical engine/text modules under
`extra.package_identity`.

- Use `factor` only when `confidence >= 0.70` and `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown.

## Bad-PDF open gate

Blender refuses bad PDFs at open time (**fail closed**). SketchUp may fail open on rare gate errors; messages are aligned, detection parity is not.

## License

MIT -- Copyright (c) 2024-2026 BlueCollar Systems

See [LICENSE](LICENSE) for full text.

---

AI-assisted development by Claude (Anthropic).
