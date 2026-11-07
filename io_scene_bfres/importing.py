import io
import logging
import struct
from pathlib import Path

import bpy

from . import romfs, yaz0
from .bfrespy import common
from .bfrespy.res_file import ResFile
from .bone_anim_importer import BoneAnimationImporter
from .exceptions import MalformedFileError, UnsupportedFileTypeError
from .model_importer import ModelImporter

log = logging.getLogger(__name__)


class Importer:
    def __init__(self, operator, filepath):
        self.operator = operator
        self.bfres: ResFile

        # Extract path information.
        self.filepath = Path(filepath)
        self.directory = self.filepath.parent
        self.filename = self.filepath.name
        self.fileext = self.filepath.suffix.upper()
        self.texture_map = {}

        # Files taken from a romfs dump need the dump's zstd dictionaries and shared strings.
        self.romfs_root = romfs.find_romfs_root(self.filepath)
        self.zstd_dicts = romfs.load_zstd_dicts(self.romfs_root)
        if self.romfs_root:
            log.info("Found romfs dump at: %s", self.romfs_root)

    def run(self):
        with open(self.filepath, "rb") as f:
            return self._load_stream(f)

    def run_file(self, filepath: str | Path):
        with open(filepath, "rb") as f:
            return self._load_stream(f)

    def _load_stream(self, raw) -> set:
        """Check if the stream is decompressed, and then check if it's an archive"""
        # Ensure to have a stream with decompressed data.
        magic = raw.read(4)
        raw.seek(0, io.SEEK_SET)
        match magic:
            # Uncompressed
            case b"FRES":
                return self._import_bfres(raw)
            case b"BNTX":
                return self._import_bntx(raw)
            # Archive
            case b"SARC":
                r = self._get_from_sarc(raw)
                return self._load_stream(r)
            # Compressed
            case b"\x28\xb5\x2f\xfd":  # zstd
                r = romfs.zstd_decompress(raw.read(), self.zstd_dicts)
                return self._load_stream(io.BytesIO(r))
            case b"Yaz0":
                r = yaz0.decompress(raw)
                return self._load_stream(r)
            case _:
                raise UnsupportedFileTypeError(magic)

    def _import_bfres(self, stream):
        """Import a BFRES file, and return 'FINISHED' if it succeeds"""
        self._load_external_strings(stream)
        bfres = ResFile(stream)
        self.bfres = bfres

        # Read and import any external files
        for node in bfres.external_files:
            self._import_embed(node, bfres.name)

        self._import_linked_textures(bfres)

        if self.operator.import_anims:
            anim_imp = BoneAnimationImporter(self)
            anim_imp._import_animations(bfres)

        # If there's more than one model, add them all to a collection.
        if len(bfres.models) > 1:
            collection = bpy.data.collections.new(name=bfres.name)
            bpy.context.scene.collection.children.link(collection)
        else:
            collection = bpy.context.scene.collection

        model_imp = ModelImporter(self)
        for fmdl in bfres.models.values():
            model_imp.convert_fmdl(fmdl, collection)

        return {"FINISHED"}

    # The file the global string cache was last filled from.
    _external_strings_source: Path | None = None

    def _load_external_strings(self, stream):
        """Load the shared string file if this bfres refers to strings stored outside of it."""
        stream.seek(0, io.SEEK_SET)
        header = stream.read(0xF0)
        stream.seek(0, io.SEEK_SET)
        if len(header) < 0xF0 or header[4:8] != b"    " or header[0x0A] < 10:
            return  # Not a v10+ Switch bfres, so there are no external flags
        if not header[0xEE] & ResFile.ExternalFlags.HAS_EXTERNAL_STRING:
            return

        if self.romfs_root is None:
            msg = (
                "This bfres keeps its strings in "
                f"{romfs.EXTERNAL_STRING_FILE.as_posix()}. Import it from inside a romfs dump."
            )
            raise MalformedFileError(msg)

        string_file = self.romfs_root / romfs.EXTERNAL_STRING_FILE
        if Importer._external_strings_source == string_file and common.stringcache:
            return
        log.info("Loading external strings from: %s", string_file)
        with open(string_file, "rb") as f:
            data = romfs.zstd_decompress(f.read(), self.zstd_dicts)
        ResFile(io.BytesIO(data))  # Fills common.stringcache
        Importer._external_strings_source = string_file

    def _import_linked_textures(self, bfres: ResFile):
        """Import textures that live in files next to the bfres instead of inside it."""
        mode = self.operator.import_tex_mode
        if mode == "EMBEDDED":
            return

        if mode in {"AUTO", "TEX_FILE"} and self._missing_textures(bfres):
            texpath = self.directory / (self.filepath.stem + ".Tex" + self.filepath.suffix)
            if texpath.is_file():
                log.info("Importing linked file: %s", texpath)
                self.run_file(texpath)

        if mode not in {"AUTO", "TEX_FOLDER"}:
            return
        missing = self._missing_textures(bfres)
        if not missing:
            return
        if self.romfs_root:
            texfolder = self.romfs_root / romfs.TEX_DIR
        else:
            texfolder = self.filepath.parents[1] / romfs.TEX_DIR
        if not texfolder.is_dir():
            log.info("No Tex folder found at: %s", texfolder)
            return

        failed = []
        for i, name in enumerate(sorted(missing)):
            texpath = romfs.find_texture_file(texfolder, name)
            if texpath is None:
                log.warning("Texture file not found in Tex folder: %s", name)
                failed.append(name)
                continue
            log.info("Importing texture file %3d/%3d: %s", i + 1, len(missing), texpath.name)
            try:
                self.run_file(texpath)
            except Exception:
                log.exception("Failed to import texture file: %s", texpath)
                failed.append(name)
        if failed:
            self.operator.report(
                {"WARNING"}, f"{len(failed)} texture(s) could not be imported, see the console for details"
            )

    def _missing_textures(self, bfres: ResFile) -> set[str]:
        """Return the texture names the materials use that haven't been imported yet.

        Images that already exist in the blend file (e.g. shared textures from an earlier import) are reused.
        """
        prefix = self.operator.name_prefix
        missing = set()
        for model in bfres.models.values():
            for fmat in model.materials.values():
                for tex in fmat.texture_refs:
                    if tex.name in self.texture_map:
                        continue
                    image = bpy.data.images.get(prefix + tex.name)
                    if image is not None:
                        self.texture_map[tex.name] = image
                    else:
                        missing.add(tex.name)
        return missing

    def _import_bntx(self, stream):
        """Import a bntx file, and return 'FINISHED' if it succeeds"""
        from . import bntx
        from .texture_importer import import_textures

        bntx_ = bntx.BNTX(stream)

        self.texture_map.update(import_textures(bntx_, self.operator))

        return {"FINISHED"}

    def _import_embed(self, node, name):
        """Import an embedded file in the ResFile"""
        name = node.key
        file = node.value
        if name.endswith(".txt"):
            # embed text blend file
            obj = bpy.data.texts.new(name=name)
            obj.write(file.data.decode("utf-8"))
        elif file.data:
            try:
                self._load_stream(io.BytesIO(file.data))
            except UnsupportedFileTypeError as ex:
                log.debug("Embedded file '%s' is of unsupported type '%s'", name, ex.magic)
        else:
            log.debug("Embedded file '%s' is empty", name)

    @staticmethod
    def _get_from_sarc(raw: io.BytesIO | io.BufferedReader):
        """Attempt to return a FRES file from a SARC archive."""
        log.debug("Extracting from SARC")
        raw.seek(6, io.SEEK_CUR)
        bom = raw.read(2)
        endianness = ">" if bom == 0xFEFF else "<"
        raw.seek(4, io.SEEK_CUR)
        offs = struct.unpack(endianness + "I", raw.read(4))[0]
        raw.seek(10, io.SEEK_CUR)
        num_nodes = struct.unpack(endianness + "H", raw.read(2))[0]
        raw.seek(4, io.SEEK_CUR)
        files = []
        for _ in range(num_nodes):
            raw.seek(8, io.SEEK_CUR)
            start_offs = struct.unpack(endianness + "I", raw.read(4))[0]
            end_offs = struct.unpack(endianness + "I", raw.read(4))[0]
            files.append((start_offs, end_offs))
        for fileoff in files:
            raw.seek(fileoff[0] + offs, io.SEEK_SET)
            if raw.read(4) == b"FRES":
                raw.seek(-4, io.SEEK_CUR)
                return io.BytesIO(raw.read(fileoff[1] - offs))
        raise MalformedFileError("Embedded SARC file does not contain FRES")

    @staticmethod
    def _add_object_to_collecton(obj, collection_name):
        """Add an object to a collection, and create it if it does not already exist."""
        group = bpy.data.collections.get(collection_name, bpy.data.collections.new(name=collection_name))

        # Link the provided object to it.
        if obj.name not in group.objects:
            group.objects.link(obj)
