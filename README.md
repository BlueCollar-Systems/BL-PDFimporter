# PDF Vector Importer for Blender

[![Version](https://img.shields.io/badge/Version-1.0.64-blue.svg)]()

**BUILT. NOT BOUGHT.** -- BlueCollar Systems

Import PDF vector drawings as native Blender geometry (Curves, Collections, Materials).
Powered by the pdfcadcore shared extraction library and PyMuPDF.

## Features

- **4 Import Modes** (BCS-ARCH-001) -- Auto (default, picks strategy per page), Vector, Raster, Hybrid
- **4 Text Rendering Options** -- 3D Text (default visual-parity path), Labels, Glyphs, Geometry (orthogonal to mode)
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
so users do not need system Python, pip, or operating-system packages. The
preferences-panel **Install PyMuPDF** button remains for source/dev installs and
repairing a manually modified add-on folder.

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

For Adobe-like visual sign-off, start with **3D Text** at the same zoom/scale as
the source PDF. Use **Labels** when editable Blender text matters more than
model-space PDF appearance, and use **Glyphs/Geometry** when exact outline
geometry is preferred over editability.

### Text-mode fallback ladder (TEXTMODE-1)

**The requested text mode is the delivered text mode.** Alignment, rotation,
or scaling defects are fixed *inside* the requested mode — never by
substituting a different mode. Substitution is permitted only when the
requested mode is genuinely impossible for this importer + option + PDF, must
walk the documented ladder below (most closely related rung first), must
always terminate in *some* delivered representation, and is always recorded
in `import_report.json` (`fallback.text` = requested / delivered / reason /
count, plus a `text_mode_fallback` diagnostics signal) — never silent.
(Owner directive 2026-07-13.)

FINAL Blender ladder (left rung first):

| Requested | Ladder |
|-----------|--------|
| **3D Text** | Labels (flat font curve) → Glyphs/Geometry (meshified) → raster plane |
| **Glyphs / Geometry** | peer family → 3D Text/Labels font curves → raster plane |
| **Labels** | 3D Text → Glyphs/Geometry → raster plane |
| **Raster** | terminal — always achievable |

Notes:
- **Glyphs and Geometry are one peer family** (identical meshify pipeline),
  so a fallback between them is a no-op and the ladders treat them as a
  single rung.
- Font-curve creation is core Blender API — its failure is unobserved, which
  is why the flat font curve (same object minus extrude) is the nearest
  rung for 3D Text and the landing rung for a failed meshify.
- A meshify failure for Glyphs/Geometry delivers the editable font curve
  honestly: the object is retagged (`pdf_text_mode='labels'`, requested mode
  and `reason: meshify_failed` preserved on the object), provenance records
  the delivered entity type, and the report emits `fallback.text`.
- Unknown `text_mode` strings deliver the 3D Text default loudly: a one-time
  warning, a `fallback.text` entry (`reason: unknown_text_mode_normalized`),
  and `extra.text_mode_normalized_from` in the report.
- Labels-family rungs sit before the page raster plane per the audit's
  interim ruling (no host has per-span raster patches today); every use is
  loudly reported.
- The invariant "requested == delivered OR reported fallback — never
  neither" is locked by `tests/test_textmode1_invariant_blender.py`.

## Compatibility

See **[COMPATIBILITY.md](COMPATIBILITY.md)** for the full matrix. Summary:

| Blender Version | Bundled Python | PyMuPDF | Status |
|----------------|---------------|---------|--------|
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

- Blender 3.0 or newer
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
| Very large PDFs | Documents with >10,000 primitives may cause slow import due to per-object dependency graph updates |
| Embedded subset fonts | Text using embedded subset fonts may not render correctly |
| PyMuPDF required | Release ZIPs bundle PyMuPDF; source/dev installs can use the preferences-panel installer |
| Legacy hosts | Blender/Python combinations outside the listed compatibility matrix are expected-only until verified |

## Import report / scale trust

Imports emit `import_report.json` (`bcs.import_report/1.1`) with optional `extra.resolved_scale`.

- Use `factor` only when `confidence >= 0.70` and `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown.

## Bad-PDF open gate

Blender refuses bad PDFs at open time (**fail closed**). SketchUp may fail open on rare gate errors; messages are aligned, detection parity is not.

## License

MIT -- Copyright (c) 2024-2026 BlueCollar Systems

See [LICENSE](LICENSE) for full text.

---

AI-assisted development by Claude (Anthropic).
