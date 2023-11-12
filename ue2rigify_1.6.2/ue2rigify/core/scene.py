# Copyright Epic Games, Inc. All Rights Reserved.

import os
import re
import sys
import bpy
import importlib

from ..constants import Modes, Collections, Rigify, Template
from . import nodes, utilities, templates, validations
from ..ui import view_3d, node_editor
from ..settings.tool_tips import *
from ..settings.viewport_settings import *


def get_modes(self=None, context=None):
    """
    Gets the enumeration for mode selection.

    :param object self: This is a reference to the class this functions in appended to.
    :param object context: The context of the object this function is appended to.
    :return list: A list of tuples that define the modes enumeration.
    """
    properties = bpy.context.scene.ue2rigify

    # the modes only contain source mode initially
    modes = [
        (Modes.SOURCE.name, Modes.SOURCE.value, source_mode_tool_tip, 'USER', 0),
        (Modes.METARIG.name, Modes.METARIG.value, metarig_mode_tool_tip, 'CON_ARMATURE', 1),
        (Modes.FK_TO_SOURCE.name, Modes.FK_TO_SOURCE.value, fk_to_source_mode_tool_tip, 'BACK', 2),
        (Modes.SOURCE_TO_DEFORM.name, Modes.SOURCE_TO_DEFORM.value, source_to_deform_mode_tool_tip, 'FORWARD', 3),
        (Modes.CONTROL.name, Modes.CONTROL.value, control_mode_tool_tip, 'ARMATURE_DATA', 4)
    ]

    # if there is a not a saved metarig file remove the last 3 modes
    if not os.path.exists(properties.saved_metarig_data):
        modes = modes[:-3]

    return modes


def get_keyframes(rig_object, fcurve, socket_name, data_path, keyed_values):
    """
    Gets the bones keyed frames and data paths.

    :param object rig_object: A blender object that contains armature data.
    :param object fcurve: A fcurve.
    :param str socket_name: The name of the linked node socket.
    :param str data_path: The name of the bone data that is keyed.
    :param list keyed_values: A list of keyed values.
    :return list: A list of keyed values.
    """
    # get the pose bone
    bone = rig_object.pose.bones.get(socket_name)

    # only save the rotation key frames if they match the current rotation mode
    if bone:
        if 'rotation_euler' == data_path:
            if len(bone.rotation_mode) != 3:
                return keyed_values

        if 'rotation_quaternion' == data_path:
            if bone.rotation_mode.lower() not in data_path:
                return keyed_values

    for keyframe_point in fcurve.keyframe_points:
        # get the original key frame value
        original_key_frame = keyframe_point.co[0]

        # round the original key frame value to the nearest frame
        rounded_key_frame = round(original_key_frame)

        # if this data path and frame pair are unique add them to the keyed values
        if (data_path, rounded_key_frame) not in keyed_values:
            keyed_values.append((data_path, rounded_key_frame))

    return keyed_values


def get_keyframes_by_socket_links(rig_object, fcurve, socket_name, data_path, socket_direction, links_data, keyed_values):
    """
    Gets the keyed bone data from the corresponding bone that is linked by nodes.

    :param object rig_object: A blender object that contains armature data.
    :param object fcurve: A curve.
    :param str socket_name: The name of the linked node socket.
    :param str data_path: The name of the bone data that is keyed.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    :param list links_data: A list of dictionaries that contains link attributes.
    :param list keyed_values: A list of keyed values.
    :return list: A list of keyed values.
    """

    # go through all the keyed bones
    for link in links_data:
        # select only the corresponding bones who's sockets are linked in the nodes
        if link[socket_direction] == socket_name:
            keyed_values = get_keyframes(
                    rig_object,
                    fcurve,
                    socket_name,
                    data_path,
                    keyed_values,
            )

    return keyed_values


def get_keyframe_data(rig_object, socket_direction=None, links_data=None):
    """
    This function get all the actions with there start and end frames and the keyed frames with their data paths from
    the provided rig object, a list of links, and a socket direction.

    :param object rig_object: A blender object that contains armature data.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    :param list links_data: A list of dictionaries that contains link attributes.
    :return:
    """
    animation_data = {}

    if rig_object.animation_data:

        # go through the nla tracks in the objects animation data
        for nla_track in rig_object.animation_data.nla_tracks:
            # go through the nla tracks strips
            for strip in nla_track.strips:
                if strip.action:
                    # initialize the list of keyed values, that will hold pairs of data paths and frame positions
                    animation_data[strip.action.name] = {}
                    animation_data[strip.action.name]['data'] = {}
                    animation_data[strip.action.name]['action_frame_start'] = strip.action_frame_start
                    animation_data[strip.action.name]['action_frame_end'] = strip.action_frame_end
                    animation_data[strip.action.name]['strip_frame_start'] = strip.frame_start
                    animation_data[strip.action.name]['strip_frame_end'] = strip.frame_end
                    animation_data[strip.action.name]['mute'] = nla_track.mute
                    animation_data[strip.action.name]['is_solo'] = nla_track.is_solo
                    keyed_values = []

                    # go through the strips fcurves
                    for fcurve in strip.action.fcurves:
                        socket_name = 'object'
                        data_path = fcurve.data_path

                        # if the fcurve data_path has a key parse out the bone name
                        if '["' in fcurve.data_path:
                            # parse out the bone name and data path
                            socket_name = fcurve.data_path.split('"')[1]
                            data_path = fcurve.data_path.split('.')[-1]

                        if links_data:
                            keyed_values = get_keyframes_by_socket_links(
                                rig_object,
                                fcurve,
                                socket_name,
                                data_path,
                                socket_direction,
                                links_data,
                                keyed_values
                            )

                        if keyed_values:
                            # store the bone name and all its keyed values in the animation data dictionary
                            animation_data[strip.action.name]['data'][socket_name] = keyed_values

    return animation_data


def get_to_rig_action(from_rig_action, from_rig_object, to_rig_object, bake_to_source, properties):
    """
    This function gets the related action from the from rig and creates a matching action with a unique
    name for the to rig and returns it.

    :param from_rig_action:
    :param object from_rig_object: The rig that is driving another rig by using constraints.
    :param object to_rig_object: The rig that is being driven by constraints.
    :param bool bake_to_source: This flag states whether or not the action is being baked to the source rig.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    :return object: The to rig action.
    """
    # when baking back to the source rig
    if bake_to_source:
        to_rig_action_name = f'{Modes.SOURCE.name}_{from_rig_action.name}'
    else:
        to_rig_action_name = from_rig_action.name.replace(f'{Modes.SOURCE.name}_', '')
        from_rig_action.name = f'{Modes.SOURCE.name}_{to_rig_action_name}'

    # get the to rig action
    to_rig_action = bpy.data.actions.get(to_rig_action_name)

    # if it already exists and the bake is to not to the source rig and overwrite animations is false
    # return and don't bake the animation.
    if to_rig_action and not bake_to_source and not properties.overwrite_control_animations:
        return None

    # if there is not an existing animation create a new one
    if not to_rig_action:
        to_rig_action = bpy.data.actions.new(to_rig_action_name)

    # if the to rig doesn't have animation data create it
    if not to_rig_object.animation_data:
        to_rig_object.animation_data_create()

    # set the to_rig action to the current action on the to_rig
    to_rig_object.animation_data.action = to_rig_action

    # set the from_rig_action to the current action on the from_rig
    from_rig_object.animation_data.action = from_rig_action

    return to_rig_action


