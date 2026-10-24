# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline `_MinMax` & `_coords_min_max`.

__all__ = ()

from typing import Any

# Axis-aligned bounds as `(min, max)`, each a list of 3 floats.
_MinMax = tuple[list[float], list[float]]


def _coords_min_max(coords: Any) -> _MinMax | None:
    """
    Return the bounds of the flat ``x, y, z, ...`` sequence *coords*.
    """
    if not coords:
        return None
    return (
        [min(coords[axis::3]) for axis in range(3)],
        [max(coords[axis::3]) for axis in range(3)],
    )
