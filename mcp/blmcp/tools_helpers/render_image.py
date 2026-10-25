# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Shared handling for tools that return a render as an image.
"""

__all__ = (
    "render_response_as_image_and_info",
)

import base64
import json

from mcp.server.mcpserver import Image  # pylint: disable=import-error,no-name-in-module
from mcp.server.mcpserver.exceptions import ToolError  # pylint: disable=import-error,no-name-in-module


def render_response_as_image_and_info(response: dict[str, object]) -> tuple[Image, str]:
    """
    Convert the add-on *response* of a ``*_as_image`` render tool-code
    into an image (PNG, unless the result's ``image_format`` names another)
    and a JSON string with the render info.

    Raises ``ToolError`` when the add-on or the tool-code reports an error,
    its message reaches the client (unlike other exceptions, reported without the message).
    """
    if response.get("status") != "ok":
        raise ToolError(str(response.get("message", "Unknown error")))
    result = response["result"]
    assert isinstance(result, dict)
    if result.get("status") != "ok":
        raise ToolError(str(result.get("message", "Unknown error")))
    image_base64 = result.pop("image_base64")
    image_format = result.pop("image_format", None) or "png"
    del result["status"]
    info = {key: value for key, value in result.items() if value is not None}
    return (
        Image(data=base64.b64decode(str(image_base64)), format=str(image_format)),
        json.dumps(info),
    )