def get_from_rig_animation_data(from_rig_object, to_rig_object, links_data, properties):
    """
    This function gets the animation data from the from rig objects related bones.

    :param object from_rig_object: The rig that is driving another rig by using constraints.
    :param object to_rig_object: The rig that is being driven by constraints.
    :param list links_data: A list of dictionaries that contains link attributes.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    :return dict: A dictionary of the animation data from the from rig objects related bones
    """
    from_rig_animation_data = {}

    # if there are links
    if from_rig_object.animation_data:

        # stash all animations on the from_rig
        utilities.stash_animation_data(from_rig_object)

        # switch to object mode
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # deselect everything and then select the to_rig
        bpy.ops.object.select_all(action='DESELECT')
        to_rig_object.hide_set(False)
        to_rig_object.select_set(True)
        from_rig_object.hide_set(True)

        # make the to_rig the active object
        bpy.context.view_layer.objects.active = to_rig_object

        # switch the to_rig to pose mode
        bpy.ops.object.mode_set(mode='POSE')

        # get the animation data from the from_rig
        from_rig_animation_data = get_keyframe_data(
            from_rig_object,
            socket_direction='from_socket',
            links_data=links_data
        )

    return from_rig_animation_data


def set_meta_rig(self=None, context=None):
    """
    Sets the enumeration for mode selection.

    :param object self: This is a reference to the property this functions in appended to.
    :param object context: The context of the object this function is appended to.
    """
    # create from the default rigify operators
    if self.selected_starter_metarig_template.startswith('bpy.ops'):
        create_starter_metarig_template(self)
    # or create from an existing rig
    else:
        remove_metarig()
        create_meta_rig(self, file_path=os.path.join(
            Template.RIG_TEMPLATES_PATH,
            self.selected_starter_metarig_template,
            f'{Rigify.META_RIG_NAME}.py'
        ))


def set_fk_ik_switch_values(rig_object, value):
    """
    Sets the rigify IK values to FK, and set the IK solver properties.

    :param object rig_object: A blender object that contains armature data.
    :param float value: The floating point value of the FK to IK slider.
    """
    for bone in rig_object.pose.bones:
        if isinstance(bone.get('IK_FK'), float):
            bone['IK_FK'] = value


def set_action_to_nla_strip(to_rig_action, to_rig_object, from_rig_action_data, properties):
    """
    Sets the to rig's action in a nla strip to the from rig's action.

    :param object to_rig_action: The action on the rig that is driving another rig by using constraints.
    :param object to_rig_object: The rig that is being driven by constraints.
    :param dict from_rig_action_data: A dictionary of animation data from the from rig.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    to_rig_nla_track_name = None
    to_rig_strip_name = None

    # look for an existing nla track with this action already assigned
    for nla_track in to_rig_object.animation_data.nla_tracks:
        for strip in nla_track.strips:
            if strip.action:
                if strip.action.name == to_rig_action.name:
                    to_rig_nla_track_name = nla_track.name
                    to_rig_strip_name = strip.name
                    to_rig_object.animation_data.nla_tracks.remove(nla_track)

    # set the nla track and strip names if they are not defined yet to the action name without the source prefix
    if not to_rig_nla_track_name:
        to_rig_nla_track_name = to_rig_action.name

    if not to_rig_strip_name:
        to_rig_strip_name = to_rig_action.name

    # create the nla tracks and strips for the new actions
    to_rig_nla_track = to_rig_object.animation_data.nla_tracks.new()
    to_rig_nla_track.name = to_rig_nla_track_name.replace(f'{Modes.SOURCE.name}_', '')
    to_rig_nla_track.mute = from_rig_action_data['mute']
    if from_rig_action_data['is_solo']:
        to_rig_nla_track.is_solo = from_rig_action_data['is_solo']

    # create a new nla strip and set the strip start and end values
    to_rig_strip_name = to_rig_strip_name.replace(f'{Modes.SOURCE.name}_', '')
    to_rig_strip = to_rig_nla_track.strips.new(to_rig_strip_name, 0, to_rig_action)
    to_rig_strip.frame_start = from_rig_action_data['strip_frame_start']
    to_rig_strip.frame_end = from_rig_action_data['strip_frame_end']


def remove_missing_bone_socket(node_name, socket_name, properties):
    """
    Removes a missing bone socket from a node provide the socket and node name.

    :param str node_name: The name of the node to remove the socket from.
    :param str socket_name: The name of the socket to remove.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # get the saved node and link data
    node_data = templates.get_saved_node_data(properties)
    links_data = templates.get_saved_links_data(properties)

    # remove the socket and link from the saved data
    edited_node_data = nodes.remove_node_socket_from_node_data(node_data, node_name, socket_name)
    edited_links_data = nodes.remove_link_from_link_data(links_data, node_name, socket_name)

    # save the edited node and link data
    templates.save_json_file(edited_node_data, properties.saved_node_data)
    templates.save_json_file(edited_links_data, properties.saved_links_data)

    # default the mode to source mode
    mode = Modes.SOURCE.name

    # if there is saved node data get the mode
    if node_data:
        mode = node_data[0]['mode']

    # change the tool to the appropriate mode
    properties.selected_mode = mode


def remove_metarig():
    """
    Removes the metarig and its data.
    """
    # remove the metarig object
    metarig_object = bpy.data.objects.get(Rigify.META_RIG_NAME)
    if metarig_object:
        bpy.data.objects.remove(metarig_object)

    # remove the metarig object duplicates
    for scene_object in bpy.data.objects:
        if scene_object.name.startswith(f'{Rigify.META_RIG_NAME}.'):
            bpy.data.objects.remove(scene_object)


def remove_object_constraint(scene_object):
    """
    Removes a constraint provided a object.

    :param object scene_object: A scene object.
    """
    for constraint in scene_object.constraints:
        if constraint.name.startswith(Collections.CONSTRAINTS_COLLECTION_NAME):
            if constraint:
                # remove the parent and child bone constraints
                if constraint.target:
                    if constraint.target.parent:
                        bpy.data.objects.remove(constraint.target.parent)
                    if constraint.target:
                        bpy.data.objects.remove(constraint.target)

                # remove constraint from bone
                scene_object.constraints.remove(constraint)


