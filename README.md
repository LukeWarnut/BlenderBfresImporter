I have no desire to continue this project. I don't know if anybody even uses this and the community has treated me like shit for ages

# Bfres Importer

This is a Blender(3.6+) addon imports the Nintendo .bfres file format.

## Installation

Download the zip from the Releases section of the page, and in Blender, open preferences, Get Extensions, and on the dropdown menu at the top right, click "Install from disk" and select the zip file.

## Notes

This extension is not complete, notably Wii U files and any sort of animation cannot be imported.

Blender handles skeletal animations very differently and it may not be possible to add it in without calculating everything on every frame. In the meantime, these work with animations exported from Switch Toolbox (which calculates each frame)

## Tips

### Importing from a romfs dump

Newer games (e.g. Tears of the Kingdom, Ocarina of Time on Switch 2) keep textures, zstd dictionaries and some strings outside the model file. Import the `.bfres.zs` straight from the dump's `romfs/Model` folder and, with "Texture Import Mode" set to "Automatic", the addon will find `Pack/ZsDic.pack.zs`, `Shader/ExternalBinaryString.bfres.zs` and the textures in `romfs/Tex` by itself.

Games that don't have these files, like Splatoon Raiders, work the same way. Texture files named after the model (e.g. `Tex/Npc_Foo.M_Body_Alb.bntx.zs` for `Model/Npc_Foo.Npc_Foo.bfres.zs`) are found too.

Kirby and the Forgotten Land's `.bfres.cmp` files can be imported directly. Most models embed their textures. Stage models that don't, with "Texture Import Mode" set to "Automatic", get them from the `Tex_<hash>.bfres.cmp` files next to them and from the shared files in `gfx/Step/Decor/Motif`. Only the textures the model uses are imported from those.

Kirby's Return to Dream Land Deluxe models come with "_Outline" copies of their meshes, which the game draws as outlines. These only show their back faces and get an "Outline Thickness" Displace modifier that pushes them out from the model; change its strength to make the outlines thicker or thinner, or set it to 0 to hide them.

Textures that already exist in the blend file with the same name are reused instead of being imported again.

### Material Data

This addon imports the material parameters, shader options and render info of the bfres material into the "Custom Properties" of the Blender material.

The name of the parameter is prefixed with "MP_", "SO_" and "RI_" respectively. These can be used in a shader node setup, using the Attribute node, set to Object, and the Name as `materials[0]["name_of_param"]`

### Animation data

Animations are not fully supported, but importing the base animation data will import the first frame of the animations.

These animations are stored as Blender Actions, you can select an armature, and in the Armature Data panel, or animation editor, you can select the action. It may be useful for some quick poses.

### Copy bone transforms

If you select an armature, such as a player model, you can import another model such as a hat or clothes with the "Copy bone transforms" option enabled, and it will make constraints to attach those to the model.

This works if the bones on the imported skeleton and the existing selected skeleton have the same names (with an added case if there's a "\_root" suffix on the imported model)

Splatoon headgear will still have to be manually dealt with, though a couple work.

## Dependencies

This addon uses the `numpy` and `zstandard` python modules. Blender should come with it automatically, but on a distro like Arch which uses your system python, you may have to install it with your package manager (or use `blender-bin` on the AUR)

It also includes wheels for [texture2ddecoder](https://github.com/K0lb3/texture2ddecoder).

## References

This addon was originally a fork of [RenaKunisaki/bfres_importer](https://github.com/RenaKunisaki/bfres_importer)

- [Syroot/io_scene_bfres](https://gitlab.com/Syroot/NintenTools/io_scene_bfres)
- [KillzXGaming/SwitchToolbox](https://github.com/KillzXGaming/Switch-Toolbox)
- [KillzXGaming/BfresLibrary](https://github.com/KillzXGaming/BfresLibrary/)
- [python-pillow/Pillow](https://github.com/python-pillow/Pillow) (For BC6/BC7 Importing)
- [Syroot/NintenTools.Bfres](https://gitlab.com/Syroot/NintenTools.Bfres/tree/master/src/Syroot.NintenTools.Bfres)
