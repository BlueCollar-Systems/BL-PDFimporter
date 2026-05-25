# Host Compatibility — PDF Vector Importer (Blender)

Modes are extraction **strategy** (Auto / Vector / Raster / Hybrid), not quality tiers.
Every mode uses consolidated maximum-fidelity parameters (BCS-ARCH-001).

**GUI:** Single **professional import** flow — **Auto** per page by default. Expand **Advanced Options** to override strategy. CLI/batch retain `--mode` for scripting.

## Blender

| Blender | Bundled Python | PyMuPDF | Status |
|---------|----------------|---------|--------|
| 4.5 LTS | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 4.0–4.2 | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 3.6 LTS | 3.10 | >=1.24,<2.0 | ⚠️ Expected |
| 3.0–3.5 | 3.10 | >=1.24,<2.0 | ⚠️ Expected |
| 2.83–2.93 | 3.9 | legacy pin | ⚠️ Expected after legacy testing |
| 2.79 and earlier | | | ❌ Not supported |

`bl_info["blender"]` in the add-on currently declares **(3, 6, 0)** minimum for the packaged release zip; older 3.x may work when installed manually but is not release-gated.

### Text rendering (3D-capable host)

| Option | Blender result |
|--------|----------------|
| **Labels** | Font curve objects (editable text) |
| **3D Text** | Extruded / geometric text where supported |
| **Glyphs** | Per-character vector curves |
| **Geometry** | Text as mesh/curve outlines |

## CI coverage

GitHub Actions: Python **3.10, 3.11, 3.12**, pytest, BCS-ARCH mode smoke on synthetic PDFs.
