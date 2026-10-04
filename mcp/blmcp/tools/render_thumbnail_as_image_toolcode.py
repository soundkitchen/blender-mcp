# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for low-quality thumbnail rendering, returned as a PNG image.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from collections.abc import Callable
from typing import Any, NamedTuple

# Written inside the MCP scratch directory, overwritten on each call.
_OUTPUT_NAME = "render_thumbnail_as_image.png"


class Params(NamedTuple):
    size_limit_in_bytes: int = 0


class Result(NamedTuple):
    status: str
    image_base64: str | None = None
    filepath: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    render_width: int | None = None
    render_height: int | None = None
    engine: str | None = None
    render_time_seconds: float | None = None
    message: str | None = None


# @include_begin: _template_render_thumbnail_overrides.py
def _render_thumbnail_overrides(scene: Any) -> list[tuple[object, dict[str, object]]]:
    return []
# @include_end


# @include_begin: _template_image_downscale_to_size_limit.py
def _image_downscale_to_size_limit(
        tmpdir: str, filepath: str, size_limit_in_bytes: int, size_tolerance_in_bytes: int = 0,
        use_hidpi_downscale: bool = True,
) -> bytes:
    return b''
# @include_end


# @include_begin: _template_render_result_as_image.py
def _render_as_image(
        output_name: str,
        obj_attrs: list[tuple[object, dict[str, object]]],
        size_limit_in_bytes: int,
) -> dict[str, Any] | Callable[[], dict[str, Any] | None]:
    return {}
# @include_end


def main(params: Params) -> Result | Callable[[], dict[str, Any] | None]:
    import bpy  # pylint: disable=import-error,no-name-in-module

    obj_attrs = _render_thumbnail_overrides(bpy.context.scene)
    result = _render_as_image(_OUTPUT_NAME, obj_attrs, params.size_limit_in_bytes)
    if callable(result):
        return result
    return Result(**result)
