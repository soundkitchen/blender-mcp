# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for returning the world-space bounds, element counts and modifier
settings of objects.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import Any, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_Depsgraph = Any
_Mesh = Any
_Modifier = Any
_Object = Any


class Params(NamedTuple):
    names: list[str]


class Result(NamedTuple):
    status: str
    objects: list[dict[str, Any]]
    not_found: list[str]


# @include_begin: _template_rna_props_to_json.py
def _rna_value_to_json(value: Any) -> Any:
    return value


def _rna_props_to_json(
        struct: Any,
        skip: tuple[str, ...] = (),
        skip_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {}
# @include_end


# Reported at the top level of each modifier (not under `settings`).
_MODIFIER_COMMON = (
    "name",
    "type",
    "show_viewport",
    "show_render",
    "show_in_editmode",
    "show_on_cage",
)
# UI state, not relevant to the result.
_MODIFIER_SKIP = _MODIFIER_COMMON + (
    "is_active",
    "show_expanded",
    "show_group_selector",
    "show_manage_panel",
    "use_pin_to_last",
)
_MODIFIER_SKIP_PREFIXES = ("open_",)


def _mesh_counts(mesh: _Mesh) -> dict[str, int]:
    from array import array

    loop_totals = array("i", bytes(4 * len(mesh.polygons)))
    mesh.polygons.foreach_get("loop_total", loop_totals)
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        # An n-gon is split into n - 2 triangles.
        "triangles": sum(loop_totals) - 2 * len(loop_totals),
    }


def _bounds(coords: Any) -> dict[str, list[float]] | None:
    """
    Return the axis-aligned bounds of the flat ``x, y, z, ...`` sequence *coords*.
    """
    if not coords:
        return None
    lo = [min(coords[axis::3]) for axis in range(3)]
    hi = [max(coords[axis::3]) for axis in range(3)]
    return {
        "min": lo,
        "max": hi,
        "size": [b - a for a, b in zip(lo, hi)],
    }


def _geometry_info(obj: _Object, depsgraph: _Depsgraph) -> dict[str, Any]:
    """
    Return world-space bounds and element counts, before and after modifiers.
    """
    from array import array

    counts_original = _mesh_counts(obj.data) if obj.type == 'MESH' else None
    counts_evaluated = None
    bounds = None

    obj_eval = obj.evaluated_get(depsgraph)
    try:
        mesh = obj_eval.to_mesh()
    except RuntimeError:
        # Types that can't be converted to a mesh (e.g. empties, lights).
        mesh = None
    if mesh is not None:
        try:
            counts_evaluated = _mesh_counts(mesh)
            # A temporary copy, transforming it leaves the object untouched.
            mesh.transform(obj_eval.matrix_world)
            coords = array("f", bytes(4 * 3 * len(mesh.vertices)))
            mesh.vertices.foreach_get("co", coords)
            bounds = _bounds(coords)
        finally:
            obj_eval.to_mesh_clear()
    elif obj.type in {'CURVES', 'POINTCLOUD', 'VOLUME', 'GREASEPENCIL'}:
        # No mesh, fall back to the (looser) bounds of the rotated local box.
        from mathutils import Vector  # pylint: disable=import-error,no-name-in-module
        matrix = obj_eval.matrix_world
        bounds = _bounds([v for corner in obj_eval.bound_box for v in matrix @ Vector(corner)])

    return {
        "bounds_world": bounds,
        "counts_original": counts_original,
        "counts_evaluated": counts_evaluated,
    }


def _nodes_modifier_inputs(mod: _Modifier) -> list[dict[str, Any]]:
    """
    Return the group inputs of a Geometry Nodes modifier, by socket name.
    """
    group = mod.node_group
    if group is None:
        return []
    # Blender 5.2+ exposes the inputs as RNA, older versions as ID properties.
    properties = getattr(mod, "properties", None)
    rna_inputs = properties.inputs if properties is not None else None

    result = []
    for item in group.interface.items_tree:
        if item.item_type != 'SOCKET' or item.in_out != 'INPUT':
            continue
        if item.socket_type == "NodeSocketGeometry":
            continue
        identifier = item.identifier
        info: dict[str, Any] = {
            "name": item.name,
            "identifier": identifier,
            "socket_type": item.socket_type,
        }
        if rna_inputs is not None:
            socket = getattr(rna_inputs, identifier, None)
            if socket is not None:
                info["value"] = _rna_value_to_json(getattr(socket, "value", None))
                info["input_type"] = socket.type
                if hasattr(socket, "attribute_name"):
                    info["attribute_name"] = socket.attribute_name
        elif identifier in mod:
            info["value"] = _rna_value_to_json(mod[identifier])
            if mod.get(identifier + "_use_attribute"):
                info["input_type"] = 'ATTRIBUTE'
                info["attribute_name"] = mod.get(identifier + "_attribute_name", "")
        result.append(info)
    return result


def _modifier_info(mod: _Modifier) -> dict[str, Any]:
    info: dict[str, Any] = {key: getattr(mod, key) for key in _MODIFIER_COMMON}
    info["settings"] = _rna_props_to_json(mod, skip=_MODIFIER_SKIP, skip_prefixes=_MODIFIER_SKIP_PREFIXES)
    if mod.type == 'NODES':
        info["inputs"] = _nodes_modifier_inputs(mod)
    return info


def _object_info(obj: _Object, depsgraph: _Depsgraph) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": obj.name,
        "type": obj.type,
    }
    info.update(_geometry_info(obj, depsgraph))
    info["modifiers"] = [_modifier_info(mod) for mod in obj.modifiers]
    return info


def main(params: Params) -> Result:
    import bpy  # pylint: disable=import-error,no-name-in-module

    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects = []
    not_found = []
    for name in params.names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            not_found.append(name)
            continue
        objects.append(_object_info(obj, depsgraph))
    return Result(
        status="ok",
        objects=objects,
        not_found=not_found,
    )
