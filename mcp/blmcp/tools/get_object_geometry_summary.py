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
from blmcp.tools.get_object_geometry_summary_toolcode import Params
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Object Geometry Summary",
            read_only_hint=True,
        )
    )
    def get_object_geometry_summary(
        names: list[str],
    ) -> dict[str, object]:
        """
        Return world-space bounds, element counts and modifier settings of the objects in *names*.

        Use to check sizes, overlaps and contact between objects, and how
        modifiers shape them, before changing anything.

        ``bounds_world`` holds the ``min``, ``max`` and ``size`` of the
        axis-aligned world-space box around the geometry after modifiers
        (as shown in the viewport) and the instances with geometry the object
        generates (e.g. with Geometry Nodes or collection instancing, measured
        by their rotated local boxes), ``None`` for objects without geometry.
        ``instance_count`` is the number of those instances (instanced lights,
        cameras and empties are not counted).
        ``counts_original`` (mesh objects only) and ``counts_evaluated``
        (after modifiers, instances not included) hold the number of vertices,
        edges, faces and triangles. In Edit Mode, ``counts_original`` may not
        include unsynced edits.
        ``is_evaluated`` is false for objects outside the view layer (e.g. not
        linked to the scene or in an excluded collection), their
        ``bounds_world``, ``counts_evaluated`` and ``instance_count`` are ``None``.
        ``modifiers`` lists each modifier with its visibility flags and
        ``settings`` (all editable properties, data-blocks by name).
        Geometry Nodes modifiers also list their group ``inputs`` by name.
        Names that don't match an object are listed in ``not_found``.
        """
        p = Params(names=names)
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)
