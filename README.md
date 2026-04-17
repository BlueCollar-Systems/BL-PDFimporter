# PDF Vector Importer for Blender

**BUILT. NOT BOUGHT.** -- BlueCollar Systems

Import PDF vector drawings as native Blender geometry (Curves, Collections, Materials).
Powered by the pdfcadcore shared extraction library and PyMuPDF.

## Features

- **7 Import Presets** -- Fast Preview, General Vector, Technical Drawing, Shop Drawing, Raster+Vectors, Raster Only, Max Fidelity
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

1. Build a release zip:
   ```bash
   python build_release.py
   ```
2. In Blender: **Edit > Preferences > Add-ons > Install...**
3. Choose `dist/pdf_vector_importer_v1.0.0.zip`
4. Enable **PDF Vector Importer**
5. If prompted, click **Install PyMuPDF** in the addon preferences panel

### Manual Install

Copy the `pdf_vector_importer/` directory into your Blender addons path:
- Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\`
- macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/`
- Linux: `~/.config/blender/<version>/scripts/addons/`

## Usage

After enabling the addon:

1. **File > Import > PDF Vector (.pdf)**
2. Select a PDF file
3. Choose a preset and adjust options in the import panel
4. Click **Import PDF Vector**

Geometry is grouped into collections by page and (optionally) by source layer or color.

## Compatibility

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
- Python 3.10+
- PyMuPDF >=1.24,<2.0 (auto-installed via addon preferences)

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
| PyMuPDF required | PyMuPDF must be installable in Blender's bundled Python (see dependency manager) |
| Legacy hosts | Blender/Python combinations outside the listed compatibility matrix are expected-only until verified |

## License

MIT -- Copyright (c) 2024-2026 BlueCollar Systems

See [LICENSE](LICENSE) for full text.

---

AI-assisted development by Claude (Anthropic).
