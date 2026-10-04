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
from blmcp.tools.render_thumbnail_as_image_toolcode import Params
from mcp.server.mcpserver import MCPServer, Image  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Render Thumbnail as Image",
            destructive_hint=True,
        )
    )
    def render_thumbnail_as_image(size_limit_in_bytes: int = 0) -> tuple[Image, str]:
        """
        Render a small, low-quality thumbnail and return it as a PNG image (temporarily overrides settings).

        Fast enough to check the result of each change visually.
        Returns the image followed by JSON render info: ``filepath``,
        ``image_width`` & ``image_height`` (of the returned image),
        ``render_width`` & ``render_height``, ``engine`` and
        ``render_time_seconds`` (approximate).

        *size_limit_in_bytes* caps the image size in bytes.
        Zero (the default) uses the MCP message size limit.
        """
        p = Params(size_limit_in_bytes=size_limit_in_bytes)
        response = send_code(toolcode_format_call(_TOOL_CALL, p), strict_json=True)
        return render_response_as_image_and_info(response)
