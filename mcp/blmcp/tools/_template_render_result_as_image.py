# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline `_render_as_image` & `_render_file_as_image`.
#
# Requires `_image_downscale_to_size_limit`,
# include `_template_image_downscale_to_size_limit.py` as well.

__all__ = ()

from collections.abc import Callable
from typing import Any

# MCP messages are limited to 1,048,576 bytes (1 MB). The image is base64-encoded
# which expands data by 4/3. Subtract 4096 for 4 KiB headroom for the JSON envelope
# and the render info sent along with the image.
# NOTE: the same calculation as `_IMAGE_SIZE_LIMIT_IN_BYTES` in the screenshot tools,
# with more headroom.
_RENDER_IMAGE_SIZE_LIMIT_IN_BYTES = ((1_048_576 - 4096) * 3) // 4

# Larger renders are downscaled to this size (longest dimension) before fitting
# the size limit, to avoid repeatedly encoding huge images on the main thread.
# Fitting the default size limit usually needs a smaller image anyway.
_RENDER_IMAGE_DIMS_MAX = 2048

# Interval for restoring the settings once a render job finishes.
_RENDER_RESTORE_INTERVAL = 0.1


def _render_file_as_image(
        filepath: str,
        size_limit_in_bytes: int,
        info: dict[str, object],
        time_start: float,
) -> dict[str, Any]:
    """
    Return the rendered PNG at *filepath* as an image result.

    *size_limit_in_bytes* is the target image size, zero uses the MCP message size limit.
    *info* is merged into the result (e.g. engine and render resolution).
    *time_start* is the ``time.monotonic()`` value when the render started.
    """
    import base64
    import os
    import struct
    import tempfile
    import time

    render_time = time.monotonic() - time_start
    size_limit = size_limit_in_bytes if size_limit_in_bytes > 0 else _RENDER_IMAGE_SIZE_LIMIT_IN_BYTES

    with tempfile.TemporaryDirectory(prefix="blmcp_render_") as tmpdir:
        filepath_fit = filepath
        # Read the dimensions from the PNG header (IHDR chunk), avoids loading the image.
        with open(filepath, "rb") as fh:
            width, height = struct.unpack(">II", fh.read(24)[16:24])
        if max(width, height) > _RENDER_IMAGE_DIMS_MAX:
            import imbuf  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
            scale = _RENDER_IMAGE_DIMS_MAX / max(width, height)
            im = imbuf.load(filepath)
            try:
                im.resize((max(round(width * scale), 1), max(round(height * scale), 1)), method='BILINEAR')
                filepath_fit = os.path.join(tmpdir, "render_dims_max.png")
                imbuf.write(im, filepath=filepath_fit)
            finally:
                im.free()

        # Defined by `_template_image_downscale_to_size_limit.py` (see the header).
        image_data = _image_downscale_to_size_limit(  # type: ignore[name-defined]  # noqa: F821
            tmpdir, filepath_fit,
            size_limit_in_bytes=size_limit,
            size_tolerance_in_bytes=size_limit // 16,
            use_hidpi_downscale=False,
        )

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


def _render_as_image(
        output_name: str,
        obj_attrs: list[tuple[object, dict[str, object]]],
        size_limit_in_bytes: int,
) -> dict[str, Any] | Callable[[], dict[str, Any] | None]:
    """
    Render the current scene to *output_name* (in the MCP scratch directory)
    as an 8-bit PNG, and return it as an image result (see ``_render_file_as_image``).

    *obj_attrs* are additional temporary settings as ``(obj, {attr: value, ...})`` pairs.

    In background mode the render completes before returning a result ``dict``.
    Otherwise the render runs as a job and a deferred checker is returned
    (see ``deferred_tool.py``).

    All temporary settings are restored once the render job finishes (not when this
    function returns), because the render reads them while the job runs and
    ``write_still`` reads the output settings after the render completes.
    They are restored by a timer as well as by the checker, so they are restored
    even when the client disconnects or times out.
    """
    import os
    import time
    import bpy  # pylint: disable=import-error,no-name-in-module

    use_deferred = not bpy.app.background

    # Starting a render while one is running is cancelled, and would store the
    # temporary settings of the running render as the values to restore.
    if bpy.app.is_job_running('RENDER'):
        return {"status": "error", "message": "Another render is running, try again once it completes"}

    output_path = os.path.join(bpy.app.tempdir, "blender_mcp", output_name)
    # Remove the previous output, so a cancelled or failed render is not reported as a success.
    if os.path.exists(output_path):
        os.remove(output_path)

    rd = bpy.context.scene.render
    obj_attrs = [
        *obj_attrs,
        (rd, {"filepath": output_path}),
        # The order matters: `media_type` limits the available `file_format` values.
        (rd.image_settings, {"media_type": 'IMAGE', "file_format": 'PNG', "color_depth": '8'}),
    ]

    # Store all values before assigning any, as assigning `media_type` changes `file_format`.
    # Restored in the order assigned (`media_type` before `file_format`).
    restore_attrs: list[tuple[object, str, object]] = [
        (obj, attr, getattr(obj, attr))
        for obj, attrs in obj_attrs
        for attr in attrs
    ]
    is_restored = False

    def restore() -> None:
        nonlocal is_restored
        if is_restored:
            return
        is_restored = True
        for obj, attr, value in restore_attrs:
            try:
                setattr(obj, attr, value)
            except ReferenceError:
                pass  # The data was freed (e.g. a different file was loaded).

    render_args = ('INVOKE_DEFAULT',) if use_deferred else ()
    try:
        for obj, attrs in obj_attrs:
            for attr, value in attrs.items():
                setattr(obj, attr, value)
        info: dict[str, object] = {
            "render_width": rd.resolution_x * rd.resolution_percentage // 100,
            "render_height": rd.resolution_y * rd.resolution_percentage // 100,
            "engine": rd.engine,
        }
        time_start = time.monotonic()
        ret = bpy.ops.render.render(*render_args, write_still=True)
    except (RuntimeError, TypeError) as ex:
        restore()
        return {"status": "error", "message": str(ex)}

    if 'CANCELLED' in ret:
        restore()
        return {"status": "error", "message": "Render was cancelled"}

    def result_from_output() -> dict[str, Any]:
        if not os.path.exists(output_path):
            return {"status": "error", "message": "Render completed but output file was not created"}
        return _render_file_as_image(output_path, size_limit_in_bytes, info, time_start)

    if not use_deferred:
        restore()
        return result_from_output()

    def restore_when_finished() -> float | None:
        if bpy.app.is_job_running('RENDER'):
            return _RENDER_RESTORE_INTERVAL
        restore()
        return None

    bpy.app.timers.register(restore_when_finished, first_interval=_RENDER_RESTORE_INTERVAL)

    def check_is_finished() -> dict[str, Any] | None:
        if bpy.app.is_job_running('RENDER'):
            return None
        # Restore before responding, so a following call never sees the temporary settings.
        restore()
        return result_from_output()

    return check_is_finished
