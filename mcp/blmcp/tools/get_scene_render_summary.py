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
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Scene Render Summary",
            read_only_hint=True,
        )
    )
    def get_scene_render_summary() -> dict[str, object]:
        """
        Return the scene's render settings, color management, world, lights and camera.

        Use to find out why a render looks too dark, washed out or noisy
        before changing anything.

        ``render`` holds the engine, resolution, frame range and
        ``samples`` (final render samples, ``None`` when the engine has no
        sample count). ``engine_settings`` only covers the active engine.
        ``color_management`` holds the display device, view transform
        (e.g. ``AgX``), look, exposure and gamma.
        ``world`` lists the Background nodes feeding the world output used by
        the render engine (color, strength and the node linked to each,
        if any, skipping Reroute nodes and muted links) and the
        Environment Texture images, it is ``None`` without a world.
        ``lights`` lists every light object in the scene, including hidden ones
        (see ``hide_render`` and ``visible``). ``energy`` is in watts,
        or irradiance in W/m^2 for sun lights.
        ``camera`` is the scene camera with lens, sensor, clipping, shift and
        depth of field, or ``None`` when the scene has no camera.
        ``location`` and ``direction`` are in world space,
        ``direction`` is ``None`` for point lights.
        """
        code = toolcode_format_call(_TOOL_CALL, None)
        return send_code(code, strict_json=True)
