# This file is a modified file from
# https://github.com/aboood40091/BNTX-Editor/
# Which is licensed under GPL-3
import logging
import math

log = logging.getLogger(__name__)


def round_up(x, y):
    return ((x - 1) | (y - 1)) + 1


def deswizzle(
    width, height, blk_width, blk_height, bpp, tile_mode, blk_height_log2, data, switch2_gob=False
) -> bytes:
    """Deswizzled image data

    switch2_gob selects the GOB layout used by Switch 2 (NVN2, "Ounc" platform) textures,
    which orders the 16 byte sectors inside each GOB differently from the Switch 1.
    """
    # Modified from https://github.com/aboood40091/BNTX-Editor/ which is under a GPL-3.0 License
    block_height = 1 << blk_height_log2

    width = math.ceil(width / blk_width)
    height = math.ceil(height / blk_height)

    if tile_mode == 1:
        pitch = width * bpp

        # If wii u support is added this depends on if the header of the texture is "NX" or not
        pitch = round_up(pitch, 32)

        surf_size = pitch * height

    else:
        pitch = round_up(width * bpp, 64)
        surf_size = pitch * round_up(height, block_height * 8)

    result = bytearray(surf_size)

    for y in range(height):
        for x in range(width):
            if tile_mode == 1:
                pos = y * pitch + x * bpp

            else:
                pos = __get_addr_block_linear(x, y, width, bpp, 0, block_height, switch2_gob)

            pos_ = (y * width + x) * bpp

            if pos + bpp <= surf_size:
                result[pos_ : pos_ + bpp] = data[pos : pos + bpp]

    return bytes(result)


def __get_addr_block_linear(x, y, image_width, bytes_per_pixel, base_address, block_height, switch2_gob=False):
    image_width_in_gobs = math.ceil((image_width * bytes_per_pixel) / 64)
    gob_address = (
        base_address
        + (y // (8 * block_height)) * 512 * block_height * image_width_in_gobs
        + (x * bytes_per_pixel // 64) * 512 * block_height
        + (y % (8 * block_height) // 8) * 512
    )

    x *= bytes_per_pixel

    if switch2_gob:
        # Same 64x8 byte GOB, but the sector address bits are ordered x1, y2, x0, y1, y0
        # (found by testing against textures from a Switch 2 romfs).
        return (
            gob_address
            + ((x % 64) // 32) * 256
            + ((y % 8) // 4) * 128
            + ((x % 32) // 16) * 64
            + ((y % 4) // 2) * 32
            + (y % 2) * 16
            + (x % 16)
        )

    return gob_address + ((x % 64) // 32) * 256 + ((y % 8) // 2) * 64 + ((x % 32) // 16) * 32 + (y % 2) * 16 + (x % 16)
