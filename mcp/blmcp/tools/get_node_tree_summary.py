# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=C0114  # See tool doc-string.

__all__ = (
    "register",
)

from blmcp.tools_helpers import (
    toolcode_format_call,
    toolcode_load_from_filepath,
    toolcode_wrap_with_calling_convention,
)
from blmcp.tools_helpers.connection import send_code
from blmcp.tools.get_node_tree_summary_toolcode import NodeTreeKind, Params
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Node Tree Summary",
            read_only_hint=True,
        )
    )
    def get_node_tree_summary(
        kind: NodeTreeKind,
        name: str,
    ) -> dict[str, object]:
        """
        Return the nodes of a node tree with their settings, input values and links.

        Use to understand an existing material, world, light or node group
        before changing it, instead of reading nodes with Python.

        *kind* selects the data-block type and *name* its name:
        ``node_group`` covers Geometry Nodes, shader node groups and
        compositor node groups.

        ``output_node`` is the output the tree is evaluated from (for shader
        trees, the one targeting the scene's render engine, or all engines).
        Each node has its ``type``, ``used`` (feeds ``output_node`` through
        enabled inputs, or the pass-through inputs of muted nodes),
        ``mute``, ``settings`` (the node's own editable properties,
        data-blocks by name) and ``inputs``. Inputs that don't apply to the
        node's current settings are omitted. Linked inputs list their
        sources in ``linked_from`` as ``"Node.socket_identifier"``
        (skipping Reroute nodes and muted links), others hold their ``value``.
        Frame and Reroute nodes are not listed.
        Group nodes reference their group by name in ``settings``, use
        ``node_group`` to inspect it, ``groups_used`` lists the groups.
        ``interface`` lists a node group's inputs and outputs.
        """
        p = Params(kind=kind, name=name)
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)
