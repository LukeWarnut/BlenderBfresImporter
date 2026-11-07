import logging

from .material_importer import apply_texture_cutout, import_material
from .mesh_importer import import_mesh
from .skeleton_importer import import_fskl

log = logging.getLogger(__name__)


class ModelImporter:
    def __init__(self, parent):
        self.operator = parent.operator
        self.texture_map = parent.texture_map

    def convert_fmdl(self, fmdl, collection):
        self.fskl_ob = import_fskl(fmdl, fmdl.skeleton, collection, self.operator.copy_bone_transforms)

        # Import all Materials for this model
        material_map = {}
        for i, fmat in enumerate(fmdl.materials.values()):
            log.info("Importing material %3d / %3d...", i + 1, len(fmdl.materials))
            mat = import_material(
                fmat,
                self.texture_map,
                self.operator.name_prefix,
                getattr(self.operator, "wet_effect", False),
                report=self.operator.report,
            )
            material_map[i] = mat

        # Go through the polygons in this model and create mesh objects representing them.
        material_objects = {mat: [] for mat in material_map.values()}
        for i, fshp in enumerate(fmdl.shapes.values()):
            log.info("Importing shape %3d / %3d '%s'...", i + 1, len(fmdl.shapes), fshp.name)
            # Some games keep placeholder shapes without any geometry (no meshes, empty vertex buffer).
            if not fshp.meshes:
                log.warning("Skipping shape '%s': it has no meshes", fshp.name)
                continue
            mesh_object = import_mesh(fmdl, fshp, self.operator.lod_index, self.operator.custom_normals)
            mesh_object.data.materials.append(material_map[fshp.material_idx])
            material_objects[material_map[fshp.material_idx]].append(mesh_object)

            # Parent to the empty FMDL object and link it to the scene.
            collection.objects.link(mesh_object)
            mesh_object.parent = self.fskl_ob

            # Add armature modifier
            modifier = mesh_object.modifiers.new(name=fshp.name, type="ARMATURE")
            modifier.object = self.fskl_ob
            modifier.use_bone_envelopes = False
            modifier.use_vertex_groups = True

        # Some alpha tests can only be told apart from other uses of the alpha by the meshes.
        for mat, objects in material_objects.items():
            apply_texture_cutout(mat, objects)
