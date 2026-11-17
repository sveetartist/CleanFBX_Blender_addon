"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import importlib.util
import math
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('clean_fbx_export', ROOT / 'clean_fbx_export.py')
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()
from io_scene_fbx import parse_fbx

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.object.armature_add(location=(2, 3, 4))
arm = bpy.context.object
arm.name = 'Hero'
arm.rotation_euler.z = math.radians(30)
arm.data.bones[0].name = 'Root'
bpy.ops.object.mode_set(mode='EDIT')
child = arm.data.edit_bones.new('Child')
child.head, child.tail = (0, 0, 1), (0, 0, 2)
child.parent = arm.data.edit_bones['Root']
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.mesh.primitive_cube_add()
mesh = bpy.context.object
mesh.name = 'Body'
mesh.parent = arm
group = mesh.vertex_groups.new(name='Child')
group.add(list(range(8)), 1.0, 'REPLACE')
modifier = mesh.modifiers.new('Rig', 'ARMATURE')
modifier.object = arm
mesh.shape_key_add(name='Basis')
shape = mesh.shape_key_add(name='Smile')
shape.data[0].co.x += 0.25
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 5
for frame, x in ((1, 0), (5, 1)):
    arm.pose.bones['Root'].location.x = x
    arm.pose.bones['Root'].keyframe_insert(data_path='location', frame=frame)
    arm.pose.bones['Child'].rotation_euler.y = x * 0.3
    arm.pose.bones['Child'].keyframe_insert(data_path='rotation_euler', frame=frame)
    arm.pose.bones['Child'].scale = (1 + x * 0.2,) * 3
    arm.pose.bones['Child'].keyframe_insert(data_path='scale', frame=frame)
scene.frame_set(1)
settings = scene.clean_fbx
settings.embed_textures = False


def select_source():
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm


def world_vertices(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return [evaluated.matrix_world @ vertex.co for vertex in evaluated.data.vertices]


def assert_close(a, b, tolerance=2e-4):
    assert len(a) == len(b)
    assert max(abs(x - y) for x, y in zip(a, b)) < tolerance, (a, b)


with tempfile.TemporaryDirectory() as output:
    settings.output_dir = output
    for system, length in (('NONE', 1), ('METRIC', 1), ('METRIC', 0.01), ('METRIC', 0.1)):
        scene.unit_settings.system = system
        scene.unit_settings.scale_length = length
        length = scene.unit_settings.scale_length  # RNA stores a 32-bit float.
        factor = 100 if system == 'NONE' else 100 * length
        for preset in ('CUSTOM', 'UNREAL'):
            settings.axis_forward, settings.axis_up = '-Z', 'Y'
            settings.orientation_preset = preset
            for remove in (False, True):
                settings.remove_armature_node = remove
                select_source()
                scene.frame_set(1)
                saved_matrix = arm.matrix_world.copy()
                paths, warnings = addon.export_clean_rigs(bpy.context, settings)
                assert not warnings, warnings
                assert arm.matrix_world == saved_matrix
                assert tuple(arm.scale) == (1, 1, 1)
                assert scene.unit_settings.system == system
                assert scene.unit_settings.scale_length == length

                tree, _ = parse_fbx.parse(paths[0])
                props = addon._first_child(addon._first_child(tree, b'GlobalSettings'), b'Properties70')
                units = {p.props[0]: p.props[4] for p in props.elems if p.id == b'P'}
                assert units[b'UnitScaleFactor'] == units[b'OriginalUnitScaleFactor'] == 1
                objects = addon._first_child(tree, b'Objects')
                models = {e.props[0]: e for e in objects.elems if e.id == b'Model'}
                assert (addon._fbx_uuid_for_object(arm) not in models) == remove
                for model in models.values():
                    assert_close(addon._model_trs(model).get(b'Lcl Scaling', (1, 1, 1)), (1, 1, 1))
                # A cube side is 2 Blender units; vertex/shape coordinates must
                # change together, while rotations, weights and scale stay intact.
                geometries = [e for e in objects.elems if e.id == b'Geometry']
                base = next(e for e in geometries if e.props[2] == b'Mesh')
                positions = addon._first_child(base, b'Vertices').props[0]
                assert abs(max(positions) - factor) < 1e-4
                morph = next(e for e in geometries if e.props[2] == b'Shape')
                indices = addon._first_child(morph, b'Indexes').props[0]
                deltas = addon._first_child(morph, b'Vertices').props[0]
                assert 0 in indices
                for i, vertex_index in enumerate(indices):
                    assert_close(deltas[i * 3:i * 3 + 3],
                                 (0.25 * factor if vertex_index == 0 else 0, 0, 0))

                # Verify the actual delivered skin and animation by reimporting.
                # anim_offset=0 avoids Blender's default extra-frame offset.
                reference = {}
                for frame in (1, 3, 5):
                    scene.frame_set(frame)
                    reference[frame] = world_vertices(mesh)
                existing = set(bpy.data.objects)
                bpy.ops.import_scene.fbx(filepath=paths[0], use_anim=True, anim_offset=0)
                imported = set(bpy.data.objects) - existing
                body = next(obj for obj in imported if obj.type == 'MESH')
                for frame in (1, 3, 5):
                    scene.frame_set(frame)
                    actual = world_vertices(body)
                    for expected, delivered in zip(reference[frame], actual):
                        assert_close(expected, delivered)
                for obj in imported:
                    bpy.data.objects.remove(obj, do_unlink=True)

                # Normalizing an already-centimeter file must be a no-op.
                before = Path(paths[0]).read_bytes()
                addon._normalize_fbx_units(paths[0])
                assert Path(paths[0]).read_bytes() == before

    assert not list(Path(output).glob('*.unittmp'))
    assert not list(Path(output).glob('*.cleantmp'))

addon.unregister()
print('PASS: centimeter FBX, scale 1, units, axes, shape keys, skinning, root motion and scale animation')
