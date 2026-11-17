bl_info = {
    "name": "Clean FBX Export",
    "author": "OpenCode",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Clean FBX",
    "description": "Export models and rigs as game-ready FBX files with optional technical reports",
    "category": "Import-Export",
}

import bpy
import os
import re
import math
import json
import tempfile
from datetime import datetime, timezone
from mathutils import Matrix, Vector, Euler

from bpy.props import (
    BoolProperty,
    EnumProperty,
    PointerProperty,
    StringProperty,
)


# ---------------------------------------------------------------------------
# Settings / UI state
# ---------------------------------------------------------------------------

_AXIS_ITEMS = tuple((axis, axis, "Use the %s axis" % axis)
                    for axis in ('X', 'Y', 'Z', '-X', '-Y', '-Z'))

_ORIENTATION_PRESETS = {
    'UNITY': ('-Y', 'Z'),
    'UNREAL': ('X', 'Z'),
    'GODOT': ('-Z', 'Y'),
}
_ORIENTATION_UPDATES = set()


def _update_orientation_preset(settings, context):
    axes = _ORIENTATION_PRESETS.get(settings.orientation_preset)
    if axes is None:
        return
    key = settings.as_pointer()
    _ORIENTATION_UPDATES.add(key)
    try:
        settings.axis_forward, settings.axis_up = axes
    finally:
        _ORIENTATION_UPDATES.discard(key)


def _update_orientation_axis(settings, context):
    if settings.as_pointer() in _ORIENTATION_UPDATES:
        return
    axes = _ORIENTATION_PRESETS.get(settings.orientation_preset)
    if axes is not None and (settings.axis_forward, settings.axis_up) != axes:
        settings.orientation_preset = 'CUSTOM'


class CleanFBXSettings(bpy.types.PropertyGroup):
    orientation_preset: EnumProperty(
        name="Preset",
        description="Set Forward and Up axes for an engine, or customize them manually",
        items=(('CUSTOM', "Custom", "Choose any Forward and Up axes manually"),
               ('UNITY', "Unity", "-Y Forward, Z Up"),
               ('UNREAL', "Unreal", "X Forward, Z Up; enable Force Front XAxis in Unreal's FBX importer"),
               ('GODOT', "Godot", "-Z Forward, Y Up")),
        default='CUSTOM',
        update=_update_orientation_preset,
    )
    axis_forward: EnumProperty(
        name="Forward", description="Forward axis in the exported coordinate system",
        items=_AXIS_ITEMS, default='-Z', update=_update_orientation_axis,
    )
    axis_up: EnumProperty(
        name="Up", description="Up axis in the exported coordinate system",
        items=_AXIS_ITEMS, default='Y', update=_update_orientation_axis,
    )
    primary_bone_axis: EnumProperty(
        name="Primary Bone Axis", description="Axis pointing along each exported bone",
        items=_AXIS_ITEMS, default='Y',
    )
    secondary_bone_axis: EnumProperty(
        name="Secondary Bone Axis", description="Secondary orientation axis of exported bones",
        items=_AXIS_ITEMS, default='X',
    )
    output_dir: StringProperty(
        name="Output Folder",
        description="Defaults to the .blend file's folder (//). Choose another folder to override",
        subtype='DIR_PATH',
        options={'PATH_SUPPORTS_BLEND_RELATIVE'} if bpy.app.version >= (5, 0, 0) else set(),
        default="//",
    )
    filename_prefix: StringProperty(
        name="Prefix",
        description="Optional prefix added to every exported file name",
        default="",
    )
    separate_files: BoolProperty(
        name="One File Per Model",
        description="Write one FBX per rig or selected static object hierarchy instead of combining models",
        default=True,
    )
    export_technical_details: BoolProperty(
        name="Export Technical Details (.json)",
        description="Write model, skeleton, animation and export statistics beside each FBX",
        default=False,
    )
    technical_report_format: EnumProperty(
        name="Report Detail",
        description="Choose a compact model summary or the full technical report",
        items=(('SHORT', "Short", "Basic model, topology, material, texture and skeleton information"),
               ('LONG', "Long", "Full technical report with geometry, hierarchy, animation and source details")),
        default='LONG',
    )
    export_animations: BoolProperty(
        name="Animations",
        description="Bake and export animation",
        default=True,
    )
    export_all_actions: BoolProperty(
        name="All Actions",
        description="Export every action instead of only the active action",
        default=False,
    )
    embed_textures: BoolProperty(
        name="Embed Textures",
        description="Embed textures in the FBX so the file can be dragged into an engine",
        default=True,
    )
    deform_bones_only: BoolProperty(
        name="Deform Bones Only",
        description="Export only bones that actually deform a mesh",
        default=False,
    )
    add_leaf_bones: BoolProperty(
        name="Add Leaf Bones",
        description="Add Blender's helper leaf bones to bone ends",
        default=False,
    )
    triangulate: BoolProperty(
        name="Triangulate",
        description="Triangulate exported meshes",
        default=False,
    )
    remove_armature_node: BoolProperty(
        name="Remove Armature Node",
        description="Remove the Armature object node from the FBX hierarchy (recommended)",
        default=True,
    )
    last_export_dir: StringProperty(
        name="Last Export Folder",
        default="",
        options={'HIDDEN'},
    )
    last_export_status: StringProperty(
        name="Last Export Status",
        default="",
        options={'HIDDEN'},
    )


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

class CleanFBXError(RuntimeError):
    pass


def _resolve_output_dir(settings):
    raw = settings.output_dir.strip() or "//"
    if raw.startswith("//") and not bpy.data.filepath:
        raise CleanFBXError("Save the .blend file or choose an absolute Output Folder before exporting.")
    path = bpy.path.abspath(raw)
    return os.path.abspath(path)


def _safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip().rstrip(". ")
    return name or "rig"


def _selection_objects(context):
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    return selected or ([active] if active else [])


def _iter_armatures_from_selection(context):
    found = []
    seen = set()

    def add(obj):
        if obj and obj.type == 'ARMATURE' and obj.name not in seen:
            seen.add(obj.name)
            found.append(obj)

    for obj in _selection_objects(context):
        if obj.type == 'ARMATURE':
            add(obj)
        elif obj.type == 'MESH':
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    add(mod.object)

        parent = obj.parent
        while parent:
            add(parent)
            parent = parent.parent
    return found


def _rig_objects(arm):
    objs = {arm}

    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object == arm:
                objs.add(obj)
                break

    def add_descendants(obj):
        for child in obj.children:
            if child not in objs:
                objs.add(child)
            add_descendants(child)

    add_descendants(arm)
    return objs


def _add_selected_ancestors(arm, objects, selected):
    parent = arm.parent
    while parent:
        if parent in selected:
            objects.add(parent)
        parent = parent.parent


