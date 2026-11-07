import logging

import bpy
import mathutils
from bpy_extras.node_shader_utils import PrincipledBSDFWrapper, ShaderImageTextureWrapper

from .bfrespy.common import ResString
from .bfrespy.models.material import Material

log = logging.getLogger(__name__)

TEX_WRAPPER = {
    "_a0": "base_color_texture",
    "_r0": "roughness_texture",
    "_m0": "metallic_texture",
    "_n0": "normalmap_texture",
    "_e0": "emission_color_texture",
    "_op0": "alpha_texture",
}
TEXCOORD_SELECT = {
    "_ao0": "ao",
    "_cp0": "comppaint",
    "_e0": "emmmap",
    "_fm0": "sfxmask",
    "_m0": "mtlmap",
    "_r0": "rghmap",
    "_re0": "res0",
    "_re1": "res1",
    "_re2": "res2",
    "_su0": "teamcolormap",
    "_t0": "trsmap",  # unsure if this is opacity or trm
}
# Texture name suffixes that say what a texture is, mapped to a TEX_WRAPPER sampler.
# Newer games (e.g. Ocarina of Time on Switch 2) don't use fixed shader sampler names,
# but name their textures consistently.
TEX_NAME_SUFFIX = {
    "Alb": "_a0",
    "Nrm": "_n0",
    "Rgh": "_r0",
    "Mtl": "_m0",
    "Emc": "_e0",
    "Emm": "_e0",
}


def __slot_from_texture_name(tex_name: str) -> str | None:
    """Return the sampler a texture should use based on its name suffix, if it has a known one."""
    return TEX_NAME_SUFFIX.get(tex_name.rsplit("_", 1)[-1])


def import_material(fmat: Material, texture_dict, name_prefix) -> bpy.types.Material:
    """Import specified material from fmat."""
    mat = bpy.data.materials.new(name=name_prefix + fmat.name)
    mat.use_nodes = True
    mat_wrap = PrincipledBSDFWrapper(mat, is_readonly=False)
    __add_custom_properties(fmat, mat)

    mappingnode = None
    used_slots = set()
    unused_nodes: list[tuple[str, bpy.types.ShaderNodeTexImage]] = []
    # Samplers and texture references are parallel lists. Textures whose name says what they are
    # go first, so a texture placed by the shader's sampler names can't take their slot.
    textures = list(zip(fmat.samplers.keys(), fmat.texture_refs))
    textures.sort(key=lambda t: __slot_from_texture_name(t[1].name) is None)
    for sampler, tex_ref in textures:
        tex_name = tex_ref.name
        if tex_name in texture_dict:
            image = texture_dict[tex_name]
        else:
            image = bpy.data.images.get(name_prefix + tex_name) or bpy.data.images.get(tex_name)

        if image is None:
            log.warning("Missing Texture: '%s'", tex_name)
            continue

        tex_sampler_name = fmat.shader_assign.sampler_assigns.key_from_value(ResString(sampler))
        slot = __slot_from_texture_name(tex_name) or tex_sampler_name

        # Get the bpy Texture Wrapper from the sampler name. Each slot only takes one texture.
        tex_helper_name = TEX_WRAPPER.get(slot) if slot not in used_slots else None
        if tex_helper_name:
            used_slots.add(slot)
            tex_helper: ShaderImageTextureWrapper = getattr(mat_wrap, tex_helper_name)
            tex_helper.image = image
        else:
            # Add the texture as a node not connected to anything if no samplers match
            tex_node = mat.node_tree.nodes.new(type="ShaderNodeTexImage")
            log.info("Unused texture: %s", tex_name)
            tex_node.location = mathutils.Vector(mat_wrap._grid_to_location(1, -2 - len(unused_nodes)))
            tex_node.label = f"{tex_sampler_name} {tex_name}"
            tex_node.image = image
            set_uv_coords(mat, tex_sampler_name, tex_node)
            unused_nodes.append((tex_name, tex_node))
            continue

        # Mapping
        if mat.get("MP_tex_mtx0_mode") and fmat.shaderparams["tex_mtx0"].data:
            if mappingnode is None:
                loc = mat["MP_tex_mtx0_translation"]
                tex_helper.translation = mathutils.Vector((loc[0], loc[1], 0))

                rot = mat["MP_tex_mtx0_rotation"]
                tex_helper.rotation = mathutils.Vector((0, 0, rot))

                scale = mat["MP_tex_mtx0_scaling"]
                scalemode = mat["MP_tex_mtx0_mode"]
                # XXX: Needs improvement
                match scalemode:
                    case "MODE_MAYA":
                        scalevec = (1 / scale[0], 1 / scale[1], 1)
                    case _:
                        scalevec = (scale[0], scale[1], 1)
                tex_helper.scale = mathutils.Vector(scalevec)

                mappingnode = tex_helper.node_mapping
            else:
                mat.node_tree.links.new(mappingnode.outputs["Vector"], tex_helper.node_image.inputs["Vector"])

        set_uv_coords(mat, tex_sampler_name, tex_helper.node_image)

    match fmat.shader_assign.shader_archive_name:
        case "Hoian_UBER":
            __shader_hoian(mat, mat_wrap)

    __layer_masked_albedo(mat, mat_wrap, unused_nodes)
    __setup_alpha(fmat, mat, mat_wrap)

    return mat


