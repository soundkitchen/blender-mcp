# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline the orthographic view helpers: `_IMAGE_SIZE_LIMIT_IN_BYTES`, `_VIEW_AXES`, `_AXIS_NAMES`,
# `_axis_label`, `_screen`, `_depth`, `_ortho_matrix`, `_gpu_ensure`, `_gpu_state_save`
# & `_gpu_state_restore`.

__all__ = ()

from typing import Any

# MCP messages are limited to 1,048,576 bytes (1 MB). The image is base64-encoded
# which expands data by 4/3. Subtract 8 KiB headroom for the JSON envelope and info.
# NOTE: the same calculation as `_RENDER_IMAGE_SIZE_LIMIT_IN_BYTES`, with more headroom.
_IMAGE_SIZE_LIMIT_IN_BYTES = ((1_048_576 - 8192) * 3) // 4

# Each view maps object-space axes to the screen: `(right, up)` as `(axis, sign)`.
# The viewer looks along `right x up` reversed, matching Blender's numpad views.
_VIEW_AXES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "FRONT": ((0, 1), (2, 1)),
    "BACK": ((0, -1), (2, 1)),
    "RIGHT": ((1, 1), (2, 1)),
    "LEFT": ((1, -1), (2, 1)),
    "TOP": ((0, 1), (1, 1)),
    "BOTTOM": ((0, 1), (1, -1)),
}
_AXIS_NAMES = ("X", "Y", "Z")


def _axis_label(axis_sign: tuple[int, int]) -> str:
    """
    Return *axis_sign* as text, e.g. ``"+Y"``.
    """
    axis, sign = axis_sign
    return ("+" if sign > 0 else "-") + _AXIS_NAMES[axis]


def _screen(co: Any, axis_sign: tuple[int, int]) -> float:
    axis, sign = axis_sign
    return float(co[axis] * sign)


def _depth(co: Any, right: tuple[int, int], up: tuple[int, int]) -> float:
    """
    Return the distance of *co* towards the viewer.
    """
    # The viewer is on the side of `right x up`.
    axis = 3 - right[0] - up[0]
    # `right x up` for axes in cyclic order (X, Y, Z) points along the positive third axis.
    sign = right[1] * up[1] * (1 if (up[0] - right[0]) % 3 == 1 else -1)
    return float(co[axis] * sign)


def _ortho_matrix(
        right: tuple[int, int],
        up: tuple[int, int],
        center: tuple[float, float],
        half_size: tuple[float, float],
        depth_range: tuple[float, float],
        depth_offset: float = 0.0,
) -> Any:
    """
    Return the matrix projecting object space to normalized device coordinates.

    The rectangle around *center* (on the screen axes) with *half_size* fills the viewport,
    *depth_range* (as returned by ``_depth``) maps to the depth from far to near.
    Closer to the viewer maps to a smaller depth, *depth_offset* (NDC units) pushes back.
    """
    from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module

    depth_lo, depth_hi = depth_range
    depth_half = max((depth_hi - depth_lo) / 2.0, 1e-6)
    depth_center = (depth_lo + depth_hi) / 2.0

    rows = []
    for (axis, sign), center_axis, half in zip((right, up), center, half_size):
        row = [0.0, 0.0, 0.0, -center_axis / half]
        row[axis] = sign / half
        rows.append(row)
    depth_axis = 3 - right[0] - up[0]
    depth_sign = _depth([1.0 if i == depth_axis else 0.0 for i in range(3)], right, up)
    row = [0.0, 0.0, 0.0, depth_center / depth_half + depth_offset]
    row[depth_axis] = -depth_sign / depth_half
    rows.append(row)
    rows.append([0.0, 0.0, 0.0, 1.0])
    return Matrix(rows)


def _gpu_ensure() -> None:
    """
    Initialize the GPU module in background mode, where it's not initialized on startup.
    """
    import bpy  # pylint: disable=import-error,no-name-in-module
    import gpu  # pylint: disable=import-error,no-name-in-module

    if not bpy.app.background:
        return
    try:
        gpu.types.GPUOffScreen(4, 4).free()
    except SystemError:
        gpu.init()


def _gpu_state_save() -> tuple[Any, ...]:
    """
    Return the global GPU state changed by drawing offscreen, to restore with ``_gpu_state_restore``.
    """
    import gpu  # pylint: disable=import-error,no-name-in-module

    return (
        gpu.state.blend_get(),
        gpu.state.depth_test_get(),
        gpu.state.depth_mask_get(),
        gpu.state.viewport_get(),
    )


def _gpu_state_restore(state: tuple[Any, ...]) -> None:
    """
    Restore the global GPU state saved by ``_gpu_state_save``, the UI draws with it.
    """
    import gpu  # pylint: disable=import-error,no-name-in-module

    blend, depth_test, depth_mask, viewport = state
    gpu.state.blend_set(blend)
    gpu.state.depth_test_set(depth_test)
    gpu.state.depth_mask_set(depth_mask)
    gpu.state.viewport_set(*viewport)
