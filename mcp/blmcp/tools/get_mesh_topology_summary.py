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
from blmcp.tools.get_mesh_topology_summary_toolcode import Params
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Mesh Topology Summary",
            read_only_hint=True,
        )
    )
    def get_mesh_topology_summary(
        name: str,
    ) -> dict[str, object]:
        """
        Return topology checks of the mesh object *name*, for use as a subdivision surface cage.

        Use while modeling with subdivision (e.g. characters) to find
        triangles, n-gons, poles, holes and symmetry problems in the cage.

        The cage is the mesh before modifiers (in Edit Mode, including
        unsynced edits). Vertices are listed by ``index`` with their
        object-space ``co``, edges by ``index`` and ``vertices``, faces by
        ``index``, ``sides`` and ``center``. Each check holds the ``count``
        and the first 50 ``items``.

        ``faces`` counts ``quads`` and lists ``triangles`` and ``ngons``.
        ``poles`` lists vertices with 3 (``valence_3``) or 5 or more
        (``valence_5_plus``) edges on the subdivided surface (boundary
        vertices are skipped, except on a mirror plane where their mirrored
        valence is used).
        ``boundary_edges`` are open edges (holes), edges on a mirror plane
        are not included. ``non_manifold_edges`` have more than two faces,
        ``wire_edges`` none, ``loose_vertices`` have no edges.
        ``inconsistent_normal_edges`` join faces whose normals are flipped
        relative to each other.
        ``creases`` counts edges and vertices with a crease, ``sharp_edges``
        the edges marked sharp.

        ``symmetry`` checks each axis of the Mirror modifiers (without a
        mirror object): ``off_center_vertices`` are close to the mirror plane
        but further than ``tolerance`` (the merge distance) from it,
        ``crossing_faces`` have vertices on both sides of the plane, and
        ``vertices_positive_side`` & ``vertices_negative_side`` count the
        vertices on each side (all should be on one side).
        ``modifiers`` lists the modifier stack, with ``settings`` for
        Mirror, Subdivision Surface and Multiresolution.
        ``subdivision_size`` compares the object-space size of the cage
        (mirrored) with the result after modifiers, as subdivision shrinks
        the shape. ``warnings`` points out problems in the modifier stack.
        """
        p = Params(name=name)
        code = toolcode_format_call(_TOOL_CALL, p)
        return send_code(code, strict_json=True)
