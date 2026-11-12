import logging
import re

import bpy
import mathutils
import numpy as np
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
    "_mra": "metallic_texture",  # packed metalness, roughness and AO, split by __split_mra
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
    # Kirby and the Forgotten Land, where every sampler is a generic "albedoN"
    "BaseColor": "_a0",
    "Normal": "_n0",
    "MRA": "_mra",
}


def __slot_from_texture_name(tex_name: str) -> str | None:
    """Return the sampler a texture should use based on its name suffix, if it has a known one."""
    # Kirby and the Forgotten Land ends texture names with a hash, e.g. "BodyC_BaseColor.59531894".
    return TEX_NAME_SUFFIX.get(tex_name.split(".", 1)[0].rsplit("_", 1)[-1])


def import_material(fmat: Material, texture_dict, name_prefix, wet_effect=False, report=None) -> bpy.types.Material:
    """Import specified material from fmat."""
    mat = bpy.data.materials.new(name=name_prefix + fmat.name)
    mat.use_nodes = True
    mat_wrap = PrincipledBSDFWrapper(mat, is_readonly=False)
    __add_custom_properties(fmat, mat)

    mappingnode = None
    used_slots = set()
    unused_nodes: list[tuple[str, bpy.types.ShaderNodeTexImage]] = []
    missing_textures: list[str] = []
    # Samplers and texture references are parallel lists. Textures whose name says what they are
    # go first, so a texture placed by the shader's sampler names can't take their slot. Of those, the
    # ones named after the material come first: layered materials (e.g. Kirby and the Forgotten Land's
    # stages) also use other materials' textures, like a dirt overlay, before their own.
    def priority(texture) -> int:
        name = texture[1].name
        if __slot_from_texture_name(name) is None:
            return 2
        return 0 if name.startswith(fmat.name + "_") else 1

    textures = list(zip(fmat.samplers.keys(), fmat.texture_refs))
    textures.sort(key=priority)
    for sampler, tex_ref in textures:
        tex_name = tex_ref.name
        if tex_name in texture_dict:
            image = texture_dict[tex_name]
        else:
            image = bpy.data.images.get(name_prefix + tex_name) or bpy.data.images.get(tex_name)

        if image is None:
            log.warning("Missing Texture: '%s'", tex_name)
            missing_textures.append(tex_name)
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

    __unnamed_albedo(fmat, mat, mat_wrap, unused_nodes)
    __opacity_mask(mat, mat_wrap, unused_nodes)
    __layer_masked_albedo(mat, mat_wrap, unused_nodes)
    __face_pattern(mat, mat_wrap, unused_nodes)
    __second_layer(mat, mat_wrap, unused_nodes)
    __split_mra(mat, mat_wrap)
    __split_packed_roughness(mat, mat_wrap, wet_effect)
    # Alpha first: it takes the alpha from the image plugged into Base Color, which the graphs replace.
    missing_albedo = any(__slot_from_texture_name(name) == "_a0" for name in missing_textures)
    __setup_alpha(fmat, mat, mat_wrap, missing_albedo, report)
    __outline(fmat, mat, mat_wrap)
    __apply_known_graphs(mat, mat_wrap)
    __hide_if_no_color_written(fmat, mat, mat_wrap)

    return mat


# Shader expression graphs (Ocarina of Time on Switch 2) whose effect on the albedo is approximated here.
# The graphs themselves are only stored as a hash, so this was worked out from the textures and parameters.
EYE_SURFACE_GRAPH = "2937883399"  # ns_overwrite_surface_graph of character eyes (Mt_Eye_Inner)
STRAND_EXPRESSION_GRAPH = "2221926229"  # ns_expression_evaluate_graph of eyebrows and eyelashes


