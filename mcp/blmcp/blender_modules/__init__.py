# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Modules that run inside Blender, not in the MCP server.

``execute_blender_code`` registers them in Blender's ``sys.modules`` when the
code uses them (see ``blmcp.tools_helpers.blender_modules``), so they can be
imported by name, e.g. ``import blmcp_subd``.
Don't import them in the MCP server, they need ``bpy`` and ``bmesh``.
"""

__all__ = ()
