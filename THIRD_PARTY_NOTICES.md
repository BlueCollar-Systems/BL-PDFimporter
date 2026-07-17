# Third-Party Notices

This package includes third-party components in `pdf_vector_importer/lib`,
including PyMuPDF and MuPDF runtime files.

## PyMuPDF / MuPDF

- Project: PyMuPDF
- Upstream: https://github.com/pymupdf/PyMuPDF
- License model: AGPL-3.0-or-later or commercial licensing (Artifex)
- Notes: Release ZIPs bundle runtime files for convenience. If you
  redistribute this package, preserve upstream notices and comply with
  applicable third-party license terms.

For complete third-party metadata in this package, see:

- `pdf_vector_importer/lib/pymupdf-*.dist-info/METADATA`
- `pdf_vector_importer/lib/pymupdf-*.dist-info/COPYING`

## FontTools

- Project: FontTools
- Upstream: https://github.com/fonttools/fonttools
- Bundled version: 4.60.2, official `py3-none-any` wheel (Python 3.9+)
- License: MIT
- Purpose: repairs exact PDF Unicode-to-glyph mappings in embedded font
  programs so native Text and 3D Text remain the requested representation.

The bundled notices are preserved at:

- `pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE`
- `pdf_vector_importer/lib/fonttools-4.60.2.dist-info/licenses/LICENSE.external`
