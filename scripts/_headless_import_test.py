"""Headless Blender smoke test for pdf_vector_importer."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Prefer repo addon over any user-installed copy (API drift breaks headless QA).
for p in (REPO, os.path.join(REPO, "pdf_vector_importer")):
    if p not in sys.path:
        sys.path.insert(0, p)

PDF = os.environ.get(
    "TEST_PDF",
    r"C:\Users\Rowdy Payton\Desktop\PDFTest Files\TX_Alvord_20220525_TM_geo.pdf",
)


def main():
    import bpy
    import importlib

    # Drop stale installed addon modules so repo code wins.
    for name in list(sys.modules):
        if name == "pdf_vector_importer" or name.startswith("pdf_vector_importer."):
            del sys.modules[name]

    bl_import_engine = importlib.import_module("pdf_vector_importer.bl_import_engine")

    print("pdf_vector_importer from:", bl_import_engine.__file__)

    if not os.path.isfile(PDF):
        print("SKIP: no test pdf at", PDF)
        return 0

    stats = bl_import_engine.import_pdf(
        PDF,
        config={"mode": "auto", "pages": "1"},
        context=None,
    )
    print("import stats:", stats)
    objs = [o.name for o in bpy.data.objects if o.name.startswith("PDF")]
    print("pdf objects:", len(objs), objs[:10])
    return 0 if stats.get("primitives", 0) > 0 or stats.get("images", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