def remove_bone_constraint(bone):
    """
    This function removes a constraint provided a bone object.

    :param object bone: A pose bone object.
    """
    constraint = bone.constraints.get(Collections.CONSTRAINTS_COLLECTION_NAME)
    if constraint:
        # remove the parent and child bone constraints
        if constraint.target:
            if constraint.target.parent:
                bpy.data.objects.remove(constraint.target.parent)
            if constraint.target:
                bpy.data.objects.remove(constraint.target)

        # remove constraint from bone
        bone.constraints.remove(constraint)


def remove_constraints(rig_object, properties, socket_direction='', links_data=None):
    """
    Removes all constraints from a rig object.

    :param object rig_object: A blender object that contains armature data.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    if rig_object:
        if rig_object.type == 'ARMATURE':
            # if there is links data, remove the associated bone constraints
            if links_data and socket_direction:
                for link in links_data:
                    socket_name = link.get(socket_direction)

                    # removes the constraint if it is on a bone
                    bone = rig_object.pose.bones.get(socket_name)
                    if bone:
                        remove_bone_constraint(bone)

            # if no links data is provided
            else:
                # remove all constraints on all bones of the rig object
                for bone in rig_object.pose.bones:
                    remove_bone_constraint(bone)

            # remove object constraints on the source_rig object
            if properties.source_rig:
                remove_object_constraint(properties.source_rig)

    constraints_collection = bpy.data.collections.get(Collections.CONSTRAINTS_COLLECTION_NAME)
    if constraints_collection:
        # remove the objects in the constraints collection
        for empty_object in constraints_collection.objects:
            bpy.data.objects.remove(empty_object)

        # remove the collection
        bpy.data.collections.remove(constraints_collection)


def remove_location_key_frames(rig_object, excluded_fcurves):
    """
    This function removes all location keyframes from the bones of the provided object except from the provided
    root bone.

    :param list excluded_fcurves: A list of fcurve names to exclude.
    :param object rig_object: An object of type armature.
    """
    if rig_object:
        if rig_object.animation_data:
            utilities.stash_animation_data(rig_object)

            for nla_track in rig_object.animation_data.nla_tracks:
                for strip in nla_track.strips:
                    if strip.action:
                        for fcurve in strip.action.fcurves:
                            # does not remove fcurves of the objects transforms
                            if fcurve.data_path in ['location', 'rotation_euler', 'rotation_quaternion', 'scale']:
                                continue

                            for excluded_fcurve in excluded_fcurves:
                                if excluded_fcurve not in fcurve.data_path:
                                    if fcurve.data_path[-8:] == 'location':
                                        strip.action.fcurves.remove(fcurve)

            utilities.operator_on_object_in_mode(
                lambda: utilities.clear_pose_location(),
                rig_object,
                'POSE'
            )


def remove_extra_keyframes(to_rig, from_rig_animation_data, to_rig_animation_data, links_data):
    """
    This function removes the extra keyframes from the to_rig_animation_data that don't exist in the
    from_rig_animation_data.

    :param object to_rig: A blender object that contains armature data.
    :param dict from_rig_animation_data: A dictionary of information that contains the animation data of the from_rig.
    :param dict to_rig_animation_data: A dictionary of information that contains the animation data of the to_rig.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    for from_bone_name in from_rig_animation_data.keys():
        for link in links_data:
            if link['from_socket'] == from_bone_name:
                to_bone = to_rig.pose.bones.get(link['to_socket'])
                for keyed_value in to_rig_animation_data[to_bone.name]:
                    if from_rig_animation_data[from_bone_name].count(keyed_value) == 0:
                        to_bone.keyframe_delete(keyed_value[0], index=-1, frame=keyed_value[1])


def remove_rig_object_actions(rig_object, action_name):
    """
    This function removes the provided action by its name from the provided rig object.

    :param object rig_object: A blender object that contains armature data.
    :param str action_name: The name of the action to remove.
    """
    if rig_object.animation_data:
        for nla_track in rig_object.animation_data.nla_tracks:
            # remove the strip that matches the given action name
            for strip in nla_track.strips:
                if strip.action.name == action_name:
                    nla_track.strips.remove(strip)
            # if the nla strip is empty remove it
            if not nla_track.strips:
                rig_object.animation_data.nla_tracks.remove(nla_track)

    # remove the action from the action data
    action = bpy.data.actions.get(action_name)
    if action:
        bpy.data.actions.remove(action)


def create_constraints_collection(properties):
    """
    Creates the scene collection for the constraints and empties.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # create a new collection
    constraints_collection = bpy.data.collections.new(Collections.CONSTRAINTS_COLLECTION_NAME)

    # get the extras collection
    extras_collection = bpy.data.collections.get(Collections.EXTRAS_COLLECTION_NAME)
    if not extras_collection:
        extras_collection = bpy.data.collections.new(Collections.EXTRAS_COLLECTION_NAME)
        bpy.context.scene.collection.children.link(extras_collection)

    # link the new collection to the extras collection
    extras_collection.children.link(constraints_collection)

    return constraints_collection


def create_empty(bone_name, empty_prefix, constraints_collection):
    """
    This function creates an empty object.

    :param str bone_name: A bone name to add to end of the empty name.
    :param str empty_prefix: A prefix for the empty name.
    :param object constraints_collection: The constraints collection.
    """

    # create an empty
    empty_name = f'{empty_prefix}_{bone_name}'
    empty = bpy.data.objects.new(empty_name, None)

    # add empty to the constraints collection
    constraints_collection.objects.link(empty)

    return empty


def create_empties(rig_object, empty_prefix, constraints_collection, links_data, socket_direction):
    """
    Creates empties based on an armatures pose bones.

    :param object rig_object: A blender object that contains armature data.
    :param str empty_prefix: A prefix for the empty name.
    :param object constraints_collection: The constraints collection.
    :param list links_data: A list of dictionaries that contains link attributes.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    """
    properties = bpy.context.scene.ue2rigify

    if rig_object:
        for link in links_data:
            socket_name = link.get(socket_direction)

            # create the empty
            empty = create_empty(socket_name, empty_prefix, constraints_collection)

            if socket_name == 'object':
                # get the source rig
                if properties.source_rig:
                    # move the empty to the source rig world matrix
                    empty.matrix_world = properties.source_rig.matrix_world

            else:
                # get edit bone and pose bone
                data_bone = rig_object.data.bones.get(socket_name)
                pose_bone = rig_object.pose.bones.get(socket_name)

                # if the bone exists on the rig
                if pose_bone:
                    # calculate the world bone matrix
                    pose_bone_object = pose_bone.id_data
                    bone_matrix_world = pose_bone_object.matrix_world @ data_bone.matrix_local

                    # move the empty to the bone world matrix
                    empty.matrix_world = bone_matrix_world


def create_source_rig_object_constraint(empty_object, socket_direction, properties):
    """
    Creates a constraint for the source rig object.

    :param object empty_object: A object of type empty.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    if empty_object:
        if socket_direction == 'to_socket':
            object_constraint = empty_object.constraints.new('COPY_TRANSFORMS')
            object_constraint.name = Collections.CONSTRAINTS_COLLECTION_NAME
            object_constraint.target = properties.source_rig

        else:
            object_constraint = properties.source_rig.constraints.new('COPY_TRANSFORMS')
            object_constraint.name = Collections.CONSTRAINTS_COLLECTION_NAME
            object_constraint.target = empty_object


