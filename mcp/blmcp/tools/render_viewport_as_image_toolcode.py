# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for rendering the current scene, returned as a PNG image.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from collections.abc import Callable
from typing import Any, NamedTuple

# Written inside the MCP scratch directory, overwritten on each call.
_OUTPUT_NAME = "render_viewport_as_image.png"


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


# @include_begin: _template_deferred_tool_check_for_file_output.py
def _deferred_tool_check_for_file_output(
        job_type: str,
        output_path: str,
        restore_attrs: list[tuple[object, str, object]] | None = None,
) -> Callable[[], dict[str, object] | None]:
    return lambda: None
# @include_end


# @include_begin: _template_image_downscale_to_size_limit.py
def _image_downscale_to_size_limit(
        tmpdir: str, filepath: str, size_limit_in_bytes: int, size_tolerance_in_bytes: int = 0,
        use_hidpi_downscale: bool = True,
) -> bytes:
    return b''
# @include_end


# @include_begin: _template_render_result_as_image.py
def _render_result_as_image(
        result: dict[str, object],
        size_limit_in_bytes: int,
        info: dict[str, object],
        time_start: float,
) -> dict[str, Any]:
    return result


def _deferred_tool_check_as_image(
        check_is_finished_for_file: Callable[[], dict[str, object] | None],
        size_limit_in_bytes: int,
        info: dict[str, object],
        time_start: float,
) -> Callable[[], dict[str, object] | None]:
    return check_is_finished_for_file
# @include_end


def main(params: Params) -> Result | Callable[[], dict[str, object] | None]:
    import os
    import time
    import bpy  # pylint: disable=import-error,no-name-in-module

    use_deferred = not bpy.app.background

    output_path = os.path.join(bpy.app.tempdir, "blender_mcp", _OUTPUT_NAME)

    scene = bpy.context.scene
    rd = scene.render

    # NOTE: `filepath` and the image format are restored once the render
    # completes (not via a context manager) because `write_still` reads them
    # after the render completes. With `INVOKE_DEFAULT` a context manager
    # would restore them before the file is written.
    # The format is forced to 8-bit PNG so the file can be returned as-is.
    restore_attrs: list[tuple[object, str, object]] = [
        (rd, "filepath", rd.filepath),
        (rd.image_settings, "file_format", rd.image_settings.file_format),
        (rd.image_settings, "color_depth", rd.image_settings.color_depth),
    ]
    rd.filepath = output_path
    rd.image_settings.file_format = 'PNG'
    rd.image_settings.color_depth = '8'

    def restore() -> None:
        for obj, attr, value in restore_attrs:
            setattr(obj, attr, value)

    render_args = ('INVOKE_DEFAULT',) if use_deferred else ()

    info: dict[str, object] = {
        "render_width": rd.resolution_x * rd.resolution_percentage // 100,
        "render_height": rd.resolution_y * rd.resolution_percentage // 100,
        "engine": rd.engine,
    }
    time_start = time.monotonic()
    try:
        bpy.ops.render.render(*render_args, write_still=True)
    except RuntimeError as ex:
        restore()
        return Result(status="error", message=str(ex))

    if use_deferred:
        return _deferred_tool_check_as_image(
            _deferred_tool_check_for_file_output('RENDER', output_path, restore_attrs=restore_attrs),
            params.size_limit_in_bytes, info, time_start,
        )

    restore()
    if not os.path.exists(output_path):
        return Result(status="error", message="Render completed but output file was not created")
    return Result(**_render_result_as_image(
        {"status": "ok", "filepath": output_path},
        params.size_limit_in_bytes, info, time_start,
    ))