def _export_groups(context):
    """Keep rigs intact; group selected static objects by their selected ancestors."""
    selected = set(_selection_objects(context))
    groups = []
    covered = set()
    for arm in _iter_armatures_from_selection(context):
        objects = _rig_objects(arm)
        _add_selected_ancestors(arm, objects, selected)
        groups.append((arm, objects))
        covered.update(objects)

    standalone = selected - covered
    standalone = {obj for obj in standalone
                  if obj.type in {'MESH', 'EMPTY', 'CURVE', 'SURFACE', 'FONT', 'META'}}
    static_groups = {}
    for obj in sorted(standalone, key=lambda obj: obj.name):
        root = obj
        parent = obj.parent
        while parent:
            if parent in standalone:
                root = parent
            parent = parent.parent
        static_groups.setdefault(root, set()).add(obj)
    groups.extend(static_groups.items())
    return groups


def _iter_action_fcurves(action):
    if action is None:
        return
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        try:
            for fc in legacy:
                yield fc
        except Exception:
            pass
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for bag in getattr(strip, "channelbags", ()):
                for fc in getattr(bag, "fcurves", ()):
                    yield fc


def _has_object_level_animation(arm):
    anim = arm.animation_data
    if not anim:
        return False

    object_paths = {
        "location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle",
        "scale", "delta_location", "delta_rotation_euler", "delta_rotation_quaternion",
        "delta_scale",
    }

    actions = []
    if anim.action:
        actions.append(anim.action)
    for track in getattr(anim, "nla_tracks", ()):
        for strip in getattr(track, "strips", ()):
            if getattr(strip, "action", None):
                actions.append(strip.action)

    for action in actions:
        for fc in _iter_action_fcurves(action):
            if fc.data_path in object_paths:
                return True
    return False


class _ExportContext:
    """Temporarily isolate the selection/hide state used by the exporter."""

    def __init__(self, context, objects, active):
        self.context = context
        self.objects = list(objects)
        self.active = active
        self.saved_selection = []
        self.saved_active = None
        self.saved_mode = 'OBJECT'
        self.hide_states = []

    def __enter__(self):
        ctx = self.context
        self.saved_selection = list(ctx.selected_objects)
        self.saved_active = ctx.view_layer.objects.active
        if self.saved_active and self.saved_active.mode != 'OBJECT':
            self.saved_mode = self.saved_active.mode
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        for obj in self.objects:
            try:
                self.hide_states.append((obj, obj.hide_get(), obj.hide_viewport, obj.hide_render))
                obj.hide_set(False)
            except Exception:
                pass
            obj.hide_viewport = False
            obj.hide_render = False

        for obj in list(ctx.selected_objects):
            try:
                obj.select_set(False)
            except Exception:
                pass
        for obj in self.objects:
            try:
                obj.select_set(True)
            except Exception:
                pass
        ctx.view_layer.objects.active = self.active
        ctx.view_layer.update()
        return self

    def __exit__(self, exc_type, exc, tb):
        ctx = self.context
        for obj in list(ctx.selected_objects):
            try:
                obj.select_set(False)
            except Exception:
                pass
        for obj in self.saved_selection:
            try:
                if obj.name in ctx.view_layer.objects:
                    obj.select_set(True)
            except Exception:
                pass
        ctx.view_layer.objects.active = self.saved_active
        for obj, hidden, hide_viewport, hide_render in self.hide_states:
            try:
                obj.hide_set(hidden)
            except Exception:
                pass
            obj.hide_viewport = hide_viewport
            obj.hide_render = hide_render

        if self.saved_mode != 'OBJECT' and self.saved_active:
            try:
                ctx.view_layer.objects.active = self.saved_active
                bpy.ops.object.mode_set(mode=self.saved_mode)
            except Exception:
                pass
        ctx.view_layer.update()
        return False


def _export_objects_fbx(filepath, objects, active, settings):
    scene = bpy.context.scene
    scene.frame_set(scene.frame_current)

    # Keep unit conversion out of object transforms. After export, bake the
    # FBX unit factor into all distances so centimeter importers such as Unreal
    # do not introduce a 100x skeleton/root scale. Source scene units are honored.
    with _ExportContext(bpy.context, objects, active):
        bpy.ops.export_scene.fbx(
            filepath=filepath,
            check_existing=False,
            use_selection=True,
            use_visible=False,
            use_active_collection=False,
            object_types={'ARMATURE', 'MESH', 'EMPTY', 'OTHER'},
            apply_scale_options='FBX_SCALE_ALL',
            apply_unit_scale=True,
            use_space_transform=True,
            bake_space_transform=False,
            global_scale=1.0,
            axis_forward=settings.axis_forward,
            axis_up=settings.axis_up,
            primary_bone_axis=settings.primary_bone_axis,
            secondary_bone_axis=settings.secondary_bone_axis,
            use_mesh_modifiers=True,
            mesh_smooth_type='FACE',
            use_triangles=settings.triangulate,
            use_tspace=False,
            use_custom_props=False,
            use_metadata=True,
            add_leaf_bones=settings.add_leaf_bones,
            use_armature_deform_only=settings.deform_bones_only,
            armature_nodetype='NULL',
            bake_anim=settings.export_animations,
            bake_anim_use_all_bones=False,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=settings.export_all_actions,
            bake_anim_force_startend_keying=True,
            bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0,
            path_mode='COPY' if settings.embed_textures else 'AUTO',
            embed_textures=settings.embed_textures,
            batch_mode='OFF',
        )
    _normalize_fbx_units(filepath)


# ---------------------------------------------------------------------------
# FBX tree helpers (works on io_scene_fbx.parse_fbx trees)
# ---------------------------------------------------------------------------

class _TreeElem:
    """Small mutable element used when a needed FBX property is missing."""

    __slots__ = ("id", "props", "props_type", "elems")

    def __init__(self, elem_id, props=None, props_type=None, elems=None):
        self.id = elem_id
        self.props = list(props or [])
        self.props_type = bytearray(props_type or ())
        self.elems = list(elems or [])


def _child_elems(parent, elem_id):
    return [e for e in parent.elems if e.id == elem_id]


def _first_child(parent, elem_id):
    for e in parent.elems:
        if e.id == elem_id:
            return e
    return None


def _model_trs(model):
    trs = {}
    props70 = _first_child(model, b'Properties70')
    if props70 is None:
        return trs
    for prop in props70.elems:
        if prop.id == b'P' and len(prop.props) >= 7:
            name = prop.props[0]
            if name in (b'Lcl Translation', b'Lcl Rotation', b'Lcl Scaling'):
                trs[name] = tuple(float(v) for v in prop.props[4:7])
    return trs


def _make_p_element(name, values):
    prop = _TreeElem(
        b'P',
        [name, name, b'', b'A'] + [float(v) for v in values],
        [ord('S'), ord('S'), ord('S'), ord('S'), ord('D'), ord('D'), ord('D')],
        [],
    )
    return prop


def _set_model_trs(model, translation=None, rotation_deg=None, scale=None):
    props70 = _first_child(model, b'Properties70')
    if props70 is None:
        props70 = _TreeElem(b'Properties70')
        model.elems.append(props70)

    values = {}
    if translation is not None:
        values[b'Lcl Translation'] = translation
    if rotation_deg is not None:
        values[b'Lcl Rotation'] = rotation_deg
    if scale is not None:
        values[b'Lcl Scaling'] = scale

    for name, vals in values.items():
        found = False
        for prop in props70.elems:
            if prop.id == b'P' and len(prop.props) >= 7 and prop.props[0] == name:
                prop.props[4:7] = [float(v) for v in vals]
                found = True
                break
        if not found:
            props70.elems.append(_make_p_element(name, vals))


