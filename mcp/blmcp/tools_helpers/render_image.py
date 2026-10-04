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


def render_response_as_image_and_info(response: dict[str, object]) -> tuple[Image, str]:
    """
    Convert the add-on *response* of a ``*_as_image`` render tool-code
    into a PNG image and a JSON string with the render info.

    Raises ``RuntimeError`` when the add-on or the tool-code reports an error.
    """
    if response.get("status") != "ok":
        raise RuntimeError(str(response.get("message", "Unknown error")))
    result = response["result"]
    assert isinstance(result, dict)
    if result.get("status") != "ok":
        raise RuntimeError(str(result.get("message", "Unknown error")))
    image_base64 = result.pop("image_base64")
    del result["status"]
    info = {key: value for key, value in result.items() if value is not None}
    return (
        Image(data=base64.b64decode(str(image_base64)), format="png"),
        json.dumps(info),
    )
