# Clean FBX Export

A Blender add-on that exports static models and rigs as game-engine-ready FBX files. Rigs export **without an Armature object node**: the skeleton's root bone becomes the root of the exported hierarchy, so Unity, Unreal, Godot and other importers do not create an extra `Armature` object/transform above your skeleton. Optional Short or Long JSON reports describe each exported model.

## Requirements

- Blender 4.2 or newer.
- The built-in FBX importer/exporter (`io_scene_fbx`) must be enabled (it is by default).

Tested on Blender 5.0 and 5.2.

## Installation

1. Drag **`clean_fbx_export-1.0.0.zip`** from the `builds` folder onto the Blender window (Blender 4.2 or newer).
2. Confirm **Install** in Blender's installation dialog and enable the add-on if prompted.
3. Open the 3D Viewport sidebar (`N`) and select the **Clean FBX** tab.

Keep the ZIP intact; do not extract it before installation. Alternatively, open **Edit > Preferences > Get Extensions**, use the top-right menu's **Install from Disk...**, and select the same ZIP.

If the legacy single-file version is already enabled, disable/remove it before installing the extension to avoid duplicate registration.

### Rebuilding the installable ZIP

From this source directory, run Blender's extension builder:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe' --factory-startup --command extension build --output-dir ../builds
```

The resulting `builds/clean_fbx_export-1.0.0.zip` includes the manifest, extension entry point, exporter, and this README. Older builds may also be available in `builds/old`.

The add-on adds:

- a **Clean FBX** tab in the 3D Viewport sidebar (`N` panel),
- a **File > Export > Clean FBX (.fbx)** menu entry.

## Exporting models and rigs

1. Select static meshes, an Armature, or a mesh attached to an Armature. Curves, surfaces, text, metaballs and empties are also supported.
2. Open the **Clean FBX** panel in the 3D Viewport sidebar.
3. Set the options. Exports go beside your `.blend` file by default; choose another output folder to override.
4. Click **Export FBX**.

The panel keeps the output folder, animation toggle, technical report toggle, and **Export FBX** button visible. Enabling the report reveals **Short / Long** options. The folder icon beside Export opens the output folder. **One File Per Model** appears when multiple models are selected.

Expand **Orientation** to choose an engine preset or set Forward/Up and bone axes manually. Expand **Advanced** for the file prefix, texture/mesh options, skeleton cleanup, and All Actions. Both sections start collapsed.

### Orientation presets

| Preset | Forward | Up |
| --- | --- | --- |
| Custom | User-defined | User-defined |
| Unity | -Y | Z |
| Unreal | X | Z |
| Godot | -Z | Y |

**Custom** is the default and preserves your current axes. Selecting an engine preset applies its Forward/Up values immediately. You can edit either axis at any time; changing a preset's values switches back to Custom. Bone axes remain independently editable.

The Unity preset uses the requested -Y Forward / Z Up export configuration. The Unreal preset uses X Forward / Z Up; enable **Convert Scene** and **Force Front XAxis** in Unreal's FBX import options for this configuration ([Epic's import reference](https://dev.epicgames.com/documentation/en-us/unreal-engine/fbx-import-options-reference-in-unreal-engine)). Godot uses -Z Forward / Y Up; its camera-forward convention is -Z, while oriented model fronts conventionally face +Z ([Godot's model conventions](https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/importing_3d_scenes/model_export_considerations.html)). These presets set FBX export axes; the engine's importer also performs coordinate conversion.

The format is **FBX 7.4 Binary**. Blender's bundled exporter does not offer other output versions, so this add-on does not provide a version selector. Axis settings convert the exported data without rotating objects in the source scene. Forward and Up must use different axes; primary and secondary bone axes must also differ.

Automatic FBX drag-and-drop importing has been removed. You can still drag the extension ZIP into Blender to install it.

The exported file contains:

- the skeleton starting at the root bone, with **no Armature object node**,
- the skinned mesh/meshes and materials,
- animation, if enabled,
- no extra 100x root scale on the skeleton.

FBX coordinates are stored in **centimeters**, with `UnitScaleFactor = 1`. Mesh vertices, shape-key offsets, skeleton translations, skin/bind matrices, and translation animation are converted together; object and bone scale values are preserved. A rig with applied object scale and unscaled bones therefore exports at **1, 1, 1**, without requiring Unreal to put a meter-to-centimeter conversion on the skeleton root. Use **Import Uniform Scale = 1** in Unreal. Scene units determine physical size (with Units set to None, one Blender unit means one meter). Export does not change the scene's unit settings or source objects. This applies to every orientation preset, including Custom, and also when retaining the Armature node.

Keep **One File Per Model** enabled to write one FBX per rig or independent static model. Disable it to combine the selected models into one file. Rigged and static objects can be exported together. Selecting a rig or attached mesh includes the rig's meshes and descendants. Static exports include selected objects; selecting a static parent and its children groups those selected objects into one model. Unselected static children are not added automatically. Names that sanitize to the same file name receive a numeric suffix within the export batch.

## Options

| Option | Description |
| --- | --- |
| Output Folder | Defaults to `//`, the current `.blend` file's folder. Choose another folder to override; clear the field or enter `//` to restore the default. Save an unsaved `.blend` first or choose an absolute output folder. |
| Prefix | Optional prefix for exported file names. |
| Orientation Preset | Custom, Unity (-Y / Z), Unreal (X / Z), or Godot (-Z / Y). Applies Forward/Up axes. |
| Forward / Up | Output coordinate axes. Defaults: -Z Forward, Y Up. |
| Bone Primary / Secondary | Exported bone orientation. Defaults: Y Primary, X Secondary. |
| One File Per Model | Write one FBX per rig or selected static hierarchy instead of combining. |
| Export Technical Details (.json) | Write a technical report beside each FBX. Off by default. |
| Short / Long | Choose a basic model summary or the full technical report. Long is the default. |
| Animations | Bake and export animation. |
| All Actions | Export every action instead of only the active action. |
| Embed Textures | Embed textures so the FBX is self-contained when dragged into an engine. |
| Deform Bones Only | Export only bones that actually deform a mesh. |
| Add Leaf Bones | Add Blender's helper leaf bones. Usually off for game rigs. |
| Triangulate | Triangulate exported meshes. |
| Remove Armature Node | Recommended. Uncheck only to inspect the raw FBX with the Armature node. |