def _model_matrix(model):
    trs = _model_trs(model)
    t = Vector(trs.get(b'Lcl Translation', (0.0, 0.0, 0.0)))
    r = Euler(
        [math.radians(v) for v in trs.get(b'Lcl Rotation', (0.0, 0.0, 0.0))],
        'XYZ',
    ).to_matrix().to_4x4()
    s = Vector(trs.get(b'Lcl Scaling', (1.0, 1.0, 1.0)))
    return Matrix.Translation(t) @ r @ Matrix.Diagonal(s).to_4x4()


def _global_model_matrix(uid, models, parent_of):
    chain = []
    seen = set()
    while uid and uid in models and uid not in seen:
        seen.add(uid)
        chain.append(models[uid])
        uid = parent_of.get(uid, 0)
    matrix = Matrix.Identity(4)
    for model in reversed(chain):
        matrix = matrix @ _model_matrix(model)
    return matrix


def _matrix_to_fbx_values(matrix):
    # FBX stores matrices column-major.
    return [float(matrix[row][col]) for col in range(4) for row in range(4)]


def _pose_global_matrix(objects, uid):
    for pose in _child_elems(objects, b'Pose'):
        for pose_node in _child_elems(pose, b'PoseNode'):
            node = _first_child(pose_node, b'Node')
            if node is None or node.props[0] != uid:
                continue
            matrix = _first_child(pose_node, b'Matrix')
            if matrix is None:
                continue
            values = [float(v) for v in matrix.props[0]]
            return Matrix([values[i:i + 4] for i in range(0, 16, 4)]).transposed()
    return None


def _model_name(model):
    try:
        return model.props[1].split(b'\x00', 1)[0].decode('utf-8', 'replace')
    except Exception:
        return "<unnamed>"


def _remove_pose_entries(objects, removed_uids):
    for pose in _child_elems(objects, b'Pose'):
        kept = []
        for pose_node in pose.elems:
            if pose_node.id == b'PoseNode':
                node = _first_child(pose_node, b'Node')
                if node is not None and node.props[0] in removed_uids:
                    continue
            kept.append(pose_node)
        pose.elems[:] = kept
        count_elem = _first_child(pose, b'NbPoseNodes')
        if count_elem is not None:
            count = sum(1 for e in pose.elems if e.id == b'PoseNode')
            count_elem.props[0] = count


def _convert_tree(elem, encode_bin, data_types):
    out = encode_bin.FBXElem(elem.id)
    for value, data_type in zip(elem.props, elem.props_type):
        if data_type == data_types.BOOL:
            out.add_bool(bool(value))
        elif data_type == data_types.CHAR:
            out.add_char(value)
        elif data_type == data_types.INT8:
            out.add_int8(int(value))
        elif data_type == data_types.INT16:
            out.add_int16(int(value))
        elif data_type == data_types.INT32:
            out.add_int32(int(value))
        elif data_type == data_types.INT64:
            out.add_int64(int(value))
        elif data_type == data_types.FLOAT32:
            out.add_float32(float(value))
        elif data_type == data_types.FLOAT64:
            out.add_float64(float(value))
        elif data_type == data_types.BYTES:
            out.add_bytes(value)
        elif data_type == data_types.STRING:
            out.add_string(value)
        elif data_type == data_types.INT32_ARRAY:
            out.add_int32_array(value)
        elif data_type == data_types.INT64_ARRAY:
            out.add_int64_array(value)
        elif data_type == data_types.FLOAT32_ARRAY:
            out.add_float32_array(value)
        elif data_type == data_types.FLOAT64_ARRAY:
            out.add_float64_array(value)
        elif data_type == data_types.BOOL_ARRAY:
            out.add_bool_array(value)
        elif data_type == data_types.BYTE_ARRAY:
            out.add_byte_array(value)
        else:
            raise CleanFBXError("Unsupported FBX property type: %r" % (data_type,))
    out.elems = [_convert_tree(child, encode_bin, data_types) for child in elem.elems]
    return out


def _normalize_fbx_units(filepath):
    """Bake FBX distances to centimeters without changing dimensionless scale.

    Converting only UnitScaleFactor would shrink the asset. All positions,
    shape deltas, bind matrices and translation animation must change together.
    This operates on Blender's baked FBX output, whose curves use linear keys.
    """
    from io_scene_fbx import parse_fbx, encode_bin, data_types

    root, version = parse_fbx.parse(filepath)
    globals_ = _first_child(root, b'GlobalSettings')
    props = _first_child(globals_, b'Properties70') if globals_ else None
    unit = next((p for p in props.elems
                 if p.id == b'P' and p.props[0] == b'UnitScaleFactor'), None) if props else None
    if unit is None:
        raise CleanFBXError("Exported FBX is missing its unit scale.")
    factor = float(unit.props[4])
    if not math.isfinite(factor) or factor <= 0:
        raise CleanFBXError("Exported FBX has an invalid unit scale.")
    if factor == 1.0:
        return

    objects = _first_child(root, b'Objects')
    connections = _first_child(root, b'Connections')
    # Use connections rather than names: there can be multiple takes and nodes
    # with identical names. Rotation, scale and morph-weight curves stay intact.
    translation_nodes = {
        c.props[1] for c in connections.elems
        if c.id == b'C' and len(c.props) >= 4
        and c.props[0] == b'OP' and c.props[3] == b'Lcl Translation'
    }
    translation_curves = {
        c.props[1] for c in connections.elems
        if c.id == b'C' and len(c.props) >= 4
        and c.props[0] == b'OP' and c.props[2] in translation_nodes
    }
    distance_properties = {
        b'Lcl Translation', b'RotationOffset', b'RotationPivot',
        b'ScalingOffset', b'ScalingPivot', b'GeometricTranslation',
        b'TranslationMin', b'TranslationMax',
    }

    def convert_distances(elem, parent_id=None):
        if elem.id == b'P' and len(elem.props) >= 5:
            if elem.props[0] in distance_properties:
                elem.props[4:] = [float(v) * factor for v in elem.props[4:]]
        elif parent_id == b'Geometry' and elem.id == b'Vertices':
            # Mesh positions and blend-shape offsets use the same length unit.
            values = elem.props[0]
            for i in range(len(values)):
                values[i] *= factor
        elif ((parent_id == b'PoseNode' and elem.id == b'Matrix')
              or (parent_id == b'Deformer' and elem.id in
                  (b'Transform', b'TransformLink', b'TransformAssociateModel'))):
            # S @ M @ inverse(S): only the translation of an affine matrix
            # changes when both its input and output use centimeters.
            values = elem.props[0]
            for i in (12, 13, 14):
                values[i] *= factor
        for child in elem.elems:
            convert_distances(child, elem.id)

    convert_distances(root)
    for elem in objects.elems:
        if elem.id == b'AnimationCurveNode' and elem.props[0] in translation_nodes:
            defaults = _first_child(elem, b'Properties70')
            if defaults:
                for prop in defaults.elems:
                    if prop.id == b'P' and prop.props[0] in (b'd|X', b'd|Y', b'd|Z'):
                        prop.props[4] *= factor
        elif elem.id == b'AnimationCurve' and elem.props[0] in translation_curves:
            default = _first_child(elem, b'Default')
            if default:
                default.props[0] *= factor
            values = _first_child(elem, b'KeyValueFloat')
            if values:
                for i in range(len(values.props[0])):
                    values.props[0][i] *= factor

    for prop in props.elems:
        if prop.id == b'P' and prop.props[0] in (b'UnitScaleFactor', b'OriginalUnitScaleFactor'):
            prop.props[4] = 1.0

    temporary_path = filepath + ".unittmp"
    try:
        encode_bin.write(temporary_path, _convert_tree(root, encode_bin, data_types), version)
        os.replace(temporary_path, filepath)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


