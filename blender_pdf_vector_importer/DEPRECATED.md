# Deprecated — legacy CLI package

Superseded by the shipping add-on: `pdf_vector_importer/` (install from
`dist/Blender-PDF-Importer_v*.zip`).

This package remains for historical CLI/API references only. Its Blender adapter
is a compatibility wrapper around `pdf_vector_importer.bl_import_engine`; no
independent Blender geometry, text, image, material, or raster builders remain.
That routing is intentional: every host import must receive the shipping
engine's exact-representation controller, packed-asset handling, rollback,
post-stack verification, and mandatory delivery report.

Do not add host construction here. New work belongs in `pdf_vector_importer/`.