def create_object_constraint(rig_object, empty_object, bone_name, properties):
    """
    Creates a object constraint to a the provided bone name.

    :param rig_object: A object of type armature.
    :param str bone_name: The name of the bone to constrain the object to.
    :param object empty_object: A object of type empty.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    if empty_object:
        object_constraint = empty_object.constraints.new('COPY_TRANSFORMS')
        object_constraint.name = Collections.CONSTRAINTS_COLLECTION_NAME
        object_constraint.target = rig_object
        object_constraint.subtarget = bone_name


def create_bone_constraint(bone, empty_object, properties):
    """
    Creates a bone constraint that constrains to the provided bone.

    :param object bone: A bone object.
    :param object empty_object: A object of type empty.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    if bone:
        bone_constraint = bone.constraints.new('COPY_TRANSFORMS')
        bone_constraint.name = Collections.CONSTRAINTS_COLLECTION_NAME
        bone_constraint.target = empty_object


def create_constraints(rig_object, links_data, empty_prefix, socket_direction, properties):
    """
    Creates the constraints used to bind the bone to the empty objects.

    :param object rig_object: A blender object that contains armature data.
    :param list links_data: A list of dictionaries that contains link attributes.
    :param str empty_prefix: A prefix for the empty name.
    :param str socket_direction: A socket direction either 'from_socket' or 'to_socket'.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    for link in links_data:
        socket_name = link.get(socket_direction)
        empty_name = f'{empty_prefix}_{socket_name}'
        empty_object = bpy.data.objects.get(empty_name)

        if socket_name == 'object':
            create_source_rig_object_constraint(empty_object, socket_direction, properties)

        elif empty_prefix == 'child' and socket_name != 'object':
            bone = rig_object.pose.bones.get(socket_name)
            create_bone_constraint(bone, empty_object, properties)
        else:
            create_object_constraint(rig_object, empty_object, socket_name, properties)


def create_starter_metarig_template(properties):
    """
    Creates the rigify starter meta rig from the selected starter template.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # remove the existing metarig if needed
    remove_metarig()

    # if meta rig already exists remove it.
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    # create the selected metarig
    operator = properties.selected_starter_metarig_template
    exec(operator)

    # get the meta rig object
    meta_rig_object = bpy.data.objects.get(Rigify.META_RIG_NAME)

    # scale the meta rig and apply its scale
    meta_rig_object.scale = meta_rig_object.scale * (1/bpy.context.scene.unit_settings.scale_length)
    utilities.operator_on_object_in_mode(
        lambda: bpy.ops.object.transform_apply(location=False, rotation=False, scale=True),
        meta_rig_object,
        'OBJECT'
    )

    # set the object back to edit mode
    bpy.ops.object.mode_set(mode='EDIT')

    return meta_rig_object


def create_meta_rig(properties, file_path=None):
    """
    Instantiates the rigify meta rig from the selected template.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    :param str file_path: The file path to the metarig file.
    """
    if not file_path:
        file_path = properties.saved_metarig_data

    # create the metarig armature
    metarig_armature = bpy.data.armatures.new(name=Rigify.META_RIG_NAME)

    # create the metarig object
    metarig_object = bpy.data.objects.new(name=Rigify.META_RIG_NAME, object_data=metarig_armature)

    bpy.context.view_layer.active_layer_collection.collection.objects.link(metarig_object)

    # select the metarig and make it active
    metarig_object.select_set(True)
    bpy.context.view_layer.objects.active = metarig_object

    # get the path to the saved metarig file
    rig_template_path = os.path.dirname(file_path)

    # insert the path to the location of the metarig module
    if rig_template_path not in sys.path:
        sys.path.insert(0, rig_template_path)

    # load the module
    metarig = importlib.import_module(Rigify.META_RIG_NAME)
    importlib.reload(metarig)

    # remove to prevent root module naming conflicts
    sys.path.remove(rig_template_path)

    # call the create function to add the saved metarig
    metarig.create(metarig_object)

    metarig_object.scale = metarig_object.scale * (1 / bpy.context.scene.unit_settings.scale_length)
    utilities.operator_on_object_in_mode(
        lambda: bpy.ops.object.transform_apply(location=False, rotation=False, scale=True),
        metarig_object,
        'OBJECT'
    )

    return metarig_object


def create_control_rig(properties):
    """
    Creates the rigify control rig.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # if the source rig is the mannequin, then fix it
    if properties.selected_rig_template in [Template.DEFAULT_MALE_TEMPLATE, Template.DEFAULT_FEMALE_TEMPLATE]:
        remove_location_key_frames(properties.source_rig, ['pelvis'])

    # create the meta rig
    create_meta_rig(properties)

    # try to generate the rigify rig
    try:
        bpy.ops.pose.rigify_generate()

    except Exception as error:
        properties.selected_mode = Modes.SOURCE.name
        utilities.report_rigify_error(str(error))

    finally:
        # remove the metarig
        remove_metarig()

        # get the control rig
        control_rig_object = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)

        return control_rig_object


def move_scene_object_to_collection(scene_object, collection):
    """
    Moves a scene object to the provided collection.

    :param object scene_object: A blender object.
    :param object collection: A blender collection.
    """
    if scene_object and collection:
        # remove the object from all collections
        for scene_collection in bpy.data.collections.values() + [bpy.context.scene.collection]:
            if scene_object in scene_collection.objects.values():
                scene_collection.objects.unlink(scene_object)

        # link the object to the given collection
        if scene_object not in collection.objects.values():
            collection.objects.link(scene_object)


def move_collection_to_collection(collection, to_collection):
    """
    Moves a scene object to the provided collection.

    :param object collection: A collection.
    :param object to_collection: A collection to move the provided collection to.
    """
    if collection and to_collection:
        # remove the collection from all collections
        for scene_collection in bpy.data.collections.values() + [bpy.context.scene.collection]:
            if collection in scene_collection.children.values():
                scene_collection.children.unlink(collection)

        # link the collection to the given collection
        if collection not in to_collection.children.values():
            to_collection.children.link(collection)


def select_related_keyed_bones(to_rig_object, from_rig_action_data, links_data):
    """
    Selects all the bones that need to keyed in the action bake.

    :param object to_rig_object: The rig that is being driven by constraints.
    :param dict from_rig_action_data: A dictionary of animation data from the from rig.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    # deselect everything
    if bpy.context.mode == 'POSE':
        bpy.ops.pose.select_all(action='DESELECT')
    if bpy.context.mode == 'OBJECT':
        bpy.ops.object.select_all(action='DESELECT')

    # go through all the keyed bones
    for from_bone_name in from_rig_action_data['data'].keys():
        for link in links_data:
            # select only the corresponding bones who's sockets are linked in the nodes
            if link['from_socket'] == from_bone_name:
                if bpy.context.mode == 'POSE':
                    to_bone = to_rig_object.data.bones.get(link['to_socket'])
                    if to_bone:
                        to_bone.select = True

            # select the source rig if there is a socket linked to it and its link has keyframes
            if link['to_socket'] == 'object':
                if bpy.context.mode == 'OBJECT':
                    properties = bpy.context.scene.ue2rigify
                    properties.source_rig.select_set(True)