# ---------------------------------------------------------------------------
# Animation curve helpers
# ---------------------------------------------------------------------------

def _curve_arrays(curve):
    key_time = _first_child(curve, b'KeyTime')
    key_value = _first_child(curve, b'KeyValueFloat')
    if key_time is None or key_value is None:
        return None, None
    return key_time.props[0], key_value.props[0]


def _curve_default(curve, value):
    default = _first_child(curve, b'Default')
    if default is not None and default.props:
        default.props[0] = float(value)


def _same_times(times_a, times_b):
    if len(times_a) != len(times_b):
        return False
    return all(a == b for a, b in zip(times_a, times_b))


def _get_channel_curves(objects_by_uid, curves_by_acn, acn_uid):
    result = {}
    for prop, curve_uid in curves_by_acn.get(acn_uid, {}).items():
        if prop in (b'd|X', b'd|Y', b'd|Z'):
            curve = objects_by_uid.get(curve_uid)
            if curve is not None:
                result[prop] = curve
    return result


def _transform_root_translation_curves(objects_by_uid, curves_by_acn, acn_by_parent,
                                       uid, base_t, a_translation, a_rotation_matrix, a_scale):
    acn_uid = acn_by_parent.get(uid, {}).get(b'Lcl Translation')
    if acn_uid is None:
        return
    curves = _get_channel_curves(objects_by_uid, curves_by_acn, acn_uid)
    if not curves:
        return

    axis_props = (b'd|X', b'd|Y', b'd|Z')
    arrays = {}
    reference_times = None
    for axis, prop in enumerate(axis_props):
        curve = curves.get(prop)
        if curve is None:
            continue
        times, values = _curve_arrays(curve)
        if values is None:
            continue
        if reference_times is None:
            reference_times = times
        elif not _same_times(reference_times, times):
            raise CleanFBXError(
                "Cannot remove the Armature node: root bone translation keys are not aligned."
            )
        arrays[axis] = values

    if reference_times is None:
        return

    for i in range(len(reference_times)):
        old_t = Vector((
            arrays[0][i] if 0 in arrays else base_t[0],
            arrays[1][i] if 1 in arrays else base_t[1],
            arrays[2][i] if 2 in arrays else base_t[2],
        ))
        new_t = a_translation + a_rotation_matrix @ (a_scale * old_t)
        for axis in arrays:
            arrays[axis][i] = new_t[axis]
    for axis in arrays:
        _curve_default(curves[axis_props[axis]], arrays[axis][0])


def _transform_root_rotation_curves(objects_by_uid, curves_by_acn, acn_by_parent,
                                    uid, base_r, a_rotation_quat):
    acn_uid = acn_by_parent.get(uid, {}).get(b'Lcl Rotation')
    if acn_uid is None:
        return
    curves = _get_channel_curves(objects_by_uid, curves_by_acn, acn_uid)
    if not curves:
        return

    axis_props = (b'd|X', b'd|Y', b'd|Z')
    arrays = {}
    reference_times = None
    for axis, prop in enumerate(axis_props):
        curve = curves.get(prop)
        if curve is None:
            continue
        times, values = _curve_arrays(curve)
        if values is None:
            continue
        if reference_times is None:
            reference_times = times
        elif not _same_times(reference_times, times):
            raise CleanFBXError(
                "Cannot remove the Armature node: root bone rotation keys are not aligned."
            )
        arrays[axis] = values

    if reference_times is None:
        return

    for i in range(len(reference_times)):
        old_r = Euler((
            math.radians(arrays[0][i] if 0 in arrays else base_r[0]),
            math.radians(arrays[1][i] if 1 in arrays else base_r[1]),
            math.radians(arrays[2][i] if 2 in arrays else base_r[2]),
        ), 'XYZ').to_quaternion()
        new_quat = a_rotation_quat @ old_r
        new_euler = new_quat.to_euler('XYZ')
        for axis in arrays:
            arrays[axis][i] = math.degrees(new_euler[axis])
    for axis in arrays:
        _curve_default(curves[axis_props[axis]], arrays[axis][0])


def _transform_root_scale_curves(objects_by_uid, curves_by_acn, acn_by_parent,
                                 uid, base_s, a_scale):
    acn_uid = acn_by_parent.get(uid, {}).get(b'Lcl Scaling')
    if acn_uid is None:
        return
    curves = _get_channel_curves(objects_by_uid, curves_by_acn, acn_uid)
    if not curves:
        return
    for axis, prop in enumerate((b'd|X', b'd|Y', b'd|Z')):
        curve = curves.get(prop)
        if curve is None:
            continue
        _times, values = _curve_arrays(curve)
        if values is None:
            continue
        scale = a_scale[axis]
        for i in range(len(values)):
            values[i] = float(values[i]) * scale
        if len(values):
            _curve_default(curve, values[0])


# ---------------------------------------------------------------------------
# The actual FBX rewrite
# ---------------------------------------------------------------------------

