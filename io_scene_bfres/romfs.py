"""Helpers for importing files straight out of a romfs dump.

Newer games (Tears of the Kingdom, Ocarina of Time for Switch 2, ...) share a layout:

- ``Model/<name>.bfres.zs``: zstd-compressed models, using a zstd dictionary
- ``Tex/<texture>.bntx.zs``: one BNTX per texture, referenced by name from the materials
- ``Pack/ZsDic.pack.zs``: a SARC holding the zstd dictionaries
- ``Shader/ExternalBinaryString.bfres.zs``: shared strings that models refer to by ID

None of these can be read from the model file alone, so this module finds the romfs root
from the path of the imported file and loads what is needed from it.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import zstandard

from .exceptions import MalformedFileError

log = logging.getLogger(__name__)

ZSDIC_PACK = Path("Pack", "ZsDic.pack.zs")
EXTERNAL_STRING_FILE = Path("Shader", "ExternalBinaryString.bfres.zs")
TEX_DIR = "Tex"
MOTIF_DIR = Path("Step", "Decor", "Motif")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Dictionaries are the same for every file in a dump, so keep them between imports.
_dict_cache: dict[Path, dict[int, zstandard.ZstdCompressionDict]] = {}
# Texture files by texture name, for each folder that was searched.
_texture_index_cache: dict[tuple[Path, bool], dict[str, Path]] = {}


def find_romfs_root(path: str | Path) -> Path | None:
    """Return the romfs directory that contains ``path``, or None if it's not inside one."""
    for parent in Path(path).resolve().parents:
        if (parent / ZSDIC_PACK).is_file():
            return parent
    return None


def read_sarc(data: bytes) -> dict[str, bytes]:
    """Return every named file in a SARC archive."""
    if data[:4] != b"SARC":
        raise MalformedFileError("Not a SARC archive")
    header_len, bom = struct.unpack_from("<HH", data, 4)
    endian = "<" if bom == 0xFEFF else ">"
    data_offs = struct.unpack_from(endian + "I", data, 0x0C)[0]

    sfat = header_len
    magic, _, node_count, _ = struct.unpack_from(endian + "4sHHI", data, sfat)
    if magic != b"SFAT":
        raise MalformedFileError("SARC is missing its SFAT section")
    nodes = [struct.unpack_from(endian + "IIII", data, sfat + 12 + 16 * i) for i in range(node_count)]
    names_offs = sfat + 12 + 16 * node_count + 8  # skip the SFNT header

    files = {}
    for _, attrs, start, end in nodes:
        if not attrs & 0x01000000:  # node has no name
            continue
        name_offs = names_offs + (attrs & 0xFFFF) * 4
        name = data[name_offs : data.index(b"\0", name_offs)].decode("utf-8")
        files[name] = data[data_offs + start : data_offs + end]
    return files


def load_zstd_dicts(romfs_root: Path | None) -> dict[int, zstandard.ZstdCompressionDict]:
    """Load the zstd dictionaries of a romfs dump, keyed by dictionary ID."""
    if romfs_root is None:
        return {}
    if romfs_root in _dict_cache:
        return _dict_cache[romfs_root]

    dicts = {}
    with open(romfs_root / ZSDIC_PACK, "rb") as f:
        pack = zstandard.ZstdDecompressor().decompressobj().decompress(f.read())
    for name, data in read_sarc(pack).items():
        zdict = zstandard.ZstdCompressionDict(data)
        dicts[zdict.dict_id()] = zdict
        log.debug("Loaded zstd dictionary '%s' (id %d)", name, zdict.dict_id())
    _dict_cache[romfs_root] = dicts
    return dicts