def organize_rig_objects(organize_control_rig=True):
    """
    Moves the collections and objects that are created during the rig generating process to the extras
    collection and collapses all the collections in the outliner.

    :param bool organize_control_rig: Whether to organize the control rig and its widgets or not.
    """
    # get the extras collection or create it if it doesn't exist
    extras_collection = bpy.data.collections.get(Collections.EXTRAS_COLLECTION_NAME)
    if not extras_collection:
        extras_collection = bpy.data.collections.new(Collections.EXTRAS_COLLECTION_NAME)
        bpy.context.scene.collection.children.link(extras_collection)

    # get the widgets and constraint collections
    widgets_collection = bpy.data.collections.get(Rigify.WIDGETS_COLLECTION_NAME)
    constraints_collection = bpy.data.collections.get(Collections.CONSTRAINTS_COLLECTION_NAME)

    # move the constraints collection to the extras collection
    if constraints_collection:
        move_collection_to_collection(constraints_collection, extras_collection)
        constraints_collection.hide_viewport = True

    # collapse the collections in the outliner
    utilities.toggle_expand_in_outliner()

    if organize_control_rig:
        # move the control rig to the extras collection
        control_rig = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)
        move_scene_object_to_collection(control_rig, extras_collection)

        # move the constraints collection to the extras collection
        if widgets_collection:
            move_collection_to_collection(widgets_collection, extras_collection)


def parent_empties(parent_prefix, child_prefix, constraints_collection, links_data):
    """
    This function parents empties based on the provided prefix names.

    :param str parent_prefix: The parent prefix name.
    :param str child_prefix: The child prefix name.
    :param object constraints_collection: The constraints collection.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    for empty_object in constraints_collection.objects:
        empty_prefix = f"{child_prefix or parent_prefix}_"

        # get the name of the empty that will be the parent
        child_name = empty_object.name.replace(empty_prefix, "")

        parent_name = None
        for link in links_data:
            if link.get('from_socket') == child_name:
                parent_name = link.get('to_socket')
                break

        parent_empty_name = f'{parent_prefix}_{parent_name}'

        if empty_object.name.startswith(child_prefix):
            empty_parent = bpy.data.objects.get(parent_empty_name)
            if empty_parent:
                # set the empty objects parent
                empty_object.parent = empty_parent
                empty_object.matrix_parent_inverse = empty_parent.matrix_world.inverted()


def constrain_bones(links_data, from_rig_object, to_rig_object, properties):
    """
    This function constrains bones based on the provided links data.

    :param list links_data: A list of dictionaries that contains link attributes.
    :param object from_rig_object: The rig that is driving another rig by using constraints.
    :param object to_rig_object: The rig that is being driven by constraints.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    parent_name = 'parent'
    child_name = 'child'

    if links_data:
        if properties.selected_mode != Modes.CONTROL.name:
            utilities.save_source_mode_context(properties)

        # set the current frame to zero
        frame_current = bpy.context.scene.frame_current
        bpy.context.scene.frame_set(frame=0)

        # create the constraints collection
        constraints_collection = create_constraints_collection(properties)

        # create the parent and child empty objects
        create_empties(to_rig_object, parent_name, constraints_collection, links_data, 'to_socket')
        create_empties(from_rig_object, child_name, constraints_collection, links_data, 'from_socket')

        # parent the children empties to the parent empties
        parent_empties(parent_name, child_name, constraints_collection, links_data)

        # constrain the bone to the child empty and the child empty to the parent empty
        create_constraints(from_rig_object, links_data, child_name, 'from_socket', properties)
        create_constraints(to_rig_object, links_data, parent_name, 'to_socket', properties)

        bpy.context.scene.frame_set(frame=frame_current)

        if properties.selected_mode != Modes.CONTROL.name:
            utilities.load_source_mode_context(properties)


def constrain_fk_to_source(control_rig_object, source_rig_object, properties):
    """
    Constrains the control rig's FK bones to the source bones.

    :param object control_rig_object: The control rig object.
    :param object source_rig_object: The source rig object.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # set the template paths to fk to source mode
    templates.set_template_files(properties, mode_override=Modes.FK_TO_SOURCE.name)

    # constrain the control rig FKs to the source rig
    fk_to_source_link_data = templates.get_saved_links_data(properties)
    constrain_bones(fk_to_source_link_data, control_rig_object, source_rig_object, properties)

    organize_rig_objects()


def constrain_source_to_deform(source_rig_object, control_rig_object, properties):
    """
    Constrains the source bones to the control rig's deform bones.

    :param object source_rig_object: The source rig object.
    :param object control_rig_object: The control rig object.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    print('constraining')
    print(source_rig_object.name)
    print(control_rig_object.name)

    if properties.selected_mode == Modes.SOURCE_TO_DEFORM.name:
        # remove the control rig constraints
        links_data = templates.get_saved_links_data(properties)

        # set the template paths to fk to source mode
        remove_constraints(source_rig_object, properties, 'from_socket', links_data)

    # constrain the source rig to the deform bones on the control rig
    source_to_deform_links = templates.get_saved_links_data(properties)
    constrain_bones(source_to_deform_links, source_rig_object, control_rig_object, properties)

    organize_rig_objects()