def _clean_fbx_file(filepath, armature_uuids, remove_armature_node=True):
    if not remove_armature_node or not armature_uuids:
        return

    try:
        from io_scene_fbx import parse_fbx, encode_bin, data_types
    except Exception as exc:
        raise CleanFBXError("The Blender FBX exporter is not available: %s" % exc) from exc

    root, version = parse_fbx.parse(filepath)
    objects = _first_child(root, b'Objects')
    connections = _first_child(root, b'Connections')
    if objects is None or connections is None:
        raise CleanFBXError("Exported file does not contain an FBX object tree.")

    objects_by_uid = {}
    for elem in objects.elems:
        if elem.props and isinstance(elem.props[0], int):
            objects_by_uid[elem.props[0]] = elem

    models = {
        elem.props[0]: elem
        for elem in objects.elems
        if elem.id == b'Model' and elem.props and isinstance(elem.props[0], int)
    }

    all_armature_uids = set(int(v) for v in armature_uuids)
    armature_uids_present = {uid for uid in all_armature_uids if uid in models}
    if not armature_uids_present:
        raise CleanFBXError("Could not find the exported Armature object in the FBX file.")

    # Build hierarchy and animation maps before changing anything.
    parent_of = {}
    oo_children = {}
    for conn in connections.elems:
        if conn.id != b'C' or len(conn.props) < 3 or conn.props[0] != b'OO':
            continue
        src, dst = int(conn.props[1]), int(conn.props[2])
        parent_of[src] = dst
        oo_children.setdefault(dst, []).append(src)

    acn_by_parent = {}
    curves_by_acn = {}
    for conn in connections.elems:
        if conn.id != b'C' or len(conn.props) < 4 or conn.props[0] != b'OP':
            continue
        src, dst = int(conn.props[1]), int(conn.props[2])
        src_elem = objects_by_uid.get(src)
        dst_elem = objects_by_uid.get(dst)
        if src_elem is None or dst_elem is None:
            continue
        prop = conn.props[3]
        if src_elem.id == b'AnimationCurveNode' and dst_elem.id == b'Model':
            acn_by_parent.setdefault(dst, {})[prop] = src
        elif src_elem.id == b'AnimationCurve' and dst_elem.id == b'AnimationCurveNode':
            curves_by_acn.setdefault(dst, {})[prop] = src

    removed_object_uids = set()
    bone_to_armature = {}
    armature_assoc = {}

    for arm_uid in sorted(armature_uids_present):
        arm_model = models[arm_uid]
        arm_name = _model_name(arm_model)
        attr_uids = set()
        for child_uid in oo_children.get(arm_uid, ()):
            child_elem = objects_by_uid.get(child_uid)
            if child_elem is not None and child_elem.id == b'NodeAttribute':
                attr_uids.add(child_uid)
        direct_children = [
            uid for uid in oo_children.get(arm_uid, ())
            if uid in models and uid not in attr_uids
        ]

        parent_uid = parent_of.get(arm_uid, 0)
        if parent_uid and parent_uid in models:
            new_parent_uid = parent_uid
            arm_matrix = _model_matrix(arm_model)
            # After removing the Armature node, Blender (and most importers)
            # treat the Armature's parent node as the armature object. Skin
            # clusters therefore need that node's global matrix as their
            # associate model.
            armature_assoc[arm_uid] = _global_model_matrix(parent_uid, models, parent_of)
        else:
            new_parent_uid = 0
            global_matrix = _pose_global_matrix(objects, arm_uid)
            arm_matrix = global_matrix if global_matrix is not None else _model_matrix(arm_model)
            armature_assoc[arm_uid] = Matrix.Identity(4)

        bone_to_armature[arm_uid] = arm_uid
        stack = list(direct_children)
        while stack:
            descendant_uid = stack.pop()
            bone_to_armature[descendant_uid] = arm_uid
            stack.extend(oo_children.get(descendant_uid, ()))

        try:
            a_translation, a_rotation, a_scale = arm_matrix.decompose()
        except Exception as exc:
            raise CleanFBXError("Could not decompose the Armature transform: %s" % exc) from exc

        scale_values = (a_scale.x, a_scale.y, a_scale.z)
        if any(abs(v - 1.0) > 1e-4 for v in scale_values):
            raise CleanFBXError(
                "'%s' has an object scale other than 1.0. Apply the Armature scale "
                "before exporting; this keeps the root bone and skin bind pose clean." % arm_name
            )

        a_rotation_matrix = a_rotation.to_matrix()
        base_rotation = a_rotation

        # Transform the direct children (normally the skeleton's root bones).
        # The Armature node in our exported file only carries axis conversion
        # and object placement (unit conversion is baked into distances), so baking its full
        # matrix into each child keeps the bind pose and skinning intact.
        for child_uid in direct_children:
            child_model = models.get(child_uid)
            if child_model is None:
                continue
            child_trs = _model_trs(child_model)
            base_t = Vector(child_trs.get(b'Lcl Translation', (0.0, 0.0, 0.0)))
            base_r = child_trs.get(b'Lcl Rotation', (0.0, 0.0, 0.0))
            base_s = child_trs.get(b'Lcl Scaling', (1.0, 1.0, 1.0))

            child_matrix = arm_matrix @ _model_matrix(child_model)
            t, r, s = child_matrix.decompose()
            e = r.to_euler('XYZ')
            _set_model_trs(
                child_model,
                translation=t,
                rotation_deg=[math.degrees(v) for v in e],
                scale=s,
            )

            _transform_root_translation_curves(
                objects_by_uid, curves_by_acn, acn_by_parent, child_uid,
                base_t, a_translation, a_rotation_matrix, a_scale,
            )
            _transform_root_rotation_curves(
                objects_by_uid, curves_by_acn, acn_by_parent, child_uid,
                base_r, base_rotation,
            )
            _transform_root_scale_curves(
                objects_by_uid, curves_by_acn, acn_by_parent, child_uid,
                base_s, a_scale,
            )

        # Reparent direct children to the Armature's parent (or world).
        for conn in connections.elems:
            if (conn.id == b'C' and len(conn.props) >= 3 and conn.props[0] == b'OO'
                    and int(conn.props[2]) == arm_uid
                    and int(conn.props[1]) in direct_children):
                conn.props[2] = new_parent_uid

        # Armature object-level animation cannot survive without the node.
        for acn_uid in list(acn_by_parent.get(arm_uid, {}).values()):
            removed_object_uids.add(acn_uid)
            for curve_uid in curves_by_acn.get(acn_uid, {}).values():
                removed_object_uids.add(curve_uid)

        removed_object_uids.add(arm_uid)
        removed_object_uids.update(attr_uids)

    # Remove all connections involving removed nodes/curves.
    kept_connections = []
    for conn in connections.elems:
        if conn.id == b'C' and len(conn.props) >= 3:
            src, dst = int(conn.props[1]), int(conn.props[2])
            if src in removed_object_uids or dst in removed_object_uids:
                continue
        kept_connections.append(conn)
    connections.elems[:] = kept_connections

    # Remove the Armature Model / NodeAttribute and orphaned animation objects.
    objects.elems[:] = [
        elem for elem in objects.elems
        if not (elem.props and isinstance(elem.props[0], int)
                and elem.props[0] in removed_object_uids)
    ]

    _remove_pose_entries(objects, armature_uids_present)

    # Skin clusters still store the removed Armature's bind-space matrix.
    # Replace it with the matrix of the node that now acts as the armature
    # object (normally world identity, or the Armature's parent empty). This
    # keeps the mesh at its original world transform.
    cluster_bone = {}
    for conn in connections.elems:
        if conn.id != b'C' or len(conn.props) < 3 or conn.props[0] != b'OO':
            continue
        src, dst = int(conn.props[1]), int(conn.props[2])
        dst_elem = objects_by_uid.get(dst)
        if (dst_elem is not None and dst_elem.id == b'Deformer'
                and len(dst_elem.props) >= 3 and dst_elem.props[2] == b'Cluster'
                and src in bone_to_armature):
            cluster_bone[dst] = src

    for elem in objects.elems:
        if elem.id != b'Deformer' or len(elem.props) < 3 or elem.props[2] != b'Cluster':
            continue
        associate = _first_child(elem, b'TransformAssociateModel')
        if associate is None or not associate.props:
            continue
        bone_uid = cluster_bone.get(elem.props[0])
        arm_uid = bone_to_armature.get(bone_uid)
        matrix = armature_assoc.get(arm_uid)
        if matrix is not None:
            associate.props[0] = _matrix_to_fbx_values(matrix)

    # Write through a temporary file so a failed cleanup never leaves a broken FBX.
    cleaned_path = filepath + ".cleantmp"
    try:
        converted = _convert_tree(root, encode_bin, data_types)
        encode_bin.write(cleaned_path, converted, version)
        os.replace(cleaned_path, filepath)
    finally:
        if os.path.exists(cleaned_path):
            try:
                os.remove(cleaned_path)
            except OSError:
                pass

    # Sanity check the result: no Armature model or connection should remain.
    try:
        check_root, _check_version = parse_fbx.parse(filepath)
        check_objects = _first_child(check_root, b'Objects')
        if check_objects is not None:
            for elem in check_objects.elems:
                if elem.id == b'Model' and elem.props and elem.props[0] in armature_uids_present:
                    raise CleanFBXError("The Armature node is still present after cleaning.")
    except CleanFBXError:
        raise
    except Exception:
        # A parse failure here is not worth failing an otherwise successful export.
        pass