def zstd_decompress(data: bytes, dicts: dict[int, zstandard.ZstdCompressionDict]) -> bytes:
    """Decompress a zstd frame, using the dictionary it was compressed with if it needs one."""
    dict_id = zstandard.get_frame_parameters(data).dict_id
    zdict = None
    if dict_id:
        zdict = dicts.get(dict_id)
        if zdict is None:
            msg = (
                f"This file needs zstd dictionary {dict_id}. Import it from inside a romfs dump "
                f"so that {ZSDIC_PACK.as_posix()} can be found."
            )
            raise MalformedFileError(msg)
    return zstandard.ZstdDecompressor(dict_data=zdict).decompressobj().decompress(data)


def decompress_cmp(data: bytes) -> bytes | None:
    """Decompress a HAL ``.cmp`` file: the decompressed size, then a zstd frame. None if it isn't one."""
    if data[4:8] != ZSTD_MAGIC:
        return None
    return zstandard.ZstdDecompressor().decompressobj().decompress(data[4:])


def bntx_texture_names(data: bytes) -> set[str]:
    """Return the names in the BNTX files inside ``data``, read from their string tables without decoding them."""
    names = set()
    start = data.find(b"BNTX\0\0\0\0")
    while start >= 0:
        table = data.find(b"_STR", start)
        if data[start + 0x20 : start + 0x24] == b"NX  " and table >= 0:
            (count,) = struct.unpack_from("<I", data, table + 0x10)
            pos = table + 0x18
            for _ in range(count):
                (length,) = struct.unpack_from("<H", data, pos)
                names.add(data[pos + 2 : pos + 2 + length].decode("utf-8", "replace"))
                pos = (pos + 2 + length + 2) & ~1  # length, string and its null, aligned to 2
        start = data.find(b"BNTX\0\0\0\0", start + 4)
    return names


def find_shared_texture_files(model_path: Path, names: set[str]) -> dict[Path, set[str]]:
    """Return the files that hold the given textures, for models that don't embed them.

    Kirby and the Forgotten Land's stage models use textures kept in other files: ``Tex_<hash>.bfres.cmp``
    next to them, and the material libraries in ``gfx/Step/Decor/Motif`` (e.g. ``Grass/Land/GsDefault/Main.bfres.cmp``
    or ``AutoDecor/Land/Grass/<texture>/Tex.bfres.cmp``). The model's folder is searched first, then Motif.
    """
    indexes = [_texture_index(model_path.parent, recursive=False)]
    for parent in model_path.resolve().parents:
        if (parent / MOTIF_DIR).is_dir():
            indexes.append(_texture_index(parent / MOTIF_DIR, recursive=True))
            break

    files: dict[Path, set[str]] = {}
    for name in sorted(names):
        path = next((index[name] for index in indexes if name in index), None)
        if path is not None and path.resolve() != model_path.resolve():
            files.setdefault(path, set()).add(name)
    return files


def _texture_index(folder: Path, recursive: bool) -> dict[str, Path]:
    """Return the ``.cmp`` file each texture in a folder is in, by texture name."""
    key = (folder.resolve(), recursive)
    if key not in _texture_index_cache:
        log.info("Indexing textures in: %s", folder)
        index = {}
        for path in sorted(folder.glob("**/*.cmp" if recursive else "*.cmp")):
            try:
                data = decompress_cmp(path.read_bytes())
                for name in bntx_texture_names(data) if data else ():
                    index.setdefault(name, path)
            except Exception:
                log.debug("Couldn't read textures in: %s", path, exc_info=True)
        _texture_index_cache[key] = index
    return _texture_index_cache[key]


def find_texture_file(tex_dir: Path, name: str, prefixes: tuple[str, ...] = ()) -> Path | None:
    """Return the BNTX file for a texture name in a Tex folder, if there is one.

    Some games (e.g. Splatoon Raiders) prefix each texture file with the name of the model that
    uses it (``Tex/<model>.<texture>.bntx.zs``), so those prefixes are tried after the bare name.
    """
    for stem in (name, *(f"{prefix}.{name}" for prefix in prefixes)):
        for suffix in (".bntx.zs", ".bntx", ".bntx.szs"):
            path = tex_dir / (stem + suffix)
            if path.is_file():
                return path
    return None
