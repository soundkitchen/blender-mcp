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
from blmcp.tools_helpers.render_image import render_response_as_image_and_info
from blmcp.tools.render_mesh_wireframe_as_image_toolcode import Params, ViewName
from mcp.server.mcpserver import MCPServer, Image  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Render Mesh Wireframe as Image",
            read_only_hint=True,
        ),
        # The result is unstructured (image + JSON text), see `render_viewport_as_image`.
        structured_output=False,
    )
    def render_mesh_wireframe_as_image(
        name: str,
        views: list[ViewName] | None = None,
        bounds_min: list[float] | None = None,
        bounds_max: list[float] | None = None,
        size_limit_in_bytes: int = 0,
    ) -> tuple[Image, str]:
        """
        Draw the mesh object *name* in orthographic views with its vertex indices, returned as a PNG image.

        Use while editing a mesh (e.g. a subdivision cage) to see which vertex
        index is where, then change vertices by index (``bm.verts[i]`` or
        ``mesh.vertices[i]``). The indices match ``get_mesh_topology_summary``.

        *views* are drawn side by side at the same scale, in object space
        (``FRONT``, ``BACK``, ``RIGHT``, ``LEFT``, ``TOP``, ``BOTTOM`` as in
        Blender's numpad views), defaulting to ``FRONT`` and ``RIGHT``.
        Each view shows the result after modifiers as a gray silhouette, the
        cage (the mesh before modifiers, in Edit Mode including unsynced edits)
        with its edges, and the visible vertices with their index. Edges hidden
        behind the cage are drawn faintly, hidden vertices are not labeled.

        *bounds_min* & *bounds_max* (object space, pass both) limit the drawing
        to a box, zooming in on details, e.g. a character's head.
        Labels that would overlap are skipped, narrow the bounds to see them.
        For a half cage mirrored by a Mirror modifier, view it from the side
        of the cage: from the mirror plane side, the open cage shows its inside.

        Returns the image followed by JSON info: ``views`` (each view's screen
        axes ``right`` & ``up`` and its ``rect`` in the image: x, y from the
        bottom left, width, height), ``image_width`` & ``image_height``,
        ``units_per_pixel``, the ``bounds_min``
        & ``bounds_max`` drawn, ``vertices_labeled``, ``vertices_unlabeled``
        (visible but skipped to avoid overlaps, the first 100 indices),
        ``vertices_hidden`` (hidden in every view) and
        ``vertices_outside_bounds``.

        *size_limit_in_bytes* is the target image size in bytes.
        Zero (the default) uses the MCP message size limit.
        """
        p = Params(
            name=name,
            views=views if views else ["FRONT", "RIGHT"],
            bounds_min=bounds_min,
            bounds_max=bounds_max,
            size_limit_in_bytes=size_limit_in_bytes,
        )
        response = send_code(toolcode_format_call(_TOOL_CALL, p), strict_json=True)
        return render_response_as_image_and_info(response)