def update_rig_constraints(self=None, value=None):
    """
    Called every time a node link or node gets added the the bone remapping node tree.

    :param object self: This is a reference to the property this functions in appended to.
    :param object value: The value of the property this update function is assigned to.
    """
    properties = bpy.context.scene.ue2rigify

    # get the current links and node data
    links_data = nodes.get_links_data()
    node_data = nodes.get_node_data()

    # save the node data and links
    templates.save_json_file(node_data, properties.saved_node_data)
    templates.save_json_file(links_data, properties.saved_links_data)

    # get the rig objects
    source_rig = properties.source_rig
    control_rig = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)

    if properties.selected_mode == Modes.FK_TO_SOURCE.name:
        # remove the control rig constraints that bind the FKs to the source bones
        remove_constraints(control_rig, properties, 'from_socket', links_data)

        # constrain the control rig fk bones to the source bones on the control rig
        constrain_bones(links_data, control_rig, source_rig, properties)

    if properties.selected_mode == Modes.SOURCE_TO_DEFORM.name:
        # remove the source rig constraints that bind the source bones to the control rig deform bones
        remove_constraints(source_rig, properties, 'from_socket', links_data)

        # constrain the source rig to the deform bones on the control rig
        constrain_bones(links_data, source_rig, control_rig, properties)

    organize_rig_objects(organize_control_rig=False)

    # make sure you stay in pose mode
    if bpy.context.mode != 'POSE':
        bpy.context.view_layer.objects.active = control_rig
        bpy.ops.object.mode_set(mode='POSE')


def sync_nla_track_data(control_rig_object, source_rig_object):
    """
    This function syncs the nla track data between the control and source rig to ensure they match.

    :param object source_rig_object: The source rig object.
    :param object control_rig_object: The control rig object.
    """
    if control_rig_object and source_rig_object:
        # get the nla tracks on the source rig
        control_nla_tracks = control_rig_object.animation_data.nla_tracks
        source_nla_tracks = source_rig_object.animation_data.nla_tracks

        # remove all the nla tracks from the source rig
        utilities.remove_nla_tracks(source_nla_tracks)

        for control_nla_track in control_nla_tracks:
            # create a new nla track on the source rig
            source_nla_track = source_nla_tracks.new()
            source_nla_track.name = control_nla_track.name

            # set the nla track mute value and deselect it
            source_nla_track.mute = control_nla_track.mute
            if control_nla_track.is_solo:
                source_nla_track.is_solo = control_nla_track.is_solo
            source_nla_track.select = False

            for control_strip in control_nla_track.strips:
                if control_strip.action:
                    # get the source action
                    source_action_name = f'{Modes.SOURCE.name}_{control_strip.action.name}'
                    source_action = bpy.data.actions.get(source_action_name)

                    # if there is not a matching source action, create one
                    if not source_action:
                        source_action = bpy.data.actions.new(source_action_name)

                    # create a new nla strip on the source rig
                    source_strip = source_nla_track.strips.new(
                        name=control_strip.name,
                        start=int(control_strip.frame_start),
                        action=source_action
                    )

                    # sync the strip values
                    source_strip.name = control_strip.name
                    source_strip.frame_start = control_strip.frame_start
                    source_strip.frame_end = control_strip.frame_end
                    source_strip.select = False


def sync_actions(control_rig_object, source_rig_object):
    """
    Syncs the actions between the control and source rig to ensure they match.

    :param object source_rig_object: The source rig object.
    :param object control_rig_object: The control rig object.
    """
    auto_stash_active_action = bpy.context.scene.send2ue.auto_stash_active_action

    # automatically stash the active animation if that setting is on in send2ue
    if auto_stash_active_action:
        utilities.stash_animation_data(control_rig_object)

    # sync nla track data from the control rig to the source rig
    sync_nla_track_data(control_rig_object, source_rig_object)


def bake_pose_animation(to_rig_object, from_rig_action_data, links_data, only_selected):
    """
    Bakes the visual pose bone transform data to the rig driven by constraints.

    :param object to_rig_object: The rig that is being driven by constraints.
    :param dict from_rig_action_data: A dictionary of animation data from the from rig.
    :param bool only_selected: A whether or not to only bake the selected bones.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    # select the bones on the to rig which have related keyed bones on the from rig
    select_related_keyed_bones(to_rig_object, from_rig_action_data, links_data)

    # bake the visual transforms of the selected bones to the current action
    bpy.ops.nla.bake(
        frame_start=int(from_rig_action_data['action_frame_start']),
        frame_end=int(from_rig_action_data['action_frame_end']),
        step=1,
        only_selected=only_selected,
        visual_keying=True,
        use_current_action=True,
        bake_types={'POSE'}
    )


def bake_object_animation(to_rig_object, from_rig_action_data, links_data):
    """
    This function bakes the visual object transform data to the source rig.

    :param object to_rig_object: The rig that is being driven by constraints.
    :param dict from_rig_action_data: A dictionary of animation data from the from rig.
    :param list links_data: A list of dictionaries that contains link attributes.
    """
    selected_objects = bpy.context.selected_objects.copy()
    mode = bpy.context.mode

    # switch to object mode
    if mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    # select the to rig object if it has related keyed bones on the from rig
    select_related_keyed_bones(to_rig_object, from_rig_action_data, links_data)

    bpy.ops.nla.bake(
        frame_start=int(from_rig_action_data['action_frame_start']),
        frame_end=int(from_rig_action_data['action_frame_end']),
        step=1,
        only_selected=True,
        visual_keying=True,
        use_current_action=True,
        bake_types={'OBJECT'}
    )

    # restore previous mode and selection
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode=mode)
    for selected_object in selected_objects:
        selected_object.select_set(True)


def bake_from_rig_to_rig(from_rig_object, to_rig_object, properties, bake_to_source=False):
    """
    This function takes all the keys on the bones of the from_rig_object rig and adds visual keys to the correct bones
    on the to_rig_object(The rig that is driven by the constraints).

    :param object from_rig_object: The rig that is driving another rig by using constraints.
    :param object to_rig_object: The rig that is being driven by constraints.
    :param object properties: The property group that contains variables that maintain the addon's correct state.
    :param bool bake_to_source: This flag states whether or not the action is being baked to the source rig.
    """
    # get the node links data in fk to source mode
    templates.set_template_files(properties, mode_override=Modes.FK_TO_SOURCE.name)
    links_data = templates.get_saved_links_data(properties, reverse=not bake_to_source)

    if links_data:
        # get the from rig's animation data
        from_rig_animation_data = get_from_rig_animation_data(from_rig_object, to_rig_object, links_data, properties)

        # get the from rig action attributes
        from_rig_action_attributes = utilities.get_all_action_attributes(from_rig_object)

        # remove all solo track values
        utilities.set_solo_track_values(from_rig_object, False)

        # go through the from rig's animation data
        for action_name, from_rig_action_data in from_rig_animation_data.items():

            # get the rig actions
            from_rig_action = bpy.data.actions.get(action_name)
            to_rig_action = get_to_rig_action(from_rig_action, from_rig_object, to_rig_object, bake_to_source, properties)

            # if there is no to rig action to bake to, return and don't bake
            if not to_rig_action:
                return None

            # make the rig action a fake user so that it is saved to the blend file.
            if not bake_to_source:
                to_rig_action.use_fake_user = True

            # remove all existing keyframes when baking to source
            if bake_to_source:
                for fcurve in to_rig_action.fcurves:
                    to_rig_action.fcurves.remove(fcurve)

            if from_rig_action_data['data'] or properties.bake_every_bone:
                # bake the visual pose transforms of the bones to the current action
                bake_pose_animation(
                    to_rig_object,
                    from_rig_action_data,
                    links_data,
                    only_selected=not properties.bake_every_bone
                )

                # remove the control rig action when baking to source
                if bake_to_source:
                    # bake the visual object transforms of the source rig object to the current action
                    bake_object_animation(to_rig_object, from_rig_action_data, links_data)

                    bpy.data.actions.remove(from_rig_action)
                    to_rig_action.name = to_rig_action.name.replace(f'{Modes.SOURCE.name}_', '')

                set_action_to_nla_strip(to_rig_action, to_rig_object, from_rig_action_data, properties)

            # restore the from rig action attributes
            utilities.set_all_action_attributes(from_rig_object, from_rig_action_attributes)


def save_meta_rig(properties):
    """
    This function saves the metarig to the current template.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    metarig_object = bpy.data.objects.get(Rigify.META_RIG_NAME)

    if metarig_object:
        if properties.selected_rig_template != 'create_new':
            # this ensures that the rig is saved with a scale of one relative to the scene scale
            metarig_object.scale = metarig_object.scale / (1 / bpy.context.scene.unit_settings.scale_length)
            utilities.operator_on_object_in_mode(
                lambda: bpy.ops.object.transform_apply(location=False, rotation=False, scale=True),
                metarig_object,
                'OBJECT'
            )

        # save the metarig data if there is any
        metarig_data = templates.get_metarig_data(properties)
        if metarig_data:
            # if creating a new template
            if properties.selected_rig_template == 'create_new':
                # if there is a new template name
                if properties.new_template_name:
                    # create the new template folder and save the metarig
                    templates.set_template_files(properties)
                    templates.create_template_folder(properties.new_template_name, properties)
                    templates.save_text_file(metarig_data, properties.saved_metarig_data)

                    # set the newly created template to the active template
                    properties.selected_rig_template = re.sub(r'\W+', '_', properties.new_template_name).lower()
                # otherwise set the active template to the default
                else:
                    properties.selected_rig_template = Template.DEFAULT_MALE_TEMPLATE
            # otherwise just save the active template
            else:
                templates.save_text_file(metarig_data, properties.saved_metarig_data)

        # save the constraints data if there is any
        if bpy.app.version[0] <= 2 and bpy.app.version[1] < 92:
            metarig_object = bpy.data.objects.get(Rigify.META_RIG_NAME)
            constraints_data = templates.get_constraints_data(metarig_object)
            if constraints_data:
                templates.save_constraints(constraints_data, properties)