# ---------------------------------------------------------------------------
# Public export entry
# ---------------------------------------------------------------------------

def _fbx_uuid_for_object(obj):
    from io_scene_fbx.fbx_utils import get_blenderID_key, get_fbx_uuid_from_key
    return int(get_fbx_uuid_from_key(get_blenderID_key(obj)))


def _image_details(image):
    width, height = image.size
    return {
        "name": image.name,
        "resolution": [int(width), int(height)] if width and height else None,
        "source_path": bpy.path.abspath(image.filepath, library=image.library) if image.filepath else None,
        "source": image.source,
        "file_format": image.file_format,
        "channels": image.channels,
        "color_space": image.colorspace_settings.name,
        "packed": bool(image.packed_file),
    }


def _source_textures(objects):
    """Inventory image nodes, including groups, without assuming FBX exports every node."""
    images = {}
    materials = {slot.material for obj in objects for slot in obj.material_slots if slot.material}
    for material in sorted(materials, key=lambda material: material.name):
        visited = set()

        def visit(tree):
            if tree is None or tree in visited:
                return
            visited.add(tree)
            for node in tree.nodes:
                image = getattr(node, "image", None)
                if image is not None:
                    details = images.setdefault(image, {**_image_details(image), "materials": set()})
                    details["materials"].add(material.name)
                if node.type == 'GROUP':
                    visit(node.node_tree)

        if material.use_nodes:
            visit(material.node_tree)
    return [{**details, "materials": sorted(details["materials"])}
            for image, details in sorted(images.items(), key=lambda item: item[0].name)]


