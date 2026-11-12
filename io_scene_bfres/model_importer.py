import logging

import bpy
import mathutils

from .material_importer import apply_texture_cutout, import_material
from .mesh_importer import import_mesh
from .skeleton_importer import import_fskl

log = logging.getLogger(__name__)

# How far outline shells are pushed out. The game sets this at runtime (OutlineThickness is 0 in the files).
OUTLINE_THICKNESS = 0.01


def add_outline_thickness(mesh_object):
    """Push an outline shell (an inverted hull) out along its normals so it shows around the model.

    Left where it is, it lies right on the model's surface, where Cycles can't tell the two apart.
    The shell's normals are smoothed across its hard edges, so it's pushed along those to stay closed.
    """
    modifier = mesh_object.modifiers.new(name="Outline Thickness", type="DISPLACE")
    modifier.mid_level = 0.0
    modifier.strength = OUTLINE_THICKNESS
    mesh = mesh_object.data
    if getattr(mesh, "has_custom_normals", False) and (bpy.app.version >= (4, 1) or mesh.use_auto_smooth):
        modifier.direction = "CUSTOM_NORMAL"
    else:
        modifier.direction = "NORMAL"


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

            if mesh_object.active_material.get("RI_IsOutline") == 1:
                add_outline_thickness(mesh_object)

        # Some alpha tests can only be told apart from other uses of the alpha by the meshes.
        for mat, objects in material_objects.items():
            apply_texture_cutout(mat, objects)

        cut_stencil_holes(material_objects)


def cut_stencil_holes(material_objects):
    """Cut the shapes that stencil masks reveal things through out of the meshes in front of them.

    Whispy Woods (Kirby's Return to Dream Land Deluxe) has eye sockets inside his trunk. The game marks the eyes
    with closed meshes that only write to the stencil buffer, then draws the sockets where they're behind the
    trunk ("greater" depth test) inside those marks. Blender has no stencil buffer, so the marks are cut out of
    the opaque meshes they overlap with Boolean modifiers instead.
    """

    def ri(mat, name):
        return mat.get("RI_" + name)

    def is_stencil_mask(mat):
        return (
            ri(mat, "channelMask") == "zero"
            and ri(mat, "stencilTestEnabled") == 1
            and ri(mat, "stencilFrontDepthPassOperation") == "replace"
        )

    def draws_through(mat):
        return (
            ri(mat, "stencilTestEnabled") == 1
            and ri(mat, "stencilFrontComparisonFunction") == "equal"
            and ri(mat, "depthComparisonFunction") == "greater"
        )

    if not any(draws_through(mat) for mat in material_objects):
        return
    masks = [obj for mat, objects in material_objects.items() if is_stencil_mask(mat) for obj in objects]
    targets = [
        obj
        for mat, objects in material_objects.items()
        if ri(mat, "depthWriteEnabled") == 1 and ri(mat, "channelMask") != "zero" and not draws_through(mat)
        for obj in objects
    ]

    def bounds(obj):
        corners = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
        return [min(c[i] for c in corners) for i in range(3)], [max(c[i] for c in corners) for i in range(3)]

    cut_targets = []
    for mask in masks:
        mask_lo, mask_hi = bounds(mask)
        for target in targets:
            lo, hi = bounds(target)
            if any(lo[i] > mask_hi[i] or hi[i] < mask_lo[i] for i in range(3)):
                continue
            log.info("Cutting stencil mask '%s' out of '%s'", mask.name, target.name)
            modifier = target.modifiers.new(name=f"Stencil {mask.name}", type="BOOLEAN")
            modifier.operation = "DIFFERENCE"
            modifier.solver = "EXACT"  # The others can't cut meshes that aren't closed, like the trunk
            # Bits of the mask that the open mesh can't tell are outside it stay invisible like the mask.
            modifier.material_mode = "TRANSFER"
            modifier.object = mask
            if target not in cut_targets:
                cut_targets.append(target)

    for target in cut_targets:
        __keep_normals(target)


def __keep_normals(target):
    """Give a mesh cut by Booleans its original normals back.

    The custom normals of the faces the Boolean splits come out wrong, streaking the shading around the cut, so
    they're copied from a hidden, uncut copy of the mesh (following the same armature).
    """
    if not target.data.has_custom_normals:
        return
    source = bpy.data.objects.new(name=f"{target.name} Normals", object_data=target.data)
    for collection in target.users_collection:
        collection.objects.link(source)
    source.parent = target.parent
    source.matrix_parent_inverse = target.matrix_parent_inverse
    for modifier in target.modifiers:
        if modifier.type == "ARMATURE":
            armature = source.modifiers.new(name=modifier.name, type="ARMATURE")
            armature.object = modifier.object
            armature.use_bone_envelopes = False
            armature.use_vertex_groups = True
    source.hide_viewport = True
    source.hide_render = True

    transfer = target.modifiers.new(name="Original Normals", type="DATA_TRANSFER")
    transfer.object = source
    transfer.use_loop_data = True
    transfer.data_types_loops = {"CUSTOM_NORMAL"}
    transfer.loop_mapping = "POLYINTERP_NEAREST"