def save_rig_nodes(properties):
    """
    Saves the rig nodes to the current template.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # stop checking the node tree for updates
    properties.check_node_tree_for_updates = False

    # save the node tree data
    node_data = nodes.get_node_data()

    nodes.remove_pie_menu_hot_keys()

    if node_data:
        templates.save_json_file(node_data, properties.saved_node_data)

        # remove the added nodes and their categories
        nodes.remove_added_node_class(nodes.node_classes)
        nodes.remove_node_categories()
        nodes.node_classes.clear()
        properties.categorized_nodes.clear()

        # remove the node editor interface
        node_editor.unregister()


def load_metadata(properties):
    """
    Loads metadata from the template and applies it to the rig.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    if properties.selected_mode == Modes.CONTROL.name:

        # load the visual data
        visual_data = templates.load_template_file_data(f'{Modes.CONTROL.name.lower()}_metadata.json', properties)
        rig_object = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)

        # set the rig object attributes
        for attribute, value in visual_data.get('object', {}).items():
            setattr(rig_object, attribute, value)

        # set the rig armature attributes
        for attribute, value in visual_data.get('armature', {}).items():
            setattr(rig_object.data, attribute, value)

        # set the bone groups
        for bone_group_name, bone_group_data in visual_data.get('bone_groups', {}).items():
            bone_group = rig_object.pose.bone_groups.get(bone_group_name)
            if not bone_group:
                bone_group = rig_object.pose.bone_groups.new(name=bone_group_name)
            bone_group.color_set = bone_group_data['color_set']
            bone_group.colors.active = bone_group_data['colors']['active']
            bone_group.colors.normal = bone_group_data['colors']['normal']
            bone_group.colors.select = bone_group_data['colors']['select']

        # set the bone attributes
        for bone_name, bone_data in visual_data.get('bones', {}).items():
            bone = rig_object.pose.bones.get(bone_name)
            if not bone:
                continue

            # set the custom bone shapes
            custom_shape_data = bone_data.get('custom_shape', {})
            custom_shape = bpy.data.objects.get(custom_shape_data.get('name', ''))
            if custom_shape:
                bone.custom_shape = custom_shape
                bone.custom_shape_translation = custom_shape_data['translation']
                bone.custom_shape_rotation_euler = custom_shape_data['rotation']
                bone.custom_shape_scale_xyz = custom_shape_data['scale']
                bone.use_custom_shape_bone_size = custom_shape_data['use_bone_size']

            # set the bone group
            bone_group = rig_object.pose.bone_groups.get(bone_data.get('bone_group', ''))
            if bone_group:
                bone.bone_group = bone_group


def save_metadata(properties):
    """
    Saves the rigify visual metadata when in control mode, like custom bone shape widgets and bone group colors.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    if properties.previous_mode == Modes.CONTROL.name:
        rig_object = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)
        if rig_object:
            visual_data = {
                'object': {
                    'show_in_front': rig_object.show_in_front
                },
                'armature': {
                    'show_group_colors': rig_object.data.show_group_colors,
                    'show_names': rig_object.data.show_names,
                    'show_bone_custom_shapes': rig_object.data.show_bone_custom_shapes
                },
                'bones': {},
                'bone_groups': {}
            }

            # save the bone settings
            for bone in rig_object.pose.bones:
                bone_data = {}

                # save custom bone shape
                if bone.custom_shape:
                    bone_data['custom_shape'] = {
                        'name': bone.custom_shape.name,
                        'translation': bone.custom_shape_translation[:],
                        'rotation': bone.custom_shape_rotation_euler[:],
                        'scale': bone.custom_shape_scale_xyz[:],
                        'use_bone_size': bone.use_custom_shape_bone_size
                    }
                # save bone group
                if bone.bone_group:
                    bone_data['bone_group'] = bone.bone_group.name

                if bone_data:
                    visual_data['bones'][bone.name] = bone_data

            # save the bone_groups
            for bone_group in rig_object.pose.bone_groups:
                visual_data['bone_groups'][bone_group.name] = {
                    'color_set': bone_group.color_set,
                    'colors': {
                        'normal': bone_group.colors.normal[:],
                        'select': bone_group.colors.select[:],
                        'active': bone_group.colors.active[:],
                    }
                }

            file_path = templates.get_template_file_path(f'{Modes.CONTROL.name.lower()}_metadata.json', properties)
            templates.save_json_file(visual_data, file_path)


def edit_meta_rig_template(properties):
    """
    Switches the addons state to edit metarig mode.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # if not creating a new template, initiate the
    if properties.selected_rig_template != 'create_new':
        meta_rig_object = create_meta_rig(properties)
    else:
        meta_rig_object = create_starter_metarig_template(properties)

    # set the viewport settings
    utilities.set_viewport_settings({
        properties.source_rig.name: metarig_mode_settings['source_rig'],
        meta_rig_object.name: metarig_mode_settings['metarig']},
        properties
    )


