# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline `_render_result_as_image` & `_deferred_tool_check_as_image`.
#
# Requires `_image_downscale_to_size_limit`,
# include `_template_image_downscale_to_size_limit.py` as well.

__all__ = ()

from collections.abc import Callable
from typing import Any

# MCP messages are limited to 1,048,576 bytes (1 MB). The image is base64-encoded
# which expands data by 4/3. Subtract 4096 for 4 KiB headroom for the JSON envelope
# and the render info sent along with the image.
_RENDER_IMAGE_SIZE_LIMIT_IN_BYTES = ((1_048_576 - 4096) * 3) // 4


def _render_result_as_image(
        result: dict[str, object],
        size_limit_in_bytes: int,
        info: dict[str, object],
        time_start: float,
) -> dict[str, Any]:
    """
    Convert the result of a render that wrote a PNG file into an image result.

    *result* is ``{"status": "ok", "filepath": ...}`` on success,
    error results are returned unchanged.
    *size_limit_in_bytes* caps the image size, zero uses the MCP message size limit.
    *info* is merged into the result (e.g. engine and render resolution).
    *time_start* is the ``time.monotonic()`` value when the render started.
    """
    import base64
    import struct
    import tempfile
    import time

    if result.get("status") != "ok":
        return result

    render_time = time.monotonic() - time_start
    filepath = str(result["filepath"])
    size_limit = size_limit_in_bytes if size_limit_in_bytes > 0 else _RENDER_IMAGE_SIZE_LIMIT_IN_BYTES

    with tempfile.TemporaryDirectory(prefix="blmcp_render_") as tmpdir:
        # Defined by `_template_image_downscale_to_size_limit.py` (see the header).
        image_data = _image_downscale_to_size_limit(  # type: ignore[name-defined]  # noqa: F821
            tmpdir, filepath,
            size_limit_in_bytes=size_limit,
            size_tolerance_in_bytes=size_limit // 16,
            use_hidpi_downscale=False,
        )

    # Read the dimensions from the PNG header (IHDR chunk).
    image_width, image_height = struct.unpack(">II", image_data[16:24])

    return {
        **info,
        "status": "ok",
        "filepath": filepath,
        "image_base64": base64.b64encode(image_data).decode("ascii"),
        "image_width": image_width,
        "image_height": image_height,
        "render_time_seconds": round(render_time, 2),
    }


def _deferred_tool_check_as_image(
        check_is_finished_for_file: Callable[[], dict[str, object] | None],
        size_limit_in_bytes: int,
        info: dict[str, object],
        time_start: float,
) -> Callable[[], dict[str, object] | None]:
    """
    Wrap a deferred checker (see ``_deferred_tool_check_for_file_output``)
    so the written file is returned as an image once the job completes.
    """

    def check_is_finished() -> dict[str, object] | None:
        result = check_is_finished_for_file()
        if result is None:
            return None
        return _render_result_as_image(result, size_limit_in_bytes, info, time_start)

    return check_is_finished
