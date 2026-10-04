# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline `_render_thumbnail_overrides`.

__all__ = ()

from typing import Any

# Thumbnail render settings. Small resolution and low samples
# for a fast preview that is still useful for visual inspection.
# The longest dimension is clamped to this value, preserving aspect ratio.
_THUMB_DIMS_MAX = 320
_THUMB_SIMPLIFY_SUBDIV = 1
_THUMB_CYCLES_SAMPLES = 16
_THUMB_EEVEE_SAMPLES = 16


def _render_thumbnail_overrides(scene: Any) -> list[tuple[object, dict[str, object]]]:
    """
    Return the temporary settings for a low-quality thumbnail render of *scene*.

    The result is a list of ``(obj, {attr: value, ...})`` pairs,
    intended to be passed to ``_backup_attrs_and_assign_multi``.
    """
    rd = scene.render

    # Compute thumbnail resolution from originals, preserving aspect ratio.
    res_x = rd.resolution_x
    res_y = rd.resolution_y
    if res_x >= res_y:
        thumb_x = _THUMB_DIMS_MAX
        thumb_y = max(int(res_y * _THUMB_DIMS_MAX / res_x), 1)
    else:
        thumb_y = _THUMB_DIMS_MAX
        thumb_x = max(int(res_x * _THUMB_DIMS_MAX / res_y), 1)

    obj_attrs: list[tuple[object, dict[str, object]]] = [
        (rd, {
            "resolution_x": thumb_x,
            "resolution_y": thumb_y,
            "resolution_percentage": 100,
            "use_simplify": True,
            "simplify_subdivision_render": _THUMB_SIMPLIFY_SUBDIV,
        }),
    ]
    if rd.engine == "CYCLES":
        obj_attrs.append((scene.cycles, {"samples": _THUMB_CYCLES_SAMPLES}))
    elif rd.engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        # NOTE: keep EEVEE engine IDs in sync with `get_blendfile_summary_usage_guess_toolcode.py`.
        obj_attrs.append((scene.eevee, {"taa_render_samples": _THUMB_EEVEE_SAMPLES}))
    return obj_attrs
