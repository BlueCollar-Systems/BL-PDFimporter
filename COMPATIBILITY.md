# Compatibility — PDF Vector Importer (Blender)

**Canonical path:** `C:\1PDF-Importer-Blender`  
Modes are extraction **strategy** (Auto / Vector / Raster / Hybrid), not quality tiers.

---

## Minimum host version

**Blender 3.0** (`bl_info["blender"]` minimum). **Recommended: Blender 3.6 LTS or 5.x**.

## Oldest tested

| Host | Status |
|------|--------|
| Blender 5.2 LTS | ✅ Requested-representation host acceptance (2026-07-16) |
| Blender 5.0–5.1 | ✅ Smoke-tested (v1.0.42+ cp310-abi3 wheel) |
| Blender 4.5 LTS / 4.0–4.2 | ⚠️ Expected |
| Blender 3.6 LTS | ⚠️ Expected |
| Blender 3.0–3.5 | ⚠️ Expected after manual PyMuPDF install |
| Blender 2.83–2.93 | ⚠️ Legacy testing only |
| Blender 2.79 and earlier | ❌ Not supported |

## Ruby / Python ABI

| Runtime | Notes |
|---------|-------|
| **Blender bundled Python** | 3.10 (3.6 LTS) through 3.13 (5.x) |
| cp310-abi3 PyMuPDF wheel | v1.0.42+ for Blender 5.x |
| Ruby | Not used |

## Bundled dependencies

| Dependency | Release ZIP | Fallback |
|------------|-------------|----------|
| PyMuPDF (>=1.24, &lt;2.0) | ✅ Vendored under `pdf_vector_importer/lib/` | Preferences → **Install PyMuPDF** |
| pdfcadcore | ✅ In add-on | Same |

No system Python or pip required when release ZIP vendored wheel loads.

## Legacy hardware notes

- **Text** and **3D Text** remain editable `FONT` objects. **Glyphs** and
  **Geometry** can create high curve/mesh counts; on **&lt; 8 GB RAM** PCs,
  import one page first when fixed outlines are required.
- Blender has no persistent, model-scaled, renderable Label entity. A Labels
  request records that item-specific host limitation before trying Text.
- Headless import validated; interactive UI still needs human confirmation (T-01).
- Glyphs produces real Blender `CURVE` outline data; Geometry produces real
  Blender `MESH` data. They are not aliases.

## Preflight command

```powershell
cd C:\1PDF-Importer-Blender
python preflight_check.py
python preflight_check.py --diagnostics
```

In Blender: enable add-on → Preferences → **Install PyMuPDF** if import fails on 5.x.

Headless diagnostics:

```powershell
blender --background --python-expr "import addon_utils; addon_utils.enable('pdf_vector_importer'); from pdf_vector_importer.dependency_manager import print_diagnostics; print_diagnostics()"
```

---

## Blender version matrix

| Blender | Bundled Python | PyMuPDF | Status |
|---------|----------------|---------|--------|
| 5.2 LTS | 3.13 | >=1.24,<2.0 | ✅ Text/3D/Glyphs/Geometry/Raster host acceptance |
| 5.0–5.1 | 3.12–3.13 | >=1.24,<2.0 | ✅ v1.0.42+ cp310-abi3 |
| 4.5 LTS | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 4.0–4.2 | 3.11 | >=1.24,<2.0 | ⚠️ Expected |
| 3.6 LTS | 3.10 | >=1.24,<2.0 | ⚠️ Expected |
| 3.0–3.5 | 3.10 | >=1.24,<2.0 | ⚠️ Expected after manual install |

### Blender 5.x PyMuPDF bootstrap (v1.0.42+)

1. Vendored **cp310-abi3** wheel under `pdf_vector_importer/lib/`
2. Self-heal for missing `pymupdf/extra.py`
3. Preferences → **Install PyMuPDF**

### Text rendering

| Option | Blender result |
|--------|----------------|
| **Labels** | Persistent model label when supported; currently item-specific impossible in Blender, then Text fallback |
| **Text** | Flat editable `FONT` using the exact embedded PDF font program |
| **3D Text** | Extruded editable `FONT` using the exact embedded PDF font program |
| **Glyphs** | Non-editable real Blender `CURVE` outlines |
| **Geometry** | Non-editable real Blender `MESH` geometry |
| **Raster** | Aligned image patch clipped from the individual source text span |

The structural modes do not search Windows/macOS/Linux fonts by name. When an
exact embedded font cannot be used, only item-specific proven fallback is
allowed. Every attempt and final entity is recorded under
`extra.text_delivery`; an unverified terminal raster is a loud failure.

## CI coverage

GitHub Actions: Python **3.9–3.12**, `pdfcadcore_sync_check.py`, pytest, BCS-ARCH mode smoke.