def edit_fk_to_source_nodes(properties):
    """
    Switches the addons state to edit fk to source mode.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # get the source and control rig objects
    source_rig = properties.source_rig
    control_rig = create_control_rig(properties)

    if control_rig and source_rig:
        # constrain the fk bones to the source bones
        constrain_fk_to_source(control_rig, source_rig, properties)

        # populate the node tree and create the nodes links
        links_data = templates.get_saved_links_data(properties)
        node_data = templates.get_saved_node_data(properties)

        nodes.populate_node_tree(node_data, links_data, properties)

        # set the viewport settings
        utilities.set_viewport_settings({
            control_rig.name: fk_to_source_mode_settings['control_rig'],
            source_rig.name: fk_to_source_mode_settings['source_rig']},
            properties
        )

        nodes.create_pie_menu_hot_key(view_3d.VIEW3D_PIE_MT_CreateNodes, 'ONE', 'Pose')


def edit_source_to_deform_nodes(properties):
    """
    Switches the addons state to edit fk to source mode.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # get the source and control rig objects
    source_rig = properties.source_rig
    control_rig_object = create_control_rig(properties)

    if control_rig_object and source_rig:
        # constrain the source bones to the deform bones
        constrain_source_to_deform(source_rig, control_rig_object, properties)

        # populate the node tree and create the nodes links
        links_data = templates.get_saved_links_data(properties)
        node_data = templates.get_saved_node_data(properties)
        nodes.populate_node_tree(node_data, links_data, properties)

        # set the viewport settings
        utilities.set_viewport_settings({
            source_rig.name: source_to_deform_mode_settings['source_rig'],
            control_rig_object.name: source_to_deform_mode_settings['control_rig']},
            properties
        )

        nodes.create_pie_menu_hot_key(view_3d.VIEW3D_PIE_MT_CreateNodes, 'ONE', 'Pose')


def revert_to_source_rig(properties):
    """
    Removes any previously generated objects and constraints.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """
    # get the widgets and constraints collection
    widget_collection = bpy.data.collections.get(Rigify.WIDGETS_COLLECTION_NAME)
    constraints_collection = bpy.data.collections.get(Collections.CONSTRAINTS_COLLECTION_NAME)

    # remove the widgets collection and its objects
    if widget_collection:
        for widget_object in widget_collection.objects:
            bpy.data.objects.remove(widget_object)
        bpy.data.collections.remove(widget_collection)

    # remove the constraints collection and its objects
    if constraints_collection:
        for constraint_object in constraints_collection.objects:
            bpy.data.objects.remove(constraint_object)
        bpy.data.collections.remove(constraints_collection)

    # remove the control rig if it exists
    control_rig_object = bpy.data.objects.get(Rigify.CONTROL_RIG_NAME)
    if control_rig_object:
        bpy.data.objects.remove(control_rig_object)

    # remove the constraints from the bones
    templates.set_template_files(properties, mode_override=Modes.FK_TO_SOURCE.name)
    links_data = templates.get_saved_links_data(properties)
    remove_constraints(properties.source_rig, properties, 'to_socket', links_data)

    # remove the metarig
    remove_metarig()

    # remove the transform change to the action if the previous mode was control mode
    if properties.previous_mode == Modes.CONTROL.name:
        utilities.load_source_mode_context(properties)


def convert_to_control_rig(properties):
    """
    Creates the control rig from a template and constrains the source rig to it and bakes over
    any animations.

    :param object properties: The property group that contains variables that maintain the addon's correct state.
    """

    source_rig = properties.source_rig
    control_rig_object = create_control_rig(properties)

    source_object_transforms = utilities.get_object_transforms(source_rig)
    utilities.set_object_transforms(control_rig_object, source_object_transforms)

    if control_rig_object and source_rig:
        # change all the control rigs IKs to FKs
        set_fk_ik_switch_values(control_rig_object, 1.0)
        utilities.match_rotation_modes(properties)

        if source_rig.animation_data:
            # constrain the fk bones to the source bones
            constrain_fk_to_source(control_rig_object, source_rig, properties)

            # bake the animation data from the source rig to the control rig
            bake_from_rig_to_rig(source_rig, control_rig_object, properties)

            links_data = templates.get_saved_links_data(properties)
            remove_constraints(control_rig_object, properties, 'from_socket', links_data)

        # set the template paths to source to deform mode
        templates.set_template_files(properties, mode_override=Modes.SOURCE_TO_DEFORM.name)

        # constrain the source bones to the deform bones
        constrain_source_to_deform(source_rig, control_rig_object, properties)

        organize_rig_objects()

        # set the metadata from the template
        load_metadata(properties)

        # set the viewport settings
        utilities.set_viewport_settings({
            source_rig.name: control_mode_settings['source_rig'],
            control_rig_object.name: control_mode_settings['control_rig']},
            properties
        )


def switch_modes(self=None, context=None):
    """
    Called every time the mode enumeration dropdown is updated.

    :param object self: This is a reference to the class this functions in appended to.
    :param object context: The context of the object this function is appended to.
    """
    properties = bpy.context.scene.ue2rigify

    # only switch modes if validations pass
    validation_manager = validations.ValidationManager(properties)
    if validation_manager.run():

        # save the current context
        utilities.save_context(properties)

        # save the metarig
        save_meta_rig(properties)

        # save and remove the nodes
        save_rig_nodes(properties)

        # save metadata
        save_metadata(properties)

        # revert to the source rig
        revert_to_source_rig(properties)

        # restore the source mode viewport settings
        utilities.restore_viewport_settings()

        # set the previous mode to the current mode
        properties.previous_mode = properties.selected_mode

        # set the template files to the previous mode
        templates.set_template_files(properties)

        if properties.selected_mode == Modes.METARIG.name:
            edit_meta_rig_template(properties)

        if properties.selected_mode == Modes.FK_TO_SOURCE.name:
            edit_fk_to_source_nodes(properties)

        if properties.selected_mode == Modes.SOURCE_TO_DEFORM.name:
            edit_source_to_deform_nodes(properties)

        if properties.selected_mode == Modes.CONTROL.name:
            convert_to_control_rig(properties)

        # clear the undo history
        utilities.clear_undo_history()

        # restore the context
        utilities.load_context(properties)
