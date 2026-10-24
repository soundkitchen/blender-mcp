# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for checking whether a mesh is suitable as a subdivision surface cage.
"""

__all__ = (
    "Params",
    "Result",
    "main",
)

from typing import Any, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_BMesh = Any
_BMVert = Any
_BMEdge = Any
_BMFace = Any
_Modifier = Any
_Object = Any


class Params(NamedTuple):
    name: str


class Result(NamedTuple):
    status: str
    name: str | None = None
    is_edit_mode: bool | None = None
    counts: dict[str, int] | None = None
    faces: dict[str, Any] | None = None
    poles: dict[str, Any] | None = None
    non_manifold_edges: dict[str, Any] | None = None
    boundary_edges: dict[str, Any] | None = None
    wire_edges: dict[str, Any] | None = None
    loose_vertices: dict[str, Any] | None = None
    inconsistent_normal_edges: dict[str, Any] | None = None
    creases: dict[str, int] | None = None
    sharp_edges: int | None = None
    symmetry: list[dict[str, Any]] | None = None
    modifiers: list[dict[str, Any]] | None = None
    subdivision_size: dict[str, Any] | None = None
    warnings: list[str] | None = None
    message: str | None = None


# @include_begin: _template_rna_props_to_json.py
def _rna_value_to_json(value: Any) -> Any:
    return value


def _rna_props_to_json(
        struct: Any,
        skip: tuple[str, ...] = (),
        skip_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {}
# @include_end


# The maximum number of elements listed per check, the `count` includes the rest.
_ITEMS_MAX = 50

# Merge distance used for the symmetry checks when the Mirror modifier doesn't merge.
_SYMMETRY_TOLERANCE_DEFAULT = 0.001
# Vertices closer to the mirror plane than this (relative to the mesh size
# along the axis) are reported as off-center, as they were likely meant to be on it.
_SYMMETRY_NEAR_FACTOR = 0.02

_AXES = ("X", "Y", "Z")

# Reported at the top level of each modifier (not under `settings`).
_MODIFIER_COMMON = (
    "name",
    "type",
    "show_viewport",
    "show_render",
)
# UI state, not relevant to the result.
_MODIFIER_SKIP = _MODIFIER_COMMON + (
    "is_active",
    "show_expanded",
    "show_in_editmode",
    "show_on_cage",
    "use_pin_to_last",
)
_MODIFIER_SKIP_PREFIXES = ("open_",)
# Modifiers that matter for a subdivision cage, others are listed by name and type only.
_MODIFIER_TYPES_WITH_SETTINGS = {'MIRROR', 'SUBSURF', 'MULTIRES'}


def _round_co(co: Any) -> list[float]:
    return [round(v, 6) for v in co]


def _vert_item(v: _BMVert) -> dict[str, Any]:
    return {"index": v.index, "co": _round_co(v.co)}


def _edge_item(e: _BMEdge) -> dict[str, Any]:
    return {"index": e.index, "vertices": [v.index for v in e.verts]}


def _face_item(f: _BMFace) -> dict[str, Any]:
    return {"index": f.index, "sides": len(f.verts), "center": _round_co(f.calc_center_median())}


def _listing(items: list[Any], item_fn: Any) -> dict[str, Any]:
    """
    Return the number of *items* and the first ``_ITEMS_MAX`` of them converted with *item_fn*.
    """
    return {
        "count": len(items),
        "items": [item_fn(item) for item in items[:_ITEMS_MAX]],
    }


def _mirror_planes(obj: _Object) -> list[tuple[int, _Modifier]]:
    """
    Return ``(axis, modifier)`` for each enabled mirror axis.

    Only Mirror modifiers without a mirror object are used,
    the plane is then at the object's origin.
    """
    result = []
    for mod in obj.modifiers:
        if mod.type != 'MIRROR' or not mod.show_viewport or mod.mirror_object is not None:
            continue
        for axis in range(3):
            if mod.use_axis[axis]:
                result.append((axis, mod))
    return result


def _symmetry_tolerance(mod: _Modifier) -> float:
    return mod.merge_threshold if mod.use_mirror_merge else _SYMMETRY_TOLERANCE_DEFAULT


def _is_edge_on_planes(e: _BMEdge, planes: list[tuple[int, float]]) -> bool:
    """
    Return whether *e* lies on a mirror plane, joining it to its mirrored copy.
    """
    return any(all(abs(v.co[axis]) <= tolerance for v in e.verts) for axis, tolerance in planes)


def _symmetry_info(bm: _BMesh, axis: int, mod: _Modifier) -> dict[str, Any]:
    """
    Check the cage against the mirror plane on *axis*.
    """
    tolerance = _symmetry_tolerance(mod)
    coords = [v.co[axis] for v in bm.verts]
    size = (max(coords) - min(coords)) if coords else 0.0
    near = max(tolerance * 10.0, size * _SYMMETRY_NEAR_FACTOR)

    off_center = [v for v in bm.verts if tolerance < abs(v.co[axis]) <= near]
    positive = sum(1 for c in coords if c > tolerance)
    negative = sum(1 for c in coords if c < -tolerance)
    # Faces with vertices on both sides overlap their mirrored copy.
    crossing = [
        f for f in bm.faces
        if any(v.co[axis] > tolerance for v in f.verts) and any(v.co[axis] < -tolerance for v in f.verts)
    ]
    return {
        "axis": _AXES[axis],
        "modifier": mod.name,
        "tolerance": round(tolerance, 6),
        "use_clip": mod.use_clip,
        "vertices_positive_side": positive,
        "vertices_negative_side": negative,
        "off_center_vertices": _listing(off_center, _vert_item),
        "crossing_faces": _listing(crossing, _face_item),
    }


def _pole_valence(v: _BMVert, planes: list[tuple[int, float]]) -> int | None:
    """
    Return the valence of *v* as seen on the subdivided surface, ``None`` for vertices on an open boundary.

    Boundary vertices on a mirror plane become interior once mirrored:
    the edges along the plane are kept and the others are doubled.
    """
    valence = len(v.link_edges)
    if not v.is_boundary:
        return valence
    boundary = [e for e in v.link_edges if e.is_boundary]
    if not all(_is_edge_on_planes(e, planes) for e in boundary):
        # An open boundary (hole), it stays open once mirrored.
        return None
    return len(boundary) + 2 * (valence - len(boundary))


def _modifier_info(index: int, mod: _Modifier) -> dict[str, Any]:
    info: dict[str, Any] = {"index": index}
    info.update({key: getattr(mod, key) for key in _MODIFIER_COMMON})
    if mod.type in _MODIFIER_TYPES_WITH_SETTINGS:
        info["settings"] = _rna_props_to_json(mod, skip=_MODIFIER_SKIP, skip_prefixes=_MODIFIER_SKIP_PREFIXES)
    return info


def _modifier_warnings(obj: _Object) -> list[str]:
    warnings = []
    mirror = None
    subsurf = None
    for mod in obj.modifiers:
        if mod.type == 'MIRROR' and mirror is None:
            mirror = mod
        elif mod.type in {'SUBSURF', 'MULTIRES'} and subsurf is None:
            subsurf = mod
    if mirror is not None and subsurf is not None:
        if list(obj.modifiers).index(subsurf) < list(obj.modifiers).index(mirror):
            warnings.append(
                "Subdivision {!r} comes before Mirror {!r}: the mirror seam is not smoothed, "
                "move the Mirror modifier above it".format(subsurf.name, mirror.name)
            )
    if mirror is not None and not mirror.use_clip:
        warnings.append(
            "Mirror {!r} has clipping disabled: vertices on the mirror plane may move off it "
            "while editing".format(mirror.name)
        )
    return warnings


def _local_size(coords: list[Any]) -> list[float]:
    return [max(c[axis] for c in coords) - min(c[axis] for c in coords) for axis in range(3)]


def _subdivision_size(obj: _Object, bm: _BMesh, planes: list[tuple[int, float]]) -> dict[str, Any] | None:
    """
    Compare the object-space size of the cage (mirrored) with the result after modifiers.

    Subdivision shrinks the shape towards the inside of the cage.
    """
    import bpy  # pylint: disable=import-error,no-name-in-module

    if not any(mod.type in {'SUBSURF', 'MULTIRES'} and mod.show_viewport for mod in obj.modifiers):
        return None
    if not bm.verts:
        return None

    cage = [list(v.co) for v in bm.verts]
    # Include the mirrored copies, so the size matches the evaluated result.
    for axis, _tolerance in planes:
        cage.extend([[-c if i == axis else c for i, c in enumerate(co)] for co in cage])
    cage_size = _local_size(cage)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    if not obj_eval.is_evaluated:
        return None
    mesh = obj_eval.to_mesh()
    try:
        if not mesh.vertices:
            return None
        evaluated_size = _local_size([v.co for v in mesh.vertices])
    finally:
        obj_eval.to_mesh_clear()

    return {
        "cage": [round(v, 6) for v in cage_size],
        "evaluated": [round(v, 6) for v in evaluated_size],
        "ratio": [round(e / c, 4) if c > 0.0 else None for c, e in zip(cage_size, evaluated_size)],
    }


def _topology(obj: _Object, bm: _BMesh) -> dict[str, Any]:
    planes = [(axis, _symmetry_tolerance(mod)) for axis, mod in _mirror_planes(obj)]

    quads = 0
    triangles = []
    ngons = []
    for f in bm.faces:
        sides = len(f.verts)
        if sides == 4:
            quads += 1
        elif sides == 3:
            triangles.append(f)
        else:
            ngons.append(f)

    poles_3 = []
    poles_5 = []
    loose = []
    for v in bm.verts:
        if not v.link_edges:
            loose.append(v)
            continue
        # Vertices only used by wire edges are reported with the edges.
        if not v.link_faces:
            continue
        valence = _pole_valence(v, planes)
        if valence == 3:
            poles_3.append(v)
        elif valence is not None and valence >= 5:
            poles_5.append(v)

    non_manifold = []
    boundary = []
    wire = []
    inconsistent = []
    for e in bm.edges:
        faces = len(e.link_faces)
        if faces == 0:
            wire.append(e)
        elif faces == 1:
            # Edges along a mirror plane are joined to their mirrored copy.
            if not _is_edge_on_planes(e, planes):
                boundary.append(e)
        elif faces > 2:
            non_manifold.append(e)
        elif not e.is_contiguous:
            inconsistent.append(e)

    crease_edge = bm.edges.layers.float.get("crease_edge")
    crease_vert = bm.verts.layers.float.get("crease_vert")

    return {
        "counts": {"vertices": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces)},
        "faces": {
            "quads": quads,
            "triangles": _listing(triangles, _face_item),
            "ngons": _listing(ngons, _face_item),
        },
        "poles": {
            "valence_3": _listing(poles_3, _vert_item),
            "valence_5_plus": {
                "count": len(poles_5),
                "items": [
                    {**_vert_item(v), "valence": _pole_valence(v, planes)}
                    for v in poles_5[:_ITEMS_MAX]
                ],
            },
        },
        "non_manifold_edges": _listing(non_manifold, _edge_item),
        "boundary_edges": _listing(boundary, _edge_item),
        "wire_edges": _listing(wire, _edge_item),
        "loose_vertices": _listing(loose, _vert_item),
        "inconsistent_normal_edges": _listing(inconsistent, _edge_item),
        "creases": {
            "edges": sum(1 for e in bm.edges if e[crease_edge] > 0.0) if crease_edge is not None else 0,
            "vertices": sum(1 for v in bm.verts if v[crease_vert] > 0.0) if crease_vert is not None else 0,
        },
        "sharp_edges": sum(1 for e in bm.edges if not e.smooth),
        "symmetry": [_symmetry_info(bm, axis, mod) for axis, mod in _mirror_planes(obj)],
        "subdivision_size": _subdivision_size(obj, bm, planes),
    }


def main(params: Params) -> Result:
    import bmesh  # pylint: disable=import-error,no-name-in-module
    import bpy  # pylint: disable=import-error,no-name-in-module

    obj = bpy.data.objects.get(params.name)
    if obj is None:
        return Result(status="error", message="Object {!r} not found".format(params.name))
    if obj.type != 'MESH':
        return Result(
            status="error",
            message="Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type),
        )

    mesh = obj.data
    # In Edit Mode, read the edit-mesh so unsynced edits are included.
    is_edit_mode = mesh.is_editmode
    if is_edit_mode:
        bm = bmesh.from_edit_mesh(mesh)
    else:
        bm = bmesh.new()
        bm.from_mesh(mesh)
    try:
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        info = _topology(obj, bm)
    finally:
        # The edit-mesh is owned by Blender.
        if not is_edit_mode:
            bm.free()

    return Result(
        status="ok",
        name=obj.name,
        is_edit_mode=is_edit_mode,
        modifiers=[_modifier_info(index, mod) for index, mod in enumerate(obj.modifiers)],
        warnings=_modifier_warnings(obj),
        **info,
    )