## Technical details report

Enable **Technical report (JSON)** in the Clean FBX panel to write a matching report, for example `Hero.fbx` and `Hero.technical.json`. Separate exports get one report per FBX; combined models share one report. Choose **Short** or **Long** below the toggle. Both formats work for static and rigged models and use the same report filename.

**Short** provides a compact `model` summary with the model name, mesh object names/count, geometry count, vertices, polygons, triangles, quads, n-gons, triangle equivalents, material count/names, texture count/list with resolution in pixels and embedding status, bone count, root-bone presence and root-bone names. It also includes the FBX filename, byte size, format version and warnings. Static models report zero bones and no root bone.

**Long** preserves the existing report structure and information, with additional topology, material and texture details:

- FBX file name, size in bytes, format version, timestamp, and Blender/add-on versions.
- Exported mesh and geometry counts, vertices, edges, polygons, triangles, quads, n-gons, triangle equivalents, UV layers, and material count/names.
- Texture count/list, per-image resolution, file paths, file format, color space, channels, packed/embedded status and material/channel usage.
- Exported bone count, root bones, and bone hierarchy, plus source rig/deform-bone counts for comparison.
- Exported animation clip names and timing in seconds, scene FPS and units, export options, and warnings.

Exported stats are read from the **finished FBX**, reflecting modifiers, triangulation, bone filtering, generated leaf bones, and animation baking. Geometry totals count shared geometry once; triangle equivalents describe how many triangles the polygons produce when triangulated. Source scene metadata is labeled separately. Animation clips are actual FBX takes rather than a list of every action in the Blender file.

`triangles` and `quads` count faces in the delivered FBX. `triangles_after_triangulation` includes the triangles that quads and n-gons would produce. With **Triangulate** enabled, the exported quad count is zero. `has_root_bone` means the skeleton has a top-level bone; `has_named_root_bone` additionally checks whether a top-level bone is named `Root` (case-insensitive, with optional namespace). Both formats list the actual root-bone names.

The exported texture list includes images actually referenced by the FBX, with duplicate image uses counted once. FBX does not provide reliable image dimensions, so resolutions come from the corresponding Blender image (`[width, height]`, or `null` if unavailable). Long reports also inventory image nodes in source materials, including node groups, separately under `source_scene.textures`; unsupported or disconnected shader nodes may appear there without being exported. Texture embedding requires saved or packed image data; the reported `embedded` value reflects data actually present in the FBX.

Reports replace an existing matching report atomically. Turning the option off leaves previous reports untouched. If a report cannot be written, the FBX is kept and Blender shows a warning.

## How the Armature node is removed

Blender's FBX exporter otherwise writes an `Armature` object node above the skeleton, often with unit/axis scale. Simply deleting that node would move the rig, break the bind pose or break the skinning.

This add-on:

1. Exports without putting unit conversion on object scales, then bakes the FBX's distance values to centimeters and sets its unit scale to 1. The Armature node retains its original scale of 1 rather than carrying a 100x conversion.
2. Rewrites the FBX hierarchy:
   - bakes the Armature object's remaining rotation/translation into the root bone(s),
   - removes the Armature Model and NodeAttribute,
   - reparents root bones to the Armature's parent (or world),
   - removes the Armature bind-pose node and the Armature object's animation curves,
   - updates skin-cluster associate matrices so skinned meshes stay in place,
   - preserves bone bind matrices and animations.
3. Writes the result atomically through a temporary file.

## Notes and limitations

- The Armature object must have a scale of 1.0. Apply its scale before exporting; otherwise the add-on reports an error instead of producing a shifted rig.
- **Object-level transform animation on the Armature object** is not preserved, because that object node is removed. Animate the root bone instead. A warning is shown when this is detected.
- Root-bone animation and root motion are preserved.

## Troubleshooting

- **"Could not find the exported Armature object"** — make sure the Armature is selected and visible in the current view layer.
- **"has an object scale other than 1.0"** — apply the Armature's scale (`Ctrl+A > Scale`) before exporting.
- **Animation looks one frame off after re-importing into Blender** — this is a known Blender FBX *importer* offset. The exported file itself is correct.

## Uninstall

Disable/remove the add-on in **Edit > Preferences > Add-ons**. The panel and export menu entry are removed automatically.