def __apply_known_graphs(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    if mat.get("SO_ns_overwrite_surface_graph") == EYE_SURFACE_GRAPH:
        __eye_graph(mat, mat_wrap)
    if mat.get("SO_ns_expression_evaluate_graph") == STRAND_EXPRESSION_GRAPH:
        __strand_graph(mat, mat_wrap)


def __param_color(mat: bpy.types.Material, name: str):
    value = mat.get("MP_" + name)
    if value is None or len(value) < 3:
        return None
    return (value[0], value[1], value[2], 1.0)


def __eye_graph(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Size and recolor the shared iris texture like the character eye shader does.

    - ns_param_scalar3 scales the iris and its mask around the middle of the UVs (bigger = smaller iris).
    - ns_param_color2 is a hue/saturation/value adjustment of the iris, with 0.5 meaning no change.
    """
    tree = mat.node_tree
    mix = next((n for n in tree.nodes if n.type == "MIX" and n.label == "Albedo Mask"), None)
    if mix is None or not mix.inputs[0].is_linked or not mix.inputs[7].is_linked:
        return
    mask_node = mix.inputs[0].links[0].from_node
    iris_socket = mix.inputs[7].links[0].from_socket
    iris_node = iris_socket.node

    scale = mat.get("MP_ns_param_scalar3")
    if scale is not None and mask_node.type == "TEX_IMAGE" and iris_node.type == "TEX_IMAGE":
        vector_links = iris_node.inputs["Vector"].links
        if vector_links:
            uv_socket = vector_links[0].from_socket
        else:
            uv_socket = tree.nodes.new(type="ShaderNodeTexCoord").outputs["UV"]
            uv_socket.node.location = iris_node.location.x - 500, iris_node.location.y
        mapping = tree.nodes.new(type="ShaderNodeMapping")
        mapping.label = "Iris Scale"
        mapping.location = iris_node.location.x - 250, iris_node.location.y
        mapping.inputs["Scale"].default_value = (scale, scale, 1.0)
        mapping.inputs["Location"].default_value = (0.5 - 0.5 * scale, 0.5 - 0.5 * scale, 0.0)
        tree.links.new(uv_socket, mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], iris_node.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], mask_node.inputs["Vector"])
        # The scaled UVs go outside 0-1, where the mask has to stay black instead of repeating.
        iris_node.extension = "EXTEND"
        mask_node.extension = "EXTEND"

    hsv = __param_color(mat, "ns_param_color2")
    if hsv is not None:
        node = tree.nodes.new(type="ShaderNodeHueSaturation")
        node.label = "Iris Color"
        node.location = mix.location.x - 200, mix.location.y - 250
        node.inputs["Hue"].default_value = hsv[0]
        node.inputs["Saturation"].default_value = hsv[1] * 2
        node.inputs["Value"].default_value = hsv[2] * 2
        tree.links.new(iris_socket, node.inputs["Color"])
        tree.links.new(node.outputs["Color"], mix.inputs[7])


def __strand_graph(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Color eyebrows and eyelashes.

    Their albedo texture is a grayscale gradient along each strand, which the shader maps from
    ns_param_color2 (root) to ns_param_color3 (tip).
    """
    root = __param_color(mat, "ns_param_color2")
    tip = __param_color(mat, "ns_param_color3")
    base_color = mat_wrap.node_principled_bsdf.inputs["Base Color"]
    if root is None or tip is None or not base_color.is_linked:
        return

    tree = mat.node_tree
    source = base_color.links[0].from_socket
    separate = tree.nodes.new(type="ShaderNodeSeparateColor")
    separate.location = source.node.location.x + 300, source.node.location.y + 200
    gradient = tree.nodes.new(type="ShaderNodeMix")
    gradient.data_type = "RGBA"
    gradient.label = "Strand Color"
    gradient.location = separate.location.x + 200, separate.location.y
    gradient.inputs[6].default_value = root
    gradient.inputs[7].default_value = tip
    tree.links.new(source, separate.inputs["Color"])
    tree.links.new(separate.outputs[0], gradient.inputs[0])
    tree.links.new(gradient.outputs[2], base_color)


# Frames of a texture pattern animation (e.g. eye expressions) are named "<texture>.00", "<texture>.01", ...
PATTERN_FRAME = re.compile(r"\.\d\d$")


def __unnamed_albedo(fmat: Material, mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, unused_nodes):
    """Use the first sampler's texture as the base color when no texture is named like one.

    Kirby's Return to Dream Land Deluxe doesn't always name the albedo "<name>_BaseColor": e.g. Bronto Burt's
    is "Bront.00" (its frames swap in other versions of the whole texture, like blinking eyes), and many eyes,
    faces and props are the same. It's still in the first sampler and stored as sRGB, unlike the masks and
    noise some effect materials have there instead.
    """
    if __render_info(fmat, "renderPass") is None:  # HAL's games (Kirby) only, their samplers are all "albedoN"
        return
    if mat_wrap.node_principled_bsdf.inputs["Base Color"].is_linked:
        return
    tex_ref = dict(zip(fmat.samplers.keys(), fmat.texture_refs)).get("_a0")
    if tex_ref is None:
        return
    name = tex_ref.name
    base_name = name.split(".", 1)[0]
    if base_name.startswith("Env") or "Mask" in base_name:  # sphere mapped reflections, face pattern masks
        return
    entry = next((entry for entry in unused_nodes if entry[0] == name), None)
    if entry is None or entry[1].image.colorspace_settings.name != "sRGB":
        return

    unused_nodes.remove(entry)
    image = entry[1].image
    mat.node_tree.nodes.remove(entry[1])
    mat_wrap.base_color_texture.image = image


def __opacity_mask(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, unused_nodes):
    """Use a grayscale "<name>_A" texture as the opacity.

    Bronto Burt's wings (Kirby's Return to Dream Land Deluxe) have one: each wing is see-through in the middle
    and solid towards the edge.
    """
    alpha_input = mat_wrap.node_principled_bsdf.inputs["Alpha"]
    masks = [node for name, node in unused_nodes if name.split(".", 1)[0].rsplit("_", 1)[-1] == "A"]
    if len(masks) != 1 or alpha_input.is_linked:
        return

    tree = mat.node_tree
    separate = tree.nodes.new(type="ShaderNodeSeparateColor")
    separate.label = "Opacity"
    separate.location = masks[0].location.x + 300, masks[0].location.y
    tree.links.new(masks[0].outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs[0], alpha_input)


def __face_pattern(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, unused_nodes):
    """Put the expression textures (eyes, mouth) of Kirby and the Forgotten Land characters on their face.

    These are projected with the first UV map, which goes outside 0-1 around the face, so the rest of the
    material's textures use the second one. Color frames are drawn over the base color using their alpha;
    the shape masks some characters use instead (e.g. Waddle Dee's eyes) are colored by the game's shader,
    so they're only given the right UV map.
    """
    patterns = [(name, node) for name, node in unused_nodes if PATTERN_FRAME.search(name)]
    if not patterns:
        return

    tree = mat.node_tree
    pattern_nodes = {node for _, node in patterns}
    for node in list(tree.nodes):
        if node.type == "TEX_IMAGE" and node not in pattern_nodes and node.image is not None:
            if __slot_from_texture_name(node.image.name) is not None:
                __set_uv_map(tree, node, "_u1")
    for _, node in patterns:
        node.extension = "CLIP"
        __set_uv_map(tree, node, "_u0")

    base_color = mat_wrap.node_principled_bsdf.inputs["Base Color"]
    albedos = [
        node
        for name, node in patterns
        if node.image.colorspace_settings.name == "sRGB"
        and not name.rsplit(".", 1)[0].endswith(("Normal", "Mask"))  # e.g. DeeEyeNormal.00, DeeEyeMask.00
    ]
    if len(albedos) != 1 or not base_color.is_linked:
        return
    mix = tree.nodes.new(type="ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.label = "Face Pattern"
    mix.location = mathutils.Vector(mat_wrap._grid_to_location(0, 1))
    tree.links.new(albedos[0].outputs["Alpha"], mix.inputs[0])
    tree.links.new(base_color.links[0].from_socket, mix.inputs[6])
    tree.links.new(albedos[0].outputs["Color"], mix.inputs[7])
    tree.links.new(mix.outputs[2], base_color)


def __second_layer(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, unused_nodes):
    """Blend in a second set of textures ("BaseColor2", "Normal2") where the vertex color's green is set.

    Kirby and the Forgotten Land uses this for parts of a model drawn with other textures than the rest, like
    Gigant Edge's fur collar, whose vertices have a green _c0 while the armor's are black.
    """
    layers = {}
    for name, node in unused_nodes:
        suffix = name.split(".", 1)[0].rsplit("_", 1)[-1]
        if suffix in {"BaseColor2", "Normal2"}:
            layers.setdefault(suffix, node)
    if not layers:
        return

    tree = mat.node_tree
    principled = mat_wrap.node_principled_bsdf
    targets = {"BaseColor2": principled.inputs["Base Color"]}
    normal_input = principled.inputs["Normal"]
    if normal_input.is_linked and normal_input.links[0].from_node.type == "NORMAL_MAP":
        targets["Normal2"] = normal_input.links[0].from_node.inputs["Color"]

    mask = None
    for suffix, node in layers.items():
        target = targets.get(suffix)
        if target is None or not target.is_linked:
            continue
        if mask is None:
            color = tree.nodes.new(type="ShaderNodeVertexColor")
            color.layer_name = "_c0"
            color.location = principled.location.x - 900, principled.location.y + 300
            mask = tree.nodes.new(type="ShaderNodeSeparateColor")
            mask.label = "Second Layer Mask"
            mask.location = color.location.x + 200, color.location.y
            tree.links.new(color.outputs["Color"], mask.inputs["Color"])
        mix = tree.nodes.new(type="ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.label = f"Mix {suffix}"
        mix.location = target.node.location.x - 200, target.node.location.y + 200
        tree.links.new(mask.outputs[1], mix.inputs[0])
        tree.links.new(target.links[0].from_socket, mix.inputs[6])
        tree.links.new(node.outputs["Color"], mix.inputs[7])
        tree.links.new(mix.outputs[2], target)


def __set_uv_map(tree: bpy.types.NodeTree, tex_node: bpy.types.ShaderNodeTexImage, uv_map: str):
    """Make an image texture node use a UV map, through its mapping node if it has one."""
    target = tex_node.inputs["Vector"]
    if target.is_linked and target.links[0].from_node.type == "MAPPING":
        target = target.links[0].from_node.inputs["Vector"]
    uv_node = tree.nodes.new(type="ShaderNodeUVMap")
    uv_node.uv_map = uv_map
    uv_node.location = target.node.location.x - 250, target.node.location.y
    tree.links.new(uv_node.outputs["UV"], target)


def __split_mra(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Split an "MRA" texture plugged into Metallic: metalness in red, roughness in green, AO in blue."""
    principled = mat_wrap.node_principled_bsdf
    metallic = principled.inputs["Metallic"]
    if not metallic.is_linked:
        return
    image_node = metallic.links[0].from_node
    if image_node.type != "TEX_IMAGE" or image_node.image is None:
        return
    if __slot_from_texture_name(image_node.image.name) != "_mra":
        return

    tree = mat.node_tree
    separate = tree.nodes.new(type="ShaderNodeSeparateColor")
    separate.label = "MRA"
    separate.location = image_node.location.x + 300, image_node.location.y
    tree.links.new(image_node.outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs[0], metallic)
    roughness = principled.inputs["Roughness"]
    if not roughness.is_linked:
        tree.links.new(separate.outputs[1], roughness)


def __split_packed_roughness(mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, wet_effect: bool):
    """Use only the roughness channel of packed "Gn" textures plugged into Roughness.

    These store roughness in red and a water droplet mask in green (dark = droplet), which the game only
    uses while a character is wet. Plugging the whole image in averages the two, so dry surfaces look wet.
    """
    roughness = mat_wrap.node_principled_bsdf.inputs["Roughness"]
    if not roughness.is_linked:
        return
    image_node = roughness.links[0].from_node
    if image_node.type != "TEX_IMAGE" or image_node.image is None:
        return
    if not image_node.image.name.rsplit("_", 1)[-1].startswith("Gn"):
        return

    tree = mat.node_tree
    separate = tree.nodes.new(type="ShaderNodeSeparateColor")
    separate.location = image_node.location.x + 300, image_node.location.y
    tree.links.new(image_node.outputs["Color"], separate.inputs["Color"])
    if wet_effect:
        wet = tree.nodes.new(type="ShaderNodeMath")
        wet.operation = "MULTIPLY"
        wet.label = "Wet Look"
        wet.location = separate.location.x + 200, separate.location.y
        tree.links.new(separate.outputs[0], wet.inputs[0])
        tree.links.new(separate.outputs[1], wet.inputs[1])
        tree.links.new(wet.outputs["Value"], roughness)
    else:
        tree.links.new(separate.outputs[0], roughness)


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


def __hide_if_no_color_written(fmat: Material, mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Hide materials that don't draw any color.

    HAL's games (Kirby) turn off every color channel for some materials, e.g. the stencil masks around Whispy
    Woods' eyes and the old flat leaf cards left behind his newer leaves, so they never show up in the game.
    """
    if __render_info(fmat, "channelMask") != "zero":
        return
    log.info("Hiding material '%s': it doesn't write any color", mat.name)
    principled = mat_wrap.node_principled_bsdf
    alpha_input = principled.inputs["Alpha"]
    for link in list(alpha_input.links):
        mat.node_tree.links.remove(link)
    alpha_input.default_value = 0.0
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    else:  # Blender < 4.2
        mat.blend_method = "CLIP"
        mat.shadow_method = "NONE"


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


def __setup_alpha(
    fmat: Material, mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper, missing_albedo=False, report=None
):
    """Use the base color texture's alpha for transparency, based on the material's render state."""
    mode = __render_info(fmat, "gsys_render_state_mode")
    blend_mode = __render_info(fmat, "gsys_render_state_blend_mode")
    alpha_test = mode == "mask" or __render_info(fmat, "gsys_alpha_test_enable") == "true"
    translucent = mode == "translucent" or (mode == "custom" and blend_mode not in {None, "none"})
    rgb_src = __render_info(fmat, "gsys_color_blend_rgb_src_func")
    rgb_dst = __render_info(fmat, "gsys_color_blend_rgb_dst_func")
    render_pass = __render_info(fmat, "renderPass")
    if render_pass is not None:
        # Kirby and the Forgotten Land. Its alpha test is part of each material's shader, so cutouts are
        # found from the meshes later instead (see apply_texture_cutout).
        translucent = render_pass == "xlu" or __render_info(fmat, "blendEnabled") == 1
        blend_factors = {"one": "one", "sourceAlpha": "src_alpha"}
        rgb_src = blend_factors.get(__render_info(fmat, "sourceColorBlendFactor"))
        rgb_dst = blend_factors.get(__render_info(fmat, "destinationColorBlendFactor"))
    if not (alpha_test or translucent):
        return

    if __render_info(fmat, "gsys_render_state_display_face") == "front":
        mat.use_backface_culling = True

    tree = mat.node_tree
    principled = mat_wrap.node_principled_bsdf
    legacy = not hasattr(mat, "surface_render_method")  # Blender < 4.2

    # Additive blending (e.g. fake eye highlights): only light gets added on top of what's behind.
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
        image_node = __base_color_image_node(principled)
        if image_node is None and missing_albedo:
            # The albedo holds the cutout mask (e.g. eyebrow cards), so without it there's nothing to show.
            # Some games reference textures that aren't in the dump; hide the surface instead of drawing
            # a solid card.
            msg = f"Hid material '{mat.name}': its albedo texture (which has the alpha mask) is missing"
            log.warning(msg)
            if report is not None:
                report({"WARNING"}, msg)
            alpha_input.default_value = 0.0
            if legacy:
                mat.blend_method = "CLIP"
                mat.shadow_method = "CLIP"
            else:
                mat.surface_render_method = "DITHERED"
            return
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
    else:
        __make_alpha_test(mat, principled, threshold)


def __outline(fmat: Material, mat: bpy.types.Material, mat_wrap: PrincipledBSDFWrapper):
    """Only draw the back faces of outline shells, darkened like the game does.

    Kirby's Return to Dream Land Deluxe gives most models an "_Outline" copy of their meshes (an inverted
    hull) that the game pushes out along the normals and draws back faces only. The push is done at runtime
    (OutlineThickness is 0 in the files), so the imported shell lies right on the model. Drawn front faces
    and all, it covers the model with the outline's texture, which is wrong wherever that isn't the
    texture underneath (e.g. the star on the present box).
    """
    if __render_info(fmat, "IsOutline") != 1:
        return

    tree = mat.node_tree
    principled = mat_wrap.node_principled_bsdf
    mat.use_backface_culling = False

    # Only seen by the camera: rays leaving the surface under the shell (shadows, bounces) would hit its back
    # faces and darken the model in Cycles. Outlines don't cast shadows in the game either.
    geometry = tree.nodes.new(type="ShaderNodeNewGeometry")
    geometry.label = "Outline"
    geometry.location = principled.location.x - 850, principled.location.y - 600
    light_path = tree.nodes.new(type="ShaderNodeLightPath")
    light_path.location = geometry.location.x, geometry.location.y - 250
    visible = tree.nodes.new(type="ShaderNodeMath")
    visible.operation = "MULTIPLY"
    visible.label = "Back Faces Only"
    visible.location = geometry.location.x + 200, geometry.location.y
    tree.links.new(geometry.outputs["Backfacing"], visible.inputs[0])
    tree.links.new(light_path.outputs["Is Camera Ray"], visible.inputs[1])
    alpha = visible.outputs["Value"]

    alpha_input = principled.inputs["Alpha"]
    if alpha_input.is_linked:
        multiply = tree.nodes.new(type="ShaderNodeMath")
        multiply.operation = "MULTIPLY"
        multiply.location = visible.location.x + 200, visible.location.y
        tree.links.new(alpha_input.links[0].from_socket, multiply.inputs[0])
        tree.links.new(alpha, multiply.inputs[1])
        alpha = multiply.outputs["Value"]
    tree.links.new(alpha, alpha_input)

    base_color = principled.inputs["Base Color"]
    brightness = mat.get("MP_OutlineBrightness_NoKey")
    if brightness is not None and base_color.is_linked:
        darken = tree.nodes.new(type="ShaderNodeMix")
        darken.data_type = "RGBA"
        darken.blend_type = "MULTIPLY"
        darken.label = "Outline Brightness"
        darken.location = principled.location.x - 200, principled.location.y + 200
        darken.inputs[0].default_value = 1.0
        darken.inputs[7].default_value = (brightness, brightness, brightness, 1.0)
        tree.links.new(base_color.links[0].from_socket, darken.inputs[6])
        tree.links.new(darken.outputs[2], base_color)

    if not hasattr(mat, "surface_render_method"):  # Blender < 4.2
        if mat.blend_method == "OPAQUE":
            mat.blend_method = "HASHED"
            mat.shadow_method = "HASHED"
    elif mat.surface_render_method != "BLENDED":
        mat.surface_render_method = "DITHERED"


def __make_alpha_test(mat: bpy.types.Material, principled, threshold: float):
    """Make the alpha plugged into the Principled BSDF a hard cutout."""
    if not hasattr(mat, "surface_render_method"):  # Blender < 4.2
        mat.blend_method = "CLIP"
        mat.shadow_method = "CLIP"
        mat.alpha_threshold = threshold
        return
    # EEVEE Next has no alpha clip mode, so make the cutout hard with a math node like the game does.
    tree = mat.node_tree
    alpha_input = principled.inputs["Alpha"]
    source = alpha_input.links[0].from_socket
    clip = tree.nodes.new(type="ShaderNodeMath")
    clip.operation = "GREATER_THAN"
    clip.label = "Alpha Test"
    clip.inputs[1].default_value = threshold
    clip.location = principled.location.x - 200, principled.location.y - 400
    tree.links.new(source, clip.inputs[0])
    tree.links.new(clip.outputs["Value"], alpha_input)
    mat.surface_render_method = "DITHERED"


# Mix nodes added on top of the base color texture, by label, and which input the base color is in.
BASE_COLOR_MIX_INPUTS = {"Face Pattern": 6, "Mix BaseColor2": 6}


def __base_color_image_node(principled):
    """Return the node plugged into Base Color, looking through the layers mixed on top of a texture."""
    links = principled.inputs["Base Color"].links
    node = links[0].from_node if links else None
    while node is not None and node.type == "MIX" and node.label in BASE_COLOR_MIX_INPUTS:
        links = node.inputs[BASE_COLOR_MIX_INPUTS[node.label]].links
        node = links[0].from_node if links else None
    return node


def apply_texture_cutout(mat: bpy.types.Material, objects):
    """Alpha test opaque Kirby and the Forgotten Land materials whose base color alpha is a cutout.

    The game's alpha test is compiled into each material's shader, and many base colors store other data
    in their alpha (e.g. masks that are 0 over most of the model), so this looks at the meshes: if some of
    their faces, but not most, sit on transparent texels (like the fur cards of the rabbit enemy's tail),
    the alpha is a cutout.
    """
    if mat.get("RI_renderPass") != "opa" or mat.get("RI_blendEnabled", 0) != 0:
        return
    principled = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if principled is None or principled.inputs["Alpha"].is_linked:
        return
    image_node = __base_color_image_node(principled)
    if image_node is None or image_node.type != "TEX_IMAGE" or image_node.image is None:
        return
    image = image_node.image
    width, height = image.size
    if image.channels < 4 or width == 0 or height == 0:
        return
    alpha = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(alpha)
    alpha = alpha[3::4].reshape(height, width)
    if alpha.min() >= 0.5:
        return

    vector = image_node.inputs["Vector"]
    uv_node = vector.links[0].from_node if vector.is_linked else None
    uv_name = uv_node.uv_map if uv_node is not None and uv_node.type == "UVMAP" else None
    transparent = total = 0
    for obj in objects:
        mesh = obj.data
        uv_layer = mesh.uv_layers.get(uv_name) if uv_name else mesh.uv_layers.active
        if uv_layer is None or not mesh.polygons:
            continue
        uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        uv_layer.data.foreach_get("uv", uvs)
        uvs = uvs.reshape(-1, 2)
        starts = np.empty(len(mesh.polygons), dtype=np.int32)
        counts = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", counts)
        centers = np.add.reduceat(uvs, starts) / counts[:, None]  # loops of a face are contiguous
        x = (np.mod(centers[:, 0], 1.0) * width).astype(np.int32).clip(0, width - 1)
        y = (np.mod(centers[:, 1], 1.0) * height).astype(np.int32).clip(0, height - 1)
        transparent += int((alpha[y, x] < 0.5).sum())
        total += len(centers)
    if total == 0 or not 0.005 < transparent / total < 0.5:
        return

    log.info("Alpha testing material '%s' (%d of %d faces are cut out)", mat.name, transparent, total)
    mat.node_tree.links.new(image_node.outputs["Alpha"], principled.inputs["Alpha"])
    __make_alpha_test(mat, principled, 0.5)


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
