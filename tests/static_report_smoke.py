"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clean_fbx_export", ROOT / "clean_fbx_export.py")
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
settings = bpy.context.scene.clean_fbx
assert settings.technical_report_format == 'LONG'
settings.export_animations = False
settings.export_technical_details = True


def select(*objects):
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def export(name=None):
    selection = set(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    units = bpy.context.scene.unit_settings.scale_length
    paths, warnings = addon.export_clean_rigs(bpy.context, settings, base_name_override=name)
    assert not warnings, warnings
    assert selection == set(bpy.context.selected_objects)
    assert active == bpy.context.view_layer.objects.active
    assert units == bpy.context.scene.unit_settings.scale_length
    reports = [json.loads(Path(path).with_suffix('.technical.json').read_text(encoding='utf-8'))
               for path in paths]
    for path, report in zip(paths, reports):
        assert report['file']['bytes'] == Path(path).stat().st_size
        assert report['file']['name'] == Path(path).name
    return paths, reports


with tempfile.TemporaryDirectory() as directory:
    settings.output_dir = directory
    bpy.ops.mesh.primitive_cube_add(location=(2, 3, 4))
    cube = bpy.context.object
    cube.name = 'Static Prop'
    cube.scale = (2, 3, 4)  # Static models have no armature scale restriction.
    bpy.context.view_layer.update()
    original_transform = cube.matrix_world.copy()
    material = bpy.data.materials.new('Prop Material')
    material.use_nodes = True
    cube.data.materials.append(material)
    image = bpy.data.images.new('Albedo', width=64, height=32)
    image.filepath_raw = str(Path(directory) / 'albedo.png')
    image.file_format = 'PNG'
    image.save()
    image.pack()
    texture = material.node_tree.nodes.new('ShaderNodeTexImage')
    texture.image = image
    shader = next(node for node in material.node_tree.nodes if node.type == 'BSDF_PRINCIPLED')
    material.node_tree.links.new(texture.outputs['Color'], shader.inputs['Base Color'])
    # One image reused for several channels still counts as one exported image.
    material.node_tree.links.new(texture.outputs['Color'], shader.inputs['Roughness'])
    unused = bpy.data.images.new('Unused Image', width=16, height=16)
    unused_node = material.node_tree.nodes.new('ShaderNodeTexImage')
    unused_node.image = unused
    unrelated = bpy.data.images.new('Unrelated Image', width=8, height=8)

    paths, reports = export()
    long_report = reports[0]
    assert Path(paths[0]).name == 'Static Prop.fbx'
    assert cube.matrix_world == original_transform
    assert long_report['report_type'] == 'LONG'
    stats = long_report['exported']
    assert stats['mesh_object_count'] == stats['mesh_geometry_count'] == 1
    assert stats['totals']['vertices'] == 8
    assert stats['totals']['triangles'] == 0
    assert stats['totals']['quads'] == 6
    assert stats['totals']['ngons'] == 0
    assert stats['totals']['triangles_after_triangulation'] == 12
    assert stats['bone_count'] == 0 and not stats['has_root_bone']
    assert not stats['has_named_root_bone'] and stats['root_bones'] == []
    assert stats['material_count'] == 1 and stats['materials'] == ['Prop Material']
    assert stats['texture_count'] == 1, stats['textures']
    exported_texture = stats['textures'][0]
    assert exported_texture['name'] == 'Albedo'
    assert exported_texture['resolution'] == [64, 32]
    assert exported_texture['embedded'] and exported_texture['packed']
    assert exported_texture['materials'] == ['Prop Material']
    assert len(exported_texture['uses']) >= 2
    assert long_report['source_scene']['rigs'] == []
    assert {t['name'] for t in long_report['source_scene']['textures']} == {'Albedo', 'Unused Image'}

    # Re-import the actual static FBX to verify placement, orientation and size.
    before_import = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=paths[0], use_anim=False)
    imported = set(bpy.data.objects) - before_import
    delivered = next(obj for obj in imported if obj.type == 'MESH')
    bpy.context.view_layer.update()

    def bounds(obj):
        points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        return [value for axis in range(3)
                for value in (min(p[axis] for p in points), max(p[axis] for p in points))]

    assert max(abs(a - b) for a, b in zip(bounds(cube), bounds(delivered))) < 1e-4
    for obj in imported:
        bpy.data.objects.remove(obj, do_unlink=True)
    select(cube)

    settings.technical_report_format = 'SHORT'
    _, reports = export()
    short = reports[0]
    assert short['report_type'] == 'SHORT'
    assert 'source_scene' not in short and 'export_settings' not in short
    assert 'bone_hierarchy' not in short['model'] and 'meshes' not in short['model']
    model = short['model']
    assert model['name'] == 'Static Prop' and model['mesh_count'] == 1
    assert model['quads'] == 6 and model['triangles_after_triangulation'] == 12
    assert model['texture_count'] == 1 and model['textures'][0]['resolution'] == [64, 32]
    assert model['bone_count'] == 0 and not model['has_root_bone']
    assert len(json.dumps(short)) < len(json.dumps(long_report))

    settings.embed_textures = False
    settings.triangulate = True
    _, reports = export()
    model = reports[0]['model']
    assert model['triangles'] == 12 and model['quads'] == 0
    assert not model['textures'][0]['embedded']
    settings.triangulate = False

    # A different static mesh must not inherit the first model's materials/textures.
    bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))
    other = bpy.context.object
    other.name = 'Other Prop'
    select(cube, other)
    _, reports = export()
    assert len(reports) == 2
    for report in reports:
        if report['model']['name'] == 'Other Prop':
            assert report['model']['texture_count'] == report['model']['material_count'] == 0

    settings.separate_files = False
    _, reports = export('Static Combined')
    assert len(reports) == 1
    assert reports[0]['model']['mesh_count'] == 2
    assert reports[0]['model']['quads'] == 12
    assert reports[0]['model']['texture_count'] == 1

    # Both report formats must work on a mixed rigged/static selection.
    bpy.ops.object.armature_add()
    arm = bpy.context.object
    arm.name = 'Hero'
    arm.data.bones[0].name = 'Root'
    bpy.ops.mesh.primitive_cube_add()
    body = bpy.context.object
    body.name = 'Body'
    body.parent = arm
    modifier = body.modifiers.new('Rig', 'ARMATURE')
    modifier.object = arm
    weights = body.vertex_groups.new(name='Root')
    weights.add(list(range(8)), 1.0, 'REPLACE')
    select(arm, cube)
    for detail in ('SHORT', 'LONG'):
        settings.technical_report_format = detail
        settings.separate_files = True
        _, reports = export()
        assert len(reports) == 2
        for report in reports:
            stats = report['model'] if detail == 'SHORT' else report['exported']
            assert stats['bone_count'] == (1 if report['file']['name'] == 'Hero.fbx' else 0)
        settings.separate_files = False
        _, reports = export('Mixed')
        stats = reports[0]['model'] if detail == 'SHORT' else reports[0]['exported']
        mesh_count = stats['mesh_count'] if detail == 'SHORT' else stats['mesh_object_count']
        assert mesh_count == 2
        assert stats['bone_count'] == 1 and stats['has_root_bone']
        assert stats['has_named_root_bone'] and stats['root_bones'] == ['Root']
        assert stats['texture_count'] == 1

    # Bone-parented rigid attachments without modifiers still select the rig.
    body.modifiers.remove(modifier)
    select(body)
    assert addon._iter_armatures_from_selection(bpy.context) == [arm]
    _, reports = export()
    assert reports[0]['exported']['bone_count'] == 1
    assert reports[0]['exported']['mesh_object_count'] == 1
    assert reports[0]['exported']['has_named_root_bone']
    arm.data.bones[0].name = 'Hips'
    _, reports = export()
    assert reports[0]['exported']['has_root_bone']
    assert not reports[0]['exported']['has_named_root_bone']

    # A selected static hierarchy is one model; unselected siblings stay out.
    bpy.ops.object.empty_add()
    parent = bpy.context.object
    parent.name = 'Prop Group'
    cube.parent = parent
    other.parent = parent
    settings.separate_files = True
    select(parent, cube, other)
    _, reports = export()
    assert len(reports) == 1 and reports[0]['exported']['mesh_object_count'] == 2
    select(parent, cube)
    _, reports = export()
    assert len(reports) == 1 and reports[0]['exported']['mesh_object_count'] == 1

    # Converting non-mesh geometry also works without a rig.
    bpy.ops.object.text_add()
    text = bpy.context.object
    text.data.body = 'Static'
    _, reports = export()
    assert reports[0]['exported']['mesh_object_count'] == 1
    assert reports[0]['exported']['totals']['vertices'] > 0
    assert reports[0]['exported']['bone_count'] == 0

    # Count mixed topology from the delivered FBX, including shared instances.
    geometry = bpy.data.meshes.new('Mixed Topology')
    geometry.from_pydata(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0),
         (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0),
         (4, 0, 0), (5, 0, 0), (5.5, 1, 0), (4.5, 2, 0), (3.5, 1, 0)],
        [], [(0, 1, 2), (3, 4, 5, 6), (7, 8, 9, 10, 11)])
    first = bpy.data.objects.new('Topology', geometry)
    second = bpy.data.objects.new('Topology Instance', geometry)
    bpy.context.collection.objects.link(first)
    bpy.context.collection.objects.link(second)
    select(first, second)
    settings.separate_files = False
    _, reports = export('Topology Combined')
    stats = reports[0]['exported']
    assert stats['mesh_object_count'] == 2 and stats['mesh_geometry_count'] == 1
    assert stats['totals']['triangles'] == stats['totals']['quads'] == stats['totals']['ngons'] == 1
    assert stats['totals']['triangles_after_triangulation'] == 6
    settings.triangulate = True
    _, reports = export('Topology Triangulated')
    stats = reports[0]['exported']
    # Blender may make a separate evaluated geometry block per instance.
    assert all(mesh['triangles'] == 6 for mesh in stats['meshes'])
    assert stats['totals']['triangles'] == 6 * stats['mesh_geometry_count']
    assert stats['totals']['quads'] == stats['totals']['ngons'] == 0
    settings.triangulate = False
    settings.separate_files = True

    # Names that sanitize identically cannot overwrite another job in the batch.
    cube.name, other.name = 'Prop:One', 'Prop?One'
    select(cube, other)
    paths, _ = export()
    assert len(paths) == len(set(paths)) == 2
    assert all(Path(path).exists() for path in paths)

    # Unsupported/empty selections fail clearly before writing output.
    bpy.ops.object.camera_add()
    try:
        addon.export_clean_rigs(bpy.context, settings)
    except addon.CleanFBXError as exc:
        assert 'Select a mesh' in str(exc)
    else:
        raise AssertionError('Camera-only export should be rejected')
    select()
    assert not addon._export_groups(bpy.context)
    assert not list(Path(directory).glob('*.tmp'))

addon.unregister()
print('PASS: static and mixed exports, Short/Long reports, textures, topology, hierarchy and state restoration')