def _technical_report(context, filepath, objects, settings, warnings):
    """Read delivered FBX data so modifiers, bone filtering and baking are reflected."""
    from io_scene_fbx import parse_fbx

    root, version = parse_fbx.parse(filepath)
    tree = _first_child(root, b'Objects')
    connections = _first_child(root, b'Connections')
    if tree is None or connections is None:
        raise CleanFBXError("Exported file does not contain an FBX object tree")

    def name(elem):
        return elem.props[1].split(b'\x00\x01', 1)[0].decode('utf-8', errors='replace')

    def array(elem, key):
        child = _first_child(elem, key)
        return child.props[0] if child is not None and child.props else ()

    def properties(elem):
        props = _first_child(elem, b'Properties70')
        return {p.props[0]: p.props[4:] for p in props.elems
                if p.id == b'P' and len(p.props) >= 5} if props else {}

    models = {e.props[0]: e for e in tree.elems if e.id == b'Model'}
    materials = {e.props[0]: e for e in tree.elems if e.id == b'Material'}
    texture_nodes = {e.props[0]: e for e in tree.elems if e.id == b'Texture'}
    videos = {e.props[0]: e for e in tree.elems if e.id == b'Video'}
    parents = {}
    geometry_models = {}
    video_textures = {}
    texture_uses = {}
    for conn in connections.elems:
        if conn.id == b'C' and len(conn.props) >= 3 and conn.props[0] == b'OO':
            child, parent = conn.props[1:3]
            if child in models and (parent in models or parent == 0):
                parents[child] = parent
            if parent in models:
                geometry_models.setdefault(child, []).append(name(models[parent]))
            if child in videos and parent in texture_nodes:
                video_textures.setdefault(child, []).append(parent)
        elif (conn.id == b'C' and len(conn.props) >= 4 and conn.props[0] == b'OP'
              and conn.props[1] in texture_nodes and conn.props[2] in materials):
            texture_uses.setdefault(conn.props[1], []).append({
                "material": name(materials[conn.props[2]]),
                "channel": conn.props[3].decode('utf-8', errors='replace'),
            })

    # FBX stores file paths and connections, but no reliable image dimensions.
    # Match the exporter's image IDs to Blender images for resolution metadata.
    source_images = {_fbx_uuid_for_object(image): image for image in bpy.data.images}
    textures = []
    for uid, video in videos.items():
        image = source_images.get(uid)
        uses = [use for tex_uid in video_textures.get(uid, ())
                for use in texture_uses.get(tex_uid, ())]
        textures.append({
            **(_image_details(image) if image else {"name": name(video), "resolution": None}),
            "file_path": array(video, b'Filename').decode('utf-8', errors='replace'),
            "relative_path": array(video, b'RelativeFilename').decode('utf-8', errors='replace'),
            "embedded": bool(len(array(video, b'Content'))),
            "materials": sorted({use["material"] for use in uses}),
            "uses": sorted(uses, key=lambda use: (use["material"], use["channel"])),
        })
    textures.sort(key=lambda texture: texture["name"])

    meshes = []
    for elem in tree.elems:
        if elem.id != b'Geometry' or elem.props[2] != b'Mesh':
            continue
        indices = array(elem, b'PolygonVertexIndex')
        polygon_sizes = []
        size = 0
        for index in indices:
            size += 1
            if index < 0:
                polygon_sizes.append(size)
                size = 0
        polygons = len(polygon_sizes)
        meshes.append({
            "name": name(elem),
            "objects": sorted(geometry_models.get(elem.props[0], [])),
            "vertices": len(array(elem, b'Vertices')) // 3,
            "edges": len(array(elem, b'Edges')),
            "polygons": polygons,
            "triangles": polygon_sizes.count(3),
            "quads": polygon_sizes.count(4),
            "ngons": sum(size > 4 for size in polygon_sizes),
            "triangles_after_triangulation": len(indices) - 2 * polygons,
            "uv_layers": [array(layer, b'Name').decode('utf-8', errors='replace')
                          for layer in _child_elems(elem, b'LayerElementUV')],
        })

    bones = {uid: elem for uid, elem in models.items() if elem.props[2] == b'LimbNode'}
    skeleton = [{"name": name(elem),
                 "parent_bone": name(bones[parents[uid]]) if parents.get(uid) in bones else None}
                for uid, elem in bones.items()]
    clips = []
    fps = context.scene.render.fps / context.scene.render.fps_base
    # FBX timestamps use 46,186,158,000 ticks per second.
    for elem in tree.elems:
        if elem.id == b'AnimationStack':
            props = properties(elem)
            start = props.get(b'LocalStart', (0,))[0] / 46186158000
            end = props.get(b'LocalStop', (0,))[0] / 46186158000
            clips.append({"name": name(elem), "start_seconds": start,
                          "end_seconds": end, "duration_seconds": end - start})

    source_rigs = []
    for obj in sorted(objects, key=lambda obj: obj.name):
        if obj.type == 'ARMATURE':
            source_rigs.append({
                "name": obj.name,
                "bones": len(obj.data.bones),
                "deform_bones": sum(b.use_deform for b in obj.data.bones),
                "root_bones": [b.name for b in obj.data.bones if b.parent is None],
            })

    option_names = ("export_animations", "export_all_actions", "embed_textures",
                    "deform_bones_only", "add_leaf_bones", "triangulate",
                    "remove_armature_node", "separate_files", "export_technical_details")
    root_bones = sorted(b["name"] for b in skeleton if b["parent_bone"] is None)
    report = {
        "schema_version": 1,
        "report_type": settings.technical_report_format,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generator": {"name": bl_info["name"], "version": ".".join(map(str, bl_info["version"])),
                      "blender_version": bpy.app.version_string},
        "file": {"name": os.path.basename(filepath), "bytes": os.path.getsize(filepath),
                 "fbx_version": version},
        "export_settings": {**{key: getattr(settings, key) for key in option_names},
                            "orientation_preset": settings.orientation_preset,
                            "axis_forward": settings.axis_forward,
                            "axis_up": settings.axis_up,
                            "primary_bone_axis": settings.primary_bone_axis,
                            "secondary_bone_axis": settings.secondary_bone_axis,
                            "technical_report_format": settings.technical_report_format},
        "exported": {
            "stats_basis": "Final FBX after modifiers, export options and skeleton cleanup",
            "mesh_geometry_count": len(meshes),
            "mesh_object_count": sum(e.props[2] == b'Mesh' for e in models.values()),
            "totals": {key: sum(mesh[key] for mesh in meshes) for key in
                       ("vertices", "edges", "polygons", "triangles", "quads", "ngons",
                        "triangles_after_triangulation")},
            "geometry_totals_basis": "Unique FBX geometry blocks; shared instances counted once",
            "meshes": sorted(meshes, key=lambda mesh: mesh["name"]),
            "materials": sorted(name(e) for e in tree.elems if e.id == b'Material'),
            "material_count": len(materials),
            "texture_count": len(textures),
            "textures": textures,
            "texture_metadata_basis": "Images referenced by final FBX Video nodes; resolution and source metadata from Blender images",
            "bone_count": len(bones),
            "has_root_bone": bool(root_bones),
            "has_named_root_bone": any(re.split(r'[:|]', bone)[-1].casefold() == 'root'
                                       for bone in root_bones),
            "root_bones": root_bones,
            "bone_hierarchy": sorted(skeleton, key=lambda bone: bone["name"]),
            "animation_clip_count": len(clips),
            "animation_clips": sorted(clips, key=lambda clip: clip["name"]),
        },
        "source_scene": {"name": context.scene.name,
                         "frame": context.scene.frame_current, "fps": fps,
                         "unit_system": context.scene.unit_settings.system,
                         "unit_scale_length": context.scene.unit_settings.scale_length,
                         "rigs": source_rigs,
                         "textures": _source_textures(objects),
                         "texture_note": "Image nodes in source object materials, including node groups; some shader nodes may not be supported by FBX",
                         "note": "Source scene metadata; exported geometry and bones are listed separately"},
        "warnings": list(warnings),
    }
    if settings.technical_report_format == 'SHORT':
        exported = report["exported"]
        return {
            "schema_version": report["schema_version"],
            "report_type": "SHORT",
            "file": report["file"],
            "model": {
                "name": os.path.splitext(os.path.basename(filepath))[0],
                "objects": sorted(name(e) for e in models.values() if e.props[2] == b'Mesh'),
                "mesh_count": exported["mesh_object_count"],
                "mesh_geometry_count": exported["mesh_geometry_count"],
                **exported["totals"],
                "geometry_totals_basis": exported["geometry_totals_basis"],
                "material_count": exported["material_count"],
                "materials": exported["materials"],
                "texture_count": exported["texture_count"],
                "textures": [{key: texture[key] for key in
                              ("name", "resolution", "file_path", "embedded", "materials")}
                             for texture in textures],
                "texture_metadata_basis": exported["texture_metadata_basis"],
                "bone_count": exported["bone_count"],
                "has_root_bone": exported["has_root_bone"],
                "has_named_root_bone": exported["has_named_root_bone"],
                "root_bones": root_bones,
            },
            "warnings": list(warnings),
        }
    return report


def _write_technical_report(context, filepath, objects, settings, warnings):
    report = _technical_report(context, filepath, objects, settings, warnings)
    report_path = os.path.splitext(filepath)[0] + ".technical.json"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=os.path.dirname(report_path),
                                         suffix='.tmp', delete=False) as handle:
            temporary_path = handle.name
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
    return report_path


