"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clean_fbx_export", ROOT / "clean_fbx_export.py")
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.object.armature_add()
arm = bpy.context.object
arm.name = "Hero"
arm.data.bones[0].name = "Root"
bpy.ops.object.mode_set(mode='EDIT')
child = arm.data.edit_bones.new("Child")
child.head = (0, 0, 1)
child.tail = (0, 0, 2)
child.parent = arm.data.edit_bones["Root"]
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.mesh.primitive_cube_add()
mesh = bpy.context.object
mesh.name = "Body"
mesh.data.materials.append(bpy.data.materials.new("Body Material"))
group = mesh.vertex_groups.new(name="Root")
group.add(list(range(8)), 1.0, 'REPLACE')
modifier = mesh.modifiers.new("Rig", 'ARMATURE')
modifier.object = arm
subdivision = mesh.modifiers.new("Subdivision", 'SUBSURF')
subdivision.subdivision_type = 'SIMPLE'
subdivision.levels = 1
mesh.parent = arm
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
settings = bpy.context.scene.clean_fbx
assert settings.orientation_preset == 'CUSTOM'
settings.export_animations = False

with tempfile.TemporaryDirectory() as output:
    settings.output_dir = output
    assert not settings.export_technical_details
    paths, warnings = addon.export_clean_rigs(bpy.context, settings)
    assert not warnings, warnings
    report_path = Path(output) / "Hero.technical.json"
    assert not report_path.exists()

    settings.export_technical_details = True
    paths, warnings = addon.export_clean_rigs(bpy.context, settings)
    assert not warnings, warnings
    report = json.loads(report_path.read_text(encoding="utf-8"))
    exported = report["exported"]
    assert report["file"]["bytes"] == Path(paths[0]).stat().st_size
    assert exported["totals"]["vertices"] == 26  # Evaluated subdivision, not the source's eight.
    assert exported["totals"]["polygons"] == 24
    assert exported["totals"]["triangles_after_triangulation"] == 48
    assert exported["bone_count"] == 2
    assert exported["root_bones"] == ["Root"], exported["bone_hierarchy"]
    assert {b["name"]: b["parent_bone"] for b in exported["bone_hierarchy"]} == {
        "Root": None, "Child": "Root"}
    assert exported["materials"] == ["Body Material"]
    assert exported["meshes"][0]["uv_layers"] == ["UVMap"]
    assert exported["animation_clip_count"] == 0
    assert report["source_scene"]["rigs"][0]["bones"] == 2

    # The report must follow actual triangulation and generated leaf bones.
    settings.triangulate = True
    settings.add_leaf_bones = True
    addon.export_clean_rigs(bpy.context, settings)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exported"]["totals"]["polygons"] == 48
    assert report["exported"]["bone_count"] == 3

    settings.export_animations = True
    settings.add_leaf_bones = False
    pose = arm.pose.bones["Root"]
    for frame, x in ((1, 0), (25, 1)):
        pose.location.x = x
        pose.keyframe_insert(data_path="location", frame=frame)
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 25
    addon.export_clean_rigs(bpy.context, settings)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exported"]["animation_clip_count"] == 1
    clip = report["exported"]["animation_clips"][0]
    assert abs(clip["duration_seconds"] - 24 / report["source_scene"]["fps"]) < 1e-6

    # A report failure must preserve the successfully exported FBX and surface a warning.
    original_writer = addon._write_technical_report
    def failing_writer(*args):
        raise OSError("Simulated report write failure")
    addon._write_technical_report = failing_writer
    paths, warnings = addon.export_clean_rigs(bpy.context, settings)
    assert Path(paths[0]).is_file()
    assert any("technical report" in w and "Simulated" in w for w in warnings)
    addon._write_technical_report = original_writer
    assert not list(Path(output).glob("*.tmp"))

    # Each separate file describes its own rig; a combined file describes both.
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.object.armature_add(location=(3, 0, 0))
    other = bpy.context.object
    other.name = "Other"
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    settings.export_animations = False
    paths, warnings = addon.export_clean_rigs(bpy.context, settings)
    assert len(paths) == 2 and not warnings, warnings
    for path in paths:
        data = json.loads(Path(path).with_suffix(".technical.json").read_text(encoding="utf-8"))
        assert len(data["source_scene"]["rigs"]) == 1
        assert data["file"]["name"] == Path(path).name
    settings.separate_files = False
    paths, warnings = addon.export_clean_rigs(bpy.context, settings, base_name_override="Combined")
    data = json.loads(Path(paths[0]).with_suffix(".technical.json").read_text(encoding="utf-8"))
    assert len(paths) == 1 and not warnings, warnings
    assert len(data["source_scene"]["rigs"]) == 2
    assert data["exported"]["bone_count"] == 3

    # Invalid axis pairs must fail before touching any existing FBX or report.
    sentinel = Path(paths[0]).read_bytes()
    for forward, up in (("Y", "Y"), ("Y", "-Y")):
        settings.axis_forward, settings.axis_up = forward, up
        try:
            addon.export_clean_rigs(bpy.context, settings)
        except addon.CleanFBXError as exc:
            assert "different axes" in str(exc)
        else:
            raise AssertionError("Invalid axes accepted")
        assert Path(paths[0]).read_bytes() == sentinel
    settings.axis_forward, settings.axis_up = '-Z', 'Y'
    settings.primary_bone_axis, settings.secondary_bone_axis = 'X', '-X'
    try:
        addon.export_clean_rigs(bpy.context, settings)
    except addon.CleanFBXError as exc:
        assert "Bone Axis" in str(exc)
    else:
        raise AssertionError("Collinear bone axes accepted")
    settings.primary_bone_axis, settings.secondary_bone_axis = 'Y', 'X'

    # Check actual FBX metadata and skeleton transforms for every valid axis pair.
    from io_scene_fbx import parse_fbx
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    settings.separate_files = True
    settings.export_technical_details = False
    settings.remove_armature_node = False
    original_matrix = arm.matrix_world.copy()
    original_scale = bpy.context.scene.unit_settings.scale_length

    def snapshot(path):
        tree, version = parse_fbx.parse(path)
        assert version == 7400
        objects = addon._first_child(tree, b'Objects')
        models = {e.props[0]: e for e in objects.elems if e.id == b'Model'}
        parents = {c.props[1]: c.props[2] for c in addon._first_child(tree, b'Connections').elems
                   if c.id == b'C' and c.props[0] == b'OO'
                   and c.props[1] in models and (c.props[2] in models or c.props[2] == 0)}
        bones = {uid: addon._global_model_matrix(uid, models, parents)
                 for uid, e in models.items() if e.props[2] == b'LimbNode'}
        props = addon._first_child(addon._first_child(tree, b'GlobalSettings'), b'Properties70')
        globals_ = {p.props[0]: p.props[4] for p in props.elems if p.id == b'P'}
        return bones, globals_

    axes = ('X', 'Y', 'Z', '-X', '-Y', '-Z')
    pairs = [(f, u) for f in axes for u in axes if f[-1] != u[-1]]
    # Exercise output coordinates, then bone orientation independently.
    for mode in ('output', 'bones'):
        for first, second in pairs:
            if mode == 'output':
                settings.axis_forward, settings.axis_up = first, second
            else:
                settings.axis_forward, settings.axis_up = '-Z', 'Y'
                settings.primary_bone_axis, settings.secondary_bone_axis = first, second
            paths, warnings = addon.export_clean_rigs(bpy.context, settings)
            assert not warnings, warnings
            before, globals_ = snapshot(paths[0])
            up = settings.axis_up
            assert globals_[b'UpAxis'] == 'XYZ'.index(up[-1])
            assert globals_[b'UpAxisSign'] == (-1 if up.startswith('-') else 1)
            addon._clean_fbx_file(paths[0], {addon._fbx_uuid_for_object(arm)})
            after, _ = snapshot(paths[0])
            assert before.keys() == after.keys()
            for uid in before:
                assert max(abs(before[uid][r][c] - after[uid][r][c])
                           for r in range(4) for c in range(4)) < 1e-3, (mode, first, second)  # Centimeters.
            assert arm.matrix_world == original_matrix
            assert bpy.context.scene.unit_settings.scale_length == original_scale

    settings.axis_forward, settings.axis_up = '-Y', 'Z'
    settings.primary_bone_axis, settings.secondary_bone_axis = 'X', 'Z'
    settings.remove_armature_node = True
    settings.export_technical_details = True
    paths, warnings = addon.export_clean_rigs(bpy.context, settings)
    assert not warnings, warnings
    data = json.loads(Path(paths[0]).with_suffix('.technical.json').read_text(encoding='utf-8'))
    assert data['export_settings']['axis_forward'] == '-Y'
    assert data['export_settings']['axis_up'] == 'Z'
    assert data['export_settings']['primary_bone_axis'] == 'X'
    assert data['export_settings']['secondary_bone_axis'] == 'Z'

    # Preset callbacks must update both axes atomically and reach the finished FBX/report.
    saved_matrix = arm.matrix_world.copy()
    saved_bone_axes = (settings.primary_bone_axis, settings.secondary_bone_axis)
    for preset, forward, up in (('UNITY', '-Y', 'Z'), ('UNREAL', 'X', 'Z'), ('GODOT', '-Z', 'Y')):
        settings.orientation_preset = preset
        assert settings.orientation_preset == preset
        assert (settings.axis_forward, settings.axis_up) == (forward, up)
        assert (settings.primary_bone_axis, settings.secondary_bone_axis) == saved_bone_axes
        paths, warnings = addon.export_clean_rigs(bpy.context, settings)
        assert not warnings, warnings
        _, globals_ = snapshot(paths[0])
        assert globals_[b'UpAxis'] == 'XYZ'.index(up)
        data = json.loads(Path(paths[0]).with_suffix('.technical.json').read_text(encoding='utf-8'))
        assert data['export_settings']['orientation_preset'] == preset
        assert data['export_settings']['axis_forward'] == forward
        assert data['export_settings']['axis_up'] == up
        assert data['exported']['root_bones'] == ['Root']
        assert arm.matrix_world == saved_matrix

        settings.axis_forward = forward[1:] if forward.startswith('-') else '-' + forward
        assert settings.orientation_preset == 'CUSTOM'
        settings.orientation_preset = preset
        assert (settings.axis_forward, settings.axis_up) == (forward, up)
        settings.axis_up = '-' + up
        assert settings.orientation_preset == 'CUSTOM'

    # Choosing Custom preserves the current values and allows arbitrary valid axes.
    settings.orientation_preset = 'UNITY'
    settings.orientation_preset = 'CUSTOM'
    assert (settings.axis_forward, settings.axis_up) == ('-Y', 'Z')
    settings.axis_forward, settings.axis_up = 'Z', '-X'
    assert settings.orientation_preset == 'CUSTOM'
    assert (settings.axis_forward, settings.axis_up) == ('Z', '-X')

    # Presets are per scene; applying one cannot alter another scene's settings.
    other_scene = bpy.data.scenes.new('Preset Isolation')
    other_settings = other_scene.clean_fbx
    other_settings.orientation_preset = 'UNREAL'
    assert (other_settings.axis_forward, other_settings.axis_up) == ('X', 'Z')
    assert settings.orientation_preset == 'CUSTOM'
    assert (settings.axis_forward, settings.axis_up) == ('Z', '-X')
    assert not addon._ORIENTATION_UPDATES
    bpy.data.scenes.remove(other_scene)
    assert not hasattr(bpy.types, 'CLEANFBX_FH_drop_fbx')
    assert not hasattr(settings, 'drop_enabled')

addon.unregister()
addon.register()
addon.unregister()
print("PASS: reports, animation, multi-rig, 48 axis combinations, engine presets, validation and registration")
