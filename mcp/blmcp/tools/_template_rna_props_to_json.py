# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Inline `_rna_value_to_json` & `_rna_props_to_json`.

__all__ = ()

from typing import Any


def _rna_value_to_json(value: Any) -> Any:
    """
    Convert an RNA property value to a JSON-serializable value.

    Data-blocks are reported by name, arrays and math types as (nested) lists,
    enum flags as sorted lists, non-finite floats as strings (e.g. ``"inf"``,
    not valid JSON otherwise). Returns ``None`` for other structs.
    """
    import math

    import bpy  # pylint: disable=import-error,no-name-in-module

    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, bpy.types.ID):
        return value.name
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, bpy.types.bpy_struct):
        return None
    try:
        # `bpy_prop_array` and `mathutils` types (including matrices).
        return [_rna_value_to_json(item) for item in value]
    except TypeError:
        return None


def _rna_struct_is_id(rna_struct: Any) -> bool:
    while rna_struct is not None:
        if rna_struct.identifier == "ID":
            return True
        rna_struct = rna_struct.base
    return False


def _rna_props_to_json(
        struct: Any,
        skip: tuple[str, ...] = (),
        skip_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """
    Return the editable properties of *struct* as a JSON-serializable dict.

    Read-only properties, collections and pointers to non data-block structs
    are omitted, pointers to data-blocks are reported by name.
    """
    result: dict[str, Any] = {}
    for prop in struct.bl_rna.properties:
        identifier = prop.identifier
        if identifier == "rna_type" or identifier in skip or identifier.startswith(skip_prefixes):
            continue
        if prop.is_readonly or prop.type == 'COLLECTION':
            continue
        if prop.type == 'POINTER' and not _rna_struct_is_id(prop.fixed_type):
            continue
        value = getattr(struct, identifier)
        result[identifier] = _rna_value_to_json(value)
    return result