def export_clean_rigs(context, settings, base_name_override=None):
    if settings.axis_forward[-1] == settings.axis_up[-1]:
        raise CleanFBXError("Forward and Up must use different axes (for example -Z and Y).")
    groups = _export_groups(context)
    rigs = {obj for _active, objects in groups for obj in objects if obj.type == 'ARMATURE'}
    if rigs and settings.primary_bone_axis[-1] == settings.secondary_bone_axis[-1]:
        raise CleanFBXError("Primary and Secondary Bone Axis must use different axes.")
    if not groups:
        raise CleanFBXError("Select a mesh, rig, curve, text object or empty to export first.")

    if settings.remove_armature_node:
        for arm in rigs:
            scale = arm.scale
            if any(abs(v - 1.0) > 1e-4 for v in scale):
                raise CleanFBXError(
                    "'%s' has an object scale other than 1.0. Apply the Armature scale "
                    "before exporting." % arm.name
                )

    output_dir = _resolve_output_dir(settings)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise CleanFBXError("Could not create the output folder: %s" % exc) from exc

    prefix = _safe_filename(settings.filename_prefix) if settings.filename_prefix.strip() else ""
    warnings = []
    exported = []
    if settings.separate_files:
        jobs = [(active, objects, prefix + _safe_filename(active.name))
                for active, objects in groups]
    else:
        union = set().union(*(objects for _active, objects in groups))
        if base_name_override:
            base_name = base_name_override
        elif len(groups) == 1:
            base_name = groups[0][0].name
        else:
            base_name = context.scene.name or "models"
        jobs = [(groups[0][0], union, prefix + _safe_filename(base_name))]

    used_names = set()
    for active_object, objects, name in jobs:
        base_name = name
        suffix = 1
        while name.casefold() in used_names:
            name = "%s.%03d" % (base_name, suffix)
            suffix += 1
        used_names.add(name.casefold())
        job_warnings = []
        filepath = os.path.join(output_dir, name + ".fbx")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError as exc:
                raise CleanFBXError("Could not overwrite '%s': %s" % (filepath, exc)) from exc

        if settings.export_animations and settings.remove_armature_node:
            for arm in [o for o in objects if o.type == 'ARMATURE']:
                if _has_object_level_animation(arm):
                    job_warnings.append(
                        "Armature object transform animation on '%s' is not preserved "
                        "when the Armature node is removed." % arm.name
                    )

        armatures = [o for o in objects if o.type == 'ARMATURE']
        uuids = {_fbx_uuid_for_object(arm) for arm in armatures}

        _export_objects_fbx(filepath, objects, active_object, settings)

        if settings.remove_armature_node and uuids:
            try:
                _clean_fbx_file(filepath, uuids, remove_armature_node=True)
            except Exception as exc:
                raise CleanFBXError(
                    "Exported '%s', but could not remove the Armature node: %s"
                    % (os.path.basename(filepath), exc)
                ) from exc

        warnings.extend(job_warnings)
        exported.append(filepath)
        if settings.export_technical_details:
            try:
                _write_technical_report(context, filepath, objects, settings, job_warnings)
            except Exception as exc:
                warnings.append("FBX saved, but technical report for '%s' failed: %s"
                                % (os.path.basename(filepath), exc))

    settings.last_export_dir = output_dir
    settings.last_export_status = "Exported: " + ", ".join(os.path.basename(p) for p in exported)
    return exported, warnings


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CLEANFBX_OT_export(bpy.types.Operator):
    bl_idname = "clean_fbx.export"
    bl_label = "Export Clean FBX"
    bl_description = "Export selected models and rigs, removing Armature object nodes when enabled"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.clean_fbx
        try:
            exported, warnings = export_clean_rigs(context, settings)
        except CleanFBXError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, "Clean FBX export failed: %s" % exc)
            return {'CANCELLED'}

        for warning in warnings:
            self.report({'WARNING'}, warning)
        self.report({'INFO'}, "Clean FBX exported: %s" % ", ".join(os.path.basename(p) for p in exported))
        return {'FINISHED'}


class CLEANFBX_OT_open_folder(bpy.types.Operator):
    bl_idname = "clean_fbx.open_folder"
    bl_label = "Open Output Folder"
    bl_description = "Open the clean FBX output folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.clean_fbx
        try:
            path = settings.last_export_dir or _resolve_output_dir(settings)
            os.makedirs(path, exist_ok=True)
            bpy.ops.wm.path_open(filepath=path)
        except Exception as exc:
            self.report({'ERROR'}, "Could not open folder: %s" % exc)
            return {'CANCELLED'}
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_clean_fbx_export(bpy.types.Panel):
    bl_label = "Clean FBX Export " + ".".join(map(str, bl_info["version"]))
    bl_idname = "VIEW3D_PT_clean_fbx_export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clean FBX"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        settings = context.scene.clean_fbx

        groups = _export_groups(context)
        if len(groups) == 1:
            active = groups[0][0]
            layout.label(text=active.name,
                         icon='ARMATURE_DATA' if active.type == 'ARMATURE' else 'OBJECT_DATA')
        elif len(groups) > 1:
            layout.label(text="%d models selected" % len(groups), icon='OBJECT_DATA')
        else:
            layout.label(text="Select a model or rig", icon='INFO')

        col = layout.column(align=True)
        col.label(text="Output")
        col.prop(settings, "output_dir", text="")

        col = layout.column(align=True)
        col.prop(settings, "export_animations", text="Include animation")
        col.prop(settings, "export_technical_details", text="Technical report (JSON)")
        if settings.export_technical_details:
            col.row(align=True).prop(settings, "technical_report_format", expand=True)
        if len(groups) > 1:
            col.prop(settings, "separate_files")

        layout.separator(factor=0.5)
        row = layout.row(align=True)
        row.scale_y = 1.3
        export = row.row(align=True)
        export.enabled = bool(groups)
        export.operator("clean_fbx.export", icon='EXPORT', text="Export FBX")
        row.operator("clean_fbx.open_folder", icon='FILE_FOLDER', text="")

        if settings.last_export_status:
            layout.label(text="Export complete", icon='CHECKMARK')


class VIEW3D_PT_clean_fbx_orientation(bpy.types.Panel):
    bl_label = "Orientation"
    bl_idname = "VIEW3D_PT_clean_fbx_orientation"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clean FBX"
    bl_parent_id = "VIEW3D_PT_clean_fbx_export"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.clean_fbx
        layout.prop(settings, "orientation_preset")
        col = layout.column(align=True)
        col.prop(settings, "axis_forward")
        col.prop(settings, "axis_up")
        if settings.orientation_preset == 'UNREAL':
            layout.label(text="Unreal FBX import:", icon='INFO')
            layout.label(text="Enable Force Front XAxis")
        if settings.axis_forward[-1] == settings.axis_up[-1]:
            layout.label(text="Forward and Up must differ", icon='ERROR')
        layout.separator(factor=0.5)
        layout.label(text="Bone axes")
        col = layout.column(align=True)
        col.prop(settings, "primary_bone_axis", text="Primary")
        col.prop(settings, "secondary_bone_axis", text="Secondary")
        if settings.primary_bone_axis[-1] == settings.secondary_bone_axis[-1]:
            layout.label(text="Bone axes must differ", icon='ERROR')
        layout.separator(factor=0.5)
        layout.label(text="FBX 7.4 Binary", icon='FILE')


class VIEW3D_PT_clean_fbx_advanced(bpy.types.Panel):
    bl_label = "Advanced"
    bl_idname = "VIEW3D_PT_clean_fbx_advanced"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Clean FBX"
    bl_parent_id = "VIEW3D_PT_clean_fbx_export"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        settings = context.scene.clean_fbx
        layout.prop(settings, "filename_prefix")

        col = layout.column(align=True)
        col.prop(settings, "embed_textures")
        col.prop(settings, "triangulate")

        layout.separator(factor=0.5)
        col = layout.column(align=True)
        col.prop(settings, "remove_armature_node")
        col.prop(settings, "deform_bones_only")
        col.prop(settings, "add_leaf_bones")

        row = layout.row()
        row.enabled = settings.export_animations
        row.prop(settings, "export_all_actions")


def _menu_func_file_export(self, context):
    self.layout.operator("clean_fbx.export", text="Clean FBX (.fbx)")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    CleanFBXSettings,
    CLEANFBX_OT_export,
    CLEANFBX_OT_open_folder,
    VIEW3D_PT_clean_fbx_export,
    VIEW3D_PT_clean_fbx_orientation,
    VIEW3D_PT_clean_fbx_advanced,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.clean_fbx = PointerProperty(type=CleanFBXSettings)
    bpy.types.TOPBAR_MT_file_export.append(_menu_func_file_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_func_file_export)
    del bpy.types.Scene.clean_fbx
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
