from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import bpy
import numpy as np

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .bntx.bntx import BNTX


def import_textures(bntx: BNTX, operator, only: set[str] | None = None):
    """Import textures from BNTX, or only the ones named in ``only``."""
    images = {}
    for i, tex in enumerate(bntx.nx.textures):
        if only is not None and tex.name not in only:
            continue
        log.info(
            "Importing texture %3d/%3d '%s' (%s, %s)...",
            i + 1,
            len(bntx.nx.textures),
            tex.name,
            tex.format_.__name__,
            tex.fmt_dtype.name,
        )
        float_buffer = False
        isdata = not bool(tex.fmt_dtype.name == "SRGB")
        alpha = bool(b"\x05" in tex.channel_types)

        if tex.fmt_dtype.name in {"UHALF", "SINGLE"}:
            isdata = False
            float_buffer = True

        image = bpy.data.images.new(
            name=operator.name_prefix + tex.name,
            width=tex.width,
            height=tex.height,
            float_buffer=float_buffer,
            alpha=alpha,
            is_data=isdata,
        )

        # Issues arise when textures are not multiples of 4, pretty rare.
        if len(tex.pixels) > tex.width * tex.height * 4:
            pixels = tex.pixels[: tex.width * tex.height * 4]
            pixels = tex.format_.decodepixels(pixels)
        else:
            pixels = tex.format_.decodepixels(tex.pixels)
        pixels = pixels.reshape((tex.height, tex.width, 4))

        if (
            operator.component_selector
            and tex.format_.__name__ != "BC1"  # don't make alpha channel if it's not needed
            and not (tex.format_.__name__ == "BC5" and tex.channel_types[2] == 0)  # normal maps
        ):
            temppix = pixels.copy()
            for ch in range(4):
                if tex.channel_types[ch] == ch + 2:
                    continue
                match tex.channel_types[ch]:
                    case 0:
                        pixels[..., ch] = 0
                    case 1:
                        pixels[..., ch] = 1
                    case 2:
                        pixels[..., ch] = temppix[..., 0]
                    case 3:
                        pixels[..., ch] = temppix[..., 1]
                    case 4:
                        pixels[..., ch] = temppix[..., 2]
                    case 5:
                        pixels[..., ch] = temppix[..., 3]

            # Two channel normal maps (e.g. ASTC with an R, A, 0, 1 selector) only store X and Y.
            # Rebuild Z so they work with Blender's Normal Map node.
            if isdata and not float_buffer and tex.channel_types[2] == 0 and tex.channel_types[3] == 1:
                nx = pixels[..., 0] * 2 - 1
                ny = pixels[..., 1] * 2 - 1
                pixels[..., 2] = np.sqrt(np.clip(1 - nx * nx - ny * ny, 0, 1)) * 0.5 + 0.5

        # Add some file data if it's needed:
        if tex.fmt_dtype.name in {"UHALF", "SINGLE"}:
            image.file_format = "OPEN_EXR"
            image.colorspace_settings.name = "sRGB"
        else:
            image.file_format = "PNG"

        # flip image from dx to gl
        pixels = np.flipud(pixels)

        image.pixels.foreach_set(np.ravel(pixels).astype(np.float32))

        image.use_fake_user = operator.add_fake_user

        image.update()
        image.pack()
        images[tex.name] = image
    return images
