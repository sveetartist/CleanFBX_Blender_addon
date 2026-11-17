"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import importlib.util
from pathlib import Path
import sys
import tempfile

import bpy

source = Path(__file__).resolve().parents[1] / 'clean_fbx_export.py'
spec = importlib.util.spec_from_file_location('clean_fbx_export', source)
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()
settings = bpy.context.scene.clean_fbx
assert settings.output_dir == '//'
assert not bpy.data.filepath
try:
    addon._resolve_output_dir(settings)
except addon.CleanFBXError as exc:
    assert 'Save the .blend' in str(exc)
else:
    raise AssertionError('Unsaved default must ask for a save or explicit output folder')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    override = str(root / 'exports')
    settings.output_dir = override
    assert addon._resolve_output_dir(settings) == override
    assert settings.output_dir == override

    settings.output_dir = '//'
    bpy.ops.wm.save_as_mainfile(filepath=str(root / 'project.blend'))
    assert addon._resolve_output_dir(settings) == str(root)
    settings.output_dir = ''
    assert addon._resolve_output_dir(settings) == str(root)
    settings.output_dir = '//custom'
    assert addon._resolve_output_dir(settings) == str(root / 'custom')
    settings.output_dir = '//'
    moved = root / 'moved'
    moved.mkdir()
    bpy.ops.wm.save_as_mainfile(filepath=str(moved / 'project.blend'))
    assert addon._resolve_output_dir(settings) == str(moved)
    settings.output_dir = override
    assert addon._resolve_output_dir(settings) == override

addon.unregister()
print('PASS: saved/unsaved defaults, blank reset, relative/absolute overrides and Save As')
