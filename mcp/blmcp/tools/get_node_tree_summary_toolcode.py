# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for returning the nodes, settings, input values and links of a node tree.
"""

__all__ = (
    "NodeTreeKind",
    "Params",
    "Result",
    "main",
)

from typing import Any, Literal, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_Node = Any
_NodeSocket = Any
_NodeTree = Any

NodeTreeKind = Literal[
    "material",
    "world",
    "light",
    # Geometry Nodes, shader node groups and compositor node groups.
    "node_group",
]


class Params(NamedTuple):
    kind: NodeTreeKind
    name: str


class Result(NamedTuple):
    status: str
    kind: str | None = None
    name: str | None = None
    tree_type: str | None = None
    output_node: str | None = None
    interface: list[dict[str, Any]] | None = None
    nodes: list[dict[str, Any]] | None = None
    groups_used: list[str] | None = None
    message: str | None = None


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


# Not reported: frames only group nodes visually, reroutes are skipped over in links.
_NODE_TYPES_SKIP = {"NodeFrame", "NodeReroute"}

# Shader trees pick their output node by render engine, other trees use the group output.
_SHADER_OUTPUT_TYPES = {
    "ShaderNodeOutputMaterial",
    "ShaderNodeOutputWorld",
    "ShaderNodeOutputLight",
}


def _data_collection(kind: str) -> Any:
    import bpy  # pylint: disable=import-error,no-name-in-module

    return {
        "material": bpy.data.materials,
        "world": bpy.data.worlds,
        "light": bpy.data.lights,
        "node_group": bpy.data.node_groups,
    }[kind]


def _shader_output_target(engine: str) -> str:
    """
    Return the ``get_output_node`` target for *engine*.

    An output targeting the engine takes precedence over one targeting all engines,
    which is used as a fallback.
    """
    # NOTE: keep in sync with `_world_output_target` in `get_scene_render_summary_toolcode.py`.
    if engine == 'CYCLES':
        return 'CYCLES'
    # NOTE: keep EEVEE engine IDs in sync with `_template_render_thumbnail_overrides.py`.
    if engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        return 'EEVEE'
    return 'ALL'


def _output_node(tree: _NodeTree) -> _Node | None:
    """
    Return the output node the tree is evaluated from, if any.
    """
    import bpy  # pylint: disable=import-error,no-name-in-module

    if any(node.bl_idname in _SHADER_OUTPUT_TYPES for node in tree.nodes):
        return tree.get_output_node(_shader_output_target(bpy.context.scene.render.engine))
    for node in tree.nodes:
        if node.bl_idname == "NodeGroupOutput" and node.is_active_output:
            return node
    return None


def _active_links(socket: _NodeSocket) -> list[Any]:
    # Muted links pass nothing, the socket then uses its own value.
    return [link for link in socket.links if not link.is_muted]


def _link_sources(socket: _NodeSocket) -> list[tuple[_Node, _NodeSocket]]:
    """
    Return the ``(node, output socket)`` pairs feeding *socket*, skipping Reroute nodes.
    """
    result = []
    for link in _active_links(socket):
        node, from_socket = link.from_node, link.from_socket
        seen = set()
        while node.bl_idname == "NodeReroute":
            # Guard against Reroute cycles.
            if node.name in seen:
                node = None
                break
            seen.add(node.name)
            links = _active_links(node.inputs[0])
            if not links:
                # A dangling Reroute passes nothing.
                node = None
                break
            node, from_socket = links[0].from_node, links[0].from_socket
        if node is not None:
            result.append((node, from_socket))
    return result


def _used_nodes(output: _Node | None) -> set[str]:
    """
    Return the names of the nodes feeding *output* (included), following links upstream.
    """
    if output is None:
        return set()
    used = set()
    stack = [output]
    while stack:
        node = stack.pop()
        if node.name in used:
            continue
        used.add(node.name)
        for socket in node.inputs:
            for from_node, _from_socket in _link_sources(socket):
                stack.append(from_node)
    return used


def _socket_ref(node: _Node, socket: _NodeSocket) -> str:
    # Socket names are not unique (e.g. the Mix node), the identifier is.
    return "{:s}.{:s}".format(node.name, socket.identifier)


def _input_info(socket: _NodeSocket) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": socket.name,
        "identifier": socket.identifier,
        "type": socket.type,
    }
    sources = _link_sources(socket)
    if sources:
        info["linked_from"] = [_socket_ref(node, from_socket) for node, from_socket in sources]
    elif hasattr(socket, "default_value"):
        info["value"] = _rna_value_to_json(socket.default_value)
    return info


def _node_settings(node: _Node) -> dict[str, Any]:
    """
    Return the node's own editable properties, skipping the ones every node has.
    """
    import bpy  # pylint: disable=import-error,no-name-in-module

    common = tuple(prop.identifier for prop in bpy.types.Node.bl_rna.properties)
    settings = _rna_props_to_json(node, skip=common, skip_prefixes=("bl_",))
    # Color ramps are structs (not data-blocks), report their stops explicitly.
    color_ramp = getattr(node, "color_ramp", None)
    if color_ramp is not None:
        settings["color_ramp"] = {
            "color_mode": color_ramp.color_mode,
            "interpolation": color_ramp.interpolation,
            "elements": [
                {"position": element.position, "color": list(element.color)}
                for element in color_ramp.elements
            ],
        }
    return settings


def _node_info(node: _Node, used: set[str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": node.name,
        "type": node.bl_idname,
    }
    if node.label:
        info["label"] = node.label
    info["used"] = node.name in used
    info["mute"] = node.mute
    info["settings"] = _node_settings(node)
    # Inputs that don't apply to the node's current settings are disabled (e.g. the Mix node),
    # virtual sockets are the empty slots of group input and output nodes.
    info["inputs"] = [
        _input_info(socket)
        for socket in node.inputs
        if socket.enabled and socket.bl_idname != "NodeSocketVirtual"
    ]
    return info


def _interface_info(tree: _NodeTree) -> list[dict[str, Any]]:
    result = []
    for item in tree.interface.items_tree:
        if item.item_type != 'SOCKET':
            continue
        info: dict[str, Any] = {
            "in_out": item.in_out,
            "name": item.name,
            "identifier": item.identifier,
            "socket_type": item.socket_type,
        }
        if hasattr(item, "default_value"):
            info["default_value"] = _rna_value_to_json(item.default_value)
        result.append(info)
    return result


def main(params: Params) -> Result:
    collection = _data_collection(params.kind)
    owner = collection.get(params.name)
    if owner is None:
        available = sorted(collection.keys())
        return Result(
            status="error",
            message="{:s} {!r} not found. Available: {:s}".format(
                params.kind, params.name, ", ".join(available) if available else "(none)",
            ),
        )

    tree = owner if params.kind == "node_group" else owner.node_tree
    if tree is None:
        return Result(
            status="error",
            message="{:s} {!r} has no node tree".format(params.kind, params.name),
        )

    output = _output_node(tree)
    used = _used_nodes(output)
    nodes = [
        _node_info(node, used)
        for node in tree.nodes
        if node.bl_idname not in _NODE_TYPES_SKIP
    ]
    nodes.sort(key=lambda node: node["name"])
    groups_used = sorted({
        node.node_tree.name
        for node in tree.nodes
        if getattr(node, "node_tree", None) is not None
    })
    return Result(
        status="ok",
        kind=params.kind,
        name=owner.name,
        tree_type=tree.bl_idname,
        output_node=output.name if output is not None else None,
        interface=_interface_info(tree) if params.kind == "node_group" else None,
        nodes=nodes,
        groups_used=groups_used,
    )