def __layer_masked_albedo(mat, mat_wrap: PrincipledBSDFWrapper, unused_nodes):
    """Blend a second albedo texture under the first one using a mask texture.

    Used by e.g. eyes, which have an iris and a sclera albedo and a mask for where the iris is.
    """
    extra_albedos = [node for name, node in unused_nodes if name.endswith("_Alb")]
    masks = [node for name, node in unused_nodes if "Mask" in name]
    base_links = mat_wrap.node_principled_bsdf.inputs["Base Color"].links
    if len(extra_albedos) != 1 or len(masks) != 1 or not base_links:
        return

    tree = mat.node_tree
    mix = tree.nodes.new(type="ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.label = "Albedo Mask"
    mix.location = mathutils.Vector(mat_wrap._grid_to_location(0, 1))
    # The mask is white where the first albedo goes.
    tree.links.new(masks[0].outputs["Color"], mix.inputs[0])
    tree.links.new(extra_albedos[0].outputs["Color"], mix.inputs[6])
    tree.links.new(base_links[0].from_socket, mix.inputs[7])
    tree.links.new(mix.outputs[2], mat_wrap.node_principled_bsdf.inputs["Base Color"])


def __render_info(fmat: Material, name: str):
    """Return the first value of a render info, or None if the material doesn't have it."""
    info = fmat.renderinfos.try_get_value(name)
    if info is None or not info.data:
        return None
    return info.data[0]


def __apply_alpha_select(fmat: Material, mat: bpy.types.Material, principled):
    """Handle shaders that pick the alpha from a texture channel with a shader option.

    Some newer shaders (e.g. Ocarina of Time on Switch 2) have an "ns_alpha_select" option, where "2NN" means
    the first channel of texture NN. When the albedo doesn't come from a texture too, the material is a
    darkening overlay like the shadow over the eyes, so the texture is only used as a mask.
    """
    alpha_select = mat.get("SO_ns_alpha_select", "")
    albedo_select = mat.get("SO_ns_albedo_select", "")
    if not (len(alpha_select) == 3 and alpha_select.startswith("2") and alpha_select.isdigit()):
        return
    if albedo_select.startswith("2") or albedo_select in {"", "<default value>"}:
        return
    index = int(alpha_select[1:])
    if index >= len(fmat.texture_refs):
        return

    tex_name = fmat.texture_refs[index].name
    tree = mat.node_tree
    image_node = next(
        (n for n in tree.nodes if n.type == "TEX_IMAGE" and n.image and n.image.name.endswith(tex_name)), None
    )
    if image_node is None:
        return

    separate = tree.nodes.new(type="ShaderNodeSeparateColor")
    separate.location = principled.location.x - 250, principled.location.y - 550
    tree.links.new(image_node.outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs[0], principled.inputs["Alpha"])

    base_color = principled.inputs["Base Color"]
    for link in list(base_color.links):
        tree.links.remove(link)
    base_color.default_value = (0.0, 0.0, 0.0, 1.0)


def __setup_alpha(fmat: Material, mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Use the base color texture's alpha for transparency, based on the material's render state."""
    mode = __render_info(fmat, "gsys_render_state_mode")
    blend_mode = __render_info(fmat, "gsys_render_state_blend_mode")
    alpha_test = mode == "mask" or __render_info(fmat, "gsys_alpha_test_enable") == "true"
    translucent = mode == "translucent" or (mode == "custom" and blend_mode not in {None, "none"})
    if not (alpha_test or translucent):
        return

    if __render_info(fmat, "gsys_render_state_display_face") == "front":
        mat.use_backface_culling = True

    tree = mat.node_tree
    principled = mat_wrap.node_principled_bsdf
    legacy = not hasattr(mat, "surface_render_method")  # Blender < 4.2

    # Additive blending (e.g. fake eye highlights): only light gets added on top of what's behind.
    rgb_src = __render_info(fmat, "gsys_color_blend_rgb_src_func")
    rgb_dst = __render_info(fmat, "gsys_color_blend_rgb_dst_func")
    if translucent and rgb_dst == "one" and rgb_src in {"one", "src_alpha"}:
        emission_input = principled.inputs.get("Emission Color") or principled.inputs.get("Emission")
        for color_input in (emission_input, principled.inputs["Base Color"]):
            if color_input is not None and color_input.is_linked:
                color_source = color_input.links[0].from_socket
                break
        else:
            return
        emission = tree.nodes.new(type="ShaderNodeEmission")
        transparent = tree.nodes.new(type="ShaderNodeBsdfTransparent")
        add = tree.nodes.new(type="ShaderNodeAddShader")
        emission.location = principled.location.x + 300, principled.location.y - 300
        transparent.location = principled.location.x + 300, principled.location.y - 450
        add.location = principled.location.x + 500, principled.location.y - 350
        tree.links.new(color_source, emission.inputs["Color"])
        tree.links.new(emission.outputs["Emission"], add.inputs[0])
        tree.links.new(transparent.outputs["BSDF"], add.inputs[1])
        tree.links.new(add.outputs["Shader"], mat_wrap.node_out.inputs["Surface"])
        if legacy:
            mat.blend_method = "BLEND"
            mat.shadow_method = "NONE"
        else:
            mat.surface_render_method = "BLENDED"
        return

    alpha_input = principled.inputs["Alpha"]
    __apply_alpha_select(fmat, mat, principled)
    if not alpha_input.is_linked:
        # Take the alpha from the image plugged into Base Color, if it has one.
        base_links = principled.inputs["Base Color"].links
        image_node = base_links[0].from_node if base_links else None
        if image_node is None or image_node.type != "TEX_IMAGE" or image_node.image is None:
            return
        if image_node.image.alpha_mode == "NONE" or image_node.image.channels < 4:
            return
        tree.links.new(image_node.outputs["Alpha"], alpha_input)

    threshold = __render_info(fmat, "gsys_alpha_test_value")
    if threshold is None:
        param = fmat.shaderparams.try_get_value("gsys_alpha_test_ref_value")
        threshold = param.data if param is not None else 0.5

    if translucent:
        if legacy:
            mat.blend_method = "BLEND"
            mat.shadow_method = "HASHED"
        else:
            mat.surface_render_method = "BLENDED"
    elif legacy:
        mat.blend_method = "CLIP"
        mat.shadow_method = "CLIP"
        mat.alpha_threshold = threshold
    else:
        # EEVEE Next has no alpha clip mode, so make the cutout hard with a math node like the game does.
        source = alpha_input.links[0].from_socket
        clip = tree.nodes.new(type="ShaderNodeMath")
        clip.operation = "GREATER_THAN"
        clip.label = "Alpha Test"
        clip.inputs[1].default_value = threshold
        clip.location = principled.location.x - 200, principled.location.y - 400
        tree.links.new(source, clip.inputs[0])
        tree.links.new(clip.outputs["Value"], alpha_input)
        mat.surface_render_method = "DITHERED"


def set_uv_coords(mat: bpy.types.Material, sampler: str, tex_node: bpy.types.ShaderNodeTexImage):
    selector = TEXCOORD_SELECT.get(sampler)
    if selector is None:
        return
    uv_id = mat.get("SO_texcoord_select_" + selector)
    if uv_id and uv_id.isdigit() and uv_id != "0":
        tree = mat.node_tree

        uv_node = tree.nodes.new(type="ShaderNodeUVMap")
        uv_node.uv_map = f"_u{uv_id}"
        socket_src = uv_node.outputs["UV"]

        tree.links.new(socket_src, tex_node.inputs["Vector"])


def __add_custom_properties(fmat, mat: bpy.types.Material):
    """Add render/shader/material parameters and sampler list
    as custom properties on the Blender material object.
    """
    # Make Render Info
    for name, param in fmat.renderinfos.items():
        val = param.data
        if len(param.data) == 1:
            val = val[0]
        mat["RI_" + name] = val

    # Make Material Params
    for name, param in fmat.shaderparams.items():
        pname = "MP_" + name
        if param.type.name in {"TEX_SRT", "TEX_SRT_EX", "SRT2D", "SRT3D"}:
            if param.type.name in {"TEX_SRT", "TEX_SRT_EX"}:
                mat[pname + "_mode"] = param.data.mode.name
            mat[pname + "_scaling"] = param.data.scaling
            mat[pname + "_rotation"] = param.data.rotation
            mat[pname + "_translation"] = param.data.translation

            propman = mat.id_properties_ui(pname + "_scaling")
            propman.update(subtype="XYZ")
            propman = mat.id_properties_ui(pname + "_rotation")
            if param.type.name == "SRT3D":
                propman.update(subtype="EULER")
            else:
                propman.update(subtype="ANGLE")
            propman = mat.id_properties_ui(pname + "_translation")
            propman.update(subtype="XYZ")
        else:
            mat[pname] = param.data
            if param.type.name == "FLOAT4":
                propman = mat.id_properties_ui(pname)
                propman.update(subtype="COLOR_GAMMA", min=0, max=1, step=0.01)

    for name, val in fmat.shader_assign.shaderoptions.items():
        mat["SO_" + name] = str(val)

    mat["samplers"] = {key: str(value) for key, value in fmat.shader_assign.sampler_assigns.items()}


def __shader_hoian(mat, mat_wrap: PrincipledBSDFWrapper):
    # Small changes for Splatoon 3 shaders
    if mat.get("SO_enable_albedo_tex") == "false":
        mat_wrap.base_color = mat["MP_albedo_color"][:3]

    if mat.get("SO_enable_roughness_map") != "true":
        mat_wrap.roughness = mat["MP_roughness"]

    if mat.get("SO_enable_metalness_map") != "true":
        mat_wrap.metallic = mat["MP_metalness"]

    if mat.get("SO_enable_opacity_tex") != "true":
        mat_wrap.alpha = mat["MP_opacity"]

    if mat.get("SO_enable_emission_map") != "true":
        mat_wrap.emission_color = mat["MP_emission_color"][:3]

    if mat.get("SO_emission_color_type") == "1":
        mat_wrap.emission_color_texture.image = mat_wrap.base_color_texture.image

    if mat.get("SO_enable_emission") == "true":
        mat_wrap.emission_strength = mat["MP_emission_intensity"]
    else:
        # for blender 3.6
        mat_wrap.emission_strength = 0.0
