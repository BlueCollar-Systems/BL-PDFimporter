"""Blender add-on entrypoint + package exports."""
from __future__ import annotations

from .importer import run_import  # noqa: F401

bl_info = {
    "name": "PDF Vector Importer for Blender",
    "author": "BlueCollar Systems",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "File > Import > PDF Vector Drawing (.pdf)",
    "description": "Import vector geometry, text, and images from PDFs",
    "category": "Import-Export",
}


try:  # pragma: no cover - Blender runtime only
    import bpy
    from bpy.props import BoolProperty, EnumProperty, StringProperty
    from bpy_extras.io_utils import ImportHelper

    from .adapters.blender_adapter import BlenderImportOptions, import_into_blender

    class IMPORT_SCENE_OT_pdf_vector(bpy.types.Operator, ImportHelper):
        bl_idname = "import_scene.bc_pdf_vector"
        bl_label = "Import PDF Vector"
        bl_options = {"REGISTER", "UNDO"}

        filename_ext = ".pdf"
        filter_glob: StringProperty(default="*.pdf", options={"HIDDEN"})

        preset: EnumProperty(
            name="Preset",
            items=[
                ("fast", "Fast", "Fast preview import"),
                ("general", "General", "Balanced general import"),
                ("technical", "Technical", "Technical drawing optimization"),
                ("shop", "Shop", "Shop drawing optimization"),
                ("max", "Max Fidelity", "Highest fidelity import"),
            ],
            default="general",
        )

        pages: StringProperty(
            name="Pages",
            default="1",
            description="Page selection: 1, 1,3-5, or all",
        )

        import_text: BoolProperty(name="Import Text", default=True)
        import_images: BoolProperty(name="Import Images", default=True)
        detect_arcs: BoolProperty(name="Reconstruct Arcs", default=True)
        group_by_layer: BoolProperty(name="Group By Layer", default=True)
        group_by_color: BoolProperty(name="Tag Color Groups", default=True)

        def execute(self, context):
            options = BlenderImportOptions(
                pages=self.pages,
                import_text=self.import_text,
                import_images=self.import_images,
                detect_arcs=self.detect_arcs,
                group_by_layer=self.group_by_layer,
                group_by_color=self.group_by_color,
            )

            extraction = import_into_blender(self.filepath, preset=self.preset, options=options)
            summary = extraction.summary()
            self.report(
                {"INFO"},
                f"Imported {summary['primitives']} primitives, "
                f"{summary['text_items']} text items, {summary['images']} images",
            )
            return {"FINISHED"}

        def draw(self, context):
            layout = self.layout
            layout.prop(self, "preset")
            layout.prop(self, "pages")
            layout.prop(self, "import_text")
            layout.prop(self, "import_images")
            layout.prop(self, "detect_arcs")
            layout.prop(self, "group_by_layer")
            layout.prop(self, "group_by_color")


    def menu_func_import(self, context):
        self.layout.operator(IMPORT_SCENE_OT_pdf_vector.bl_idname,
                             text="PDF Vector Drawing (.pdf)")


    _CLASSES = (IMPORT_SCENE_OT_pdf_vector,)


    def register():
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)

except Exception:  # pragma: no cover - non-Blender runtime

    def register():
        return None


    def unregister():
        return None
