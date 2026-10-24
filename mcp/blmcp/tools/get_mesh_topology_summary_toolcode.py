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

import math
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
    islands: dict[str, Any] | None = None
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


# @include_begin: _template_coords_min_max.py
_MinMax = tuple[list[float], list[float]]


def _coords_min_max(coords: Any) -> _MinMax | None:
    return ([], []) if coords else None
# @include_end


# The maximum number of elements listed per check, the `count` includes the rest.
_ITEMS_MAX = 50

# Merge distance assumed for the symmetry checks when the Mirror modifier doesn't merge.
_MERGE_DISTANCE_DEFAULT = 0.001
# Vertices closer to the mirror plane than this (relative to the mesh size
# along the axis) are reported as off-center, as they were likely meant to be on it.
_SYMMETRY_NEAR_FACTOR = 0.02

_AXES = ("X", "Y", "Z")

_SUBDIVISION_TYPES = {'SUBSURF', 'MULTIRES'}

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
_MODIFIER_TYPES_WITH_SETTINGS = {'MIRROR'} | _SUBDIVISION_TYPES


class _Plane(NamedTuple):
    """
    A mirror plane through the object's origin, from a Mirror modifier axis.
    """

    axis: int
    modifier: _Modifier
    # Vertices this close to the plane are on it: the merge distance is
    # between a vertex and its mirrored copy, twice the distance to the plane.
    tolerance: float
    # True when the cage is welded to its mirrored copy along the plane
    # before it is subdivided (the Mirror merges and comes first).
    is_joined: bool


def _round(value: float, digits: int = 6) -> float | str:
    # Non-finite floats are not valid JSON, report them as strings (e.g. "inf").
    return round(value, digits) if math.isfinite(value) else str(value)


def _round_co(co: Any) -> list[float | str]:
    return [_round(v) for v in co]


class _Items:
    """
    Convert elements to JSON items, numbered by their order in the BMesh.

    The order matches the mesh (once synced), so these are the mesh indices.
    ``BMElem.index`` is not used: updating it would renumber the edit-mesh owned by Blender,
    which scripts may rely on.
    """

    __slots__ = (
        "_verts",
        "_edges",
        "_faces",
    )

    def __init__(self, bm: _BMesh) -> None:
        self._verts = {v: i for i, v in enumerate(bm.verts)}
        self._edges = {e: i for i, e in enumerate(bm.edges)}
        self._faces = {f: i for i, f in enumerate(bm.faces)}

    def vert_index(self, v: _BMVert) -> int:
        return self._verts[v]

    def vert(self, v: _BMVert) -> dict[str, Any]:
        return {"index": self._verts[v], "co": _round_co(v.co)}

    def edge(self, e: _BMEdge) -> dict[str, Any]:
        return {"index": self._edges[e], "vertices": [self._verts[v] for v in e.verts]}

    def face(self, f: _BMFace) -> dict[str, Any]:
        return {"index": self._faces[f], "sides": len(f.verts), "center": _round_co(f.calc_center_median())}


def _listing(items: list[Any], item_fn: Any) -> dict[str, Any]:
    """
    Return the number of *items* and the first ``_ITEMS_MAX`` of them converted with *item_fn*.
    """
    return {
        "count": len(items),
        "items": [item_fn(item) for item in items[:_ITEMS_MAX]],
    }


def _enabled_modifiers(obj: _Object) -> list[_Modifier]:
    return [mod for mod in obj.modifiers if mod.show_viewport]


def _first_subdivision_index(modifiers: list[_Modifier]) -> int:
    """
    Return the index of the first subdivision modifier in *modifiers*, ``len(modifiers)`` when there is none.
    """
    return next((i for i, mod in enumerate(modifiers) if mod.type in _SUBDIVISION_TYPES), len(modifiers))


def _mirror_planes(obj: _Object) -> list[_Plane]:
    """
    Return a plane for each axis of the enabled Mirror modifiers.

    Mirror modifiers with a mirror object are skipped, their plane is not at the object's origin.
    """
    modifiers = _enabled_modifiers(obj)
    subdivision_index = _first_subdivision_index(modifiers)
    result = []
    for index, mod in enumerate(modifiers):
        if mod.type != 'MIRROR' or mod.mirror_object is not None:
            continue
        merge_distance = mod.merge_threshold if mod.use_mirror_merge else _MERGE_DISTANCE_DEFAULT
        is_joined = mod.use_mirror_merge and index < subdivision_index
        for axis in range(3):
            if mod.use_axis[axis]:
                result.append(_Plane(axis, mod, merge_distance / 2.0, is_joined))
    return result


def _is_on_plane(v: _BMVert, plane: _Plane) -> bool:
    return bool(abs(v.co[plane.axis]) <= plane.tolerance)


def _is_edge_on_plane(e: _BMEdge, plane: _Plane) -> bool:
    return all(_is_on_plane(v, plane) for v in e.verts)


def _is_edge_joined(e: _BMEdge, planes: list[_Plane]) -> bool:
    """
    Return whether *e* lies on a joined mirror plane, welding it to its mirrored copy.
    """
    return any(plane.is_joined and _is_edge_on_plane(e, plane) for plane in planes)


def _symmetry_info(bm: _BMesh, plane: _Plane, items: _Items) -> dict[str, Any]:
    """
    Check the cage against the mirror *plane*.
    """
    axis = plane.axis
    tolerance = plane.tolerance
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
        "modifier": plane.modifier.name,
        "tolerance": _round(tolerance, 7),
        "is_joined": plane.is_joined,
        "use_clip": plane.modifier.use_clip,
        "vertices_positive_side": positive,
        "vertices_negative_side": negative,
        "off_center_vertices": _listing(off_center, items.vert),
        "crossing_faces": _listing(crossing, items.face),
    }


def _pole_valence(v: _BMVert, planes: list[_Plane]) -> int | None:
    """
    Return the valence of *v* as seen on the subdivided surface, ``None`` for vertices on an open boundary.

    Boundary vertices on joined mirror planes become interior once mirrored.
    Mirroring across the planes the vertex is on makes ``2 ** n`` copies of each edge,
    an edge lying on one of those planes is shared by the copies on either side of it,
    so each edge contributes ``2 ** (number of those planes it does not lie on)``.
    """
    if not v.is_boundary:
        return len(v.link_edges)
    vert_planes = [plane for plane in planes if plane.is_joined and _is_on_plane(v, plane)]
    for e in v.link_edges:
        if e.is_boundary and not any(_is_edge_on_plane(e, plane) for plane in vert_planes):
            # An open boundary (hole), it stays open once mirrored.
            return None
    return sum(
        1 << sum(1 for plane in vert_planes if not _is_edge_on_plane(e, plane))
        for e in v.link_edges
    )


def _modifier_info(index: int, mod: _Modifier) -> dict[str, Any]:
    info: dict[str, Any] = {"index": index}
    info.update({key: getattr(mod, key) for key in _MODIFIER_COMMON})
    if mod.type in _MODIFIER_TYPES_WITH_SETTINGS:
        info["settings"] = _rna_props_to_json(mod, skip=_MODIFIER_SKIP, skip_prefixes=_MODIFIER_SKIP_PREFIXES)
    return info


def _modifier_warnings(obj: _Object) -> list[str]:
    """
    Return problems in the stack of enabled modifiers.
    """
    modifiers = _enabled_modifiers(obj)
    subdivision_index = _first_subdivision_index(modifiers)
    warnings = []
    for index, mod in enumerate(modifiers):
        if mod.type != 'MIRROR':
            continue
        if index > subdivision_index:
            warnings.append(
                "Subdivision {!r} comes before Mirror {!r}: the mirror seam is not smoothed, "
                "move the Mirror modifier above it".format(modifiers[subdivision_index].name, mod.name)
            )
        if not mod.use_mirror_merge:
            warnings.append(
                "Mirror {!r} has merge disabled: the mirror seam stays open".format(mod.name)
            )
        if not mod.use_clip:
            warnings.append(
                "Mirror {!r} has clipping disabled: vertices on the mirror plane may move off it "
                "while editing".format(mod.name)
            )
    return warnings


def _subdivision_size(
        obj: _Object,
        bm: _BMesh,
        planes: list[_Plane],
        is_edit_mode: bool,
) -> dict[str, Any] | None:
    """
    Compare the object-space size of the cage (mirrored) with the result after modifiers.

    Subdivision shrinks the shape towards the inside of the cage.
    Only compared when the size change comes from subdivision alone: the enabled modifiers are
    joined Mirror modifiers followed by subdivision, and not in Edit Mode (the evaluated mesh
    may not include unsynced edits or modifiers hidden in Edit Mode).
    """
    from array import array

    import bpy  # pylint: disable=import-error,no-name-in-module

    if is_edit_mode or not bm.verts:
        return None
    modifiers = _enabled_modifiers(obj)
    subdivision_index = _first_subdivision_index(modifiers)
    if subdivision_index == len(modifiers):
        return None
    joined = {plane.modifier.name for plane in planes if plane.is_joined}
    for index, mod in enumerate(modifiers):
        is_mirror = index < subdivision_index and mod.name in joined
        if not (is_mirror or (index >= subdivision_index and mod.type in _SUBDIVISION_TYPES)):
            return None

    # Defined by `_template_coords_min_max.py`.
    cage_min_max = _coords_min_max([c for v in bm.verts for c in v.co])
    assert cage_min_max is not None
    cage_size = [hi - lo for lo, hi in zip(*cage_min_max)]
    # The mirrored cage is symmetric around each plane.
    for axis in {plane.axis for plane in planes if plane.is_joined}:
        cage_size[axis] = 2.0 * max(abs(cage_min_max[0][axis]), abs(cage_min_max[1][axis]))

    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    if not obj_eval.is_evaluated:
        return None
    mesh = obj_eval.to_mesh()
    try:
        coords = array("f", bytes(4 * 3 * len(mesh.vertices)))
        mesh.vertices.foreach_get("co", coords)
        evaluated_min_max = _coords_min_max(coords)
    finally:
        obj_eval.to_mesh_clear()
    if evaluated_min_max is None:
        return None
    evaluated_size = [hi - lo for lo, hi in zip(*evaluated_min_max)]

    return {
        "cage": [_round(v) for v in cage_size],
        "evaluated": [_round(v) for v in evaluated_size],
        "ratio": [_round(e / c, 4) if c > 0.0 else None for c, e in zip(cage_size, evaluated_size)],
    }


def _islands(bm: _BMesh, items: _Items) -> list[dict[str, Any]]:
    """
    Return the connected parts of the mesh, the largest first.

    Each part has its number of ``vertices`` and ``faces``, its smallest vertex index
    (``first_vertex``) and the ``center`` of its object-space bounds.
    """
    seen: set[Any] = set()
    result: list[dict[str, Any]] = []
    for start in bm.verts:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        verts = []
        while stack:
            v = stack.pop()
            verts.append(v)
            for e in v.link_edges:
                other = e.other_vert(v)
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        faces = {f for v in verts for f in v.link_faces}
        result.append({
            "vertices": len(verts),
            "faces": len(faces),
            "first_vertex": min(items.vert_index(v) for v in verts),
            "center": _round_co([
                (min(v.co[axis] for v in verts) + max(v.co[axis] for v in verts)) / 2.0
                for axis in range(3)
            ]),
        })
    result.sort(key=lambda island: (-island["vertices"], island["first_vertex"]))
    return result


def _topology(obj: _Object, bm: _BMesh, is_edit_mode: bool) -> dict[str, Any]:
    planes = _mirror_planes(obj)
    items = _Items(bm)

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
            poles_5.append((v, valence))

    non_manifold = []
    boundary = []
    wire = []
    inconsistent = []
    for e in bm.edges:
        faces = len(e.link_faces)
        if faces == 0:
            wire.append(e)
        elif faces == 1:
            # Edges along a joined mirror plane are welded to their mirrored copy.
            if not _is_edge_joined(e, planes):
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
            "triangles": _listing(triangles, items.face),
            "ngons": _listing(ngons, items.face),
        },
        "poles": {
            "valence_3": _listing(poles_3, items.vert),
            "valence_5_plus": _listing(poles_5, lambda item: {**items.vert(item[0]), "valence": item[1]}),
        },
        "non_manifold_edges": _listing(non_manifold, items.edge),
        "boundary_edges": _listing(boundary, items.edge),
        "wire_edges": _listing(wire, items.edge),
        "loose_vertices": _listing(loose, items.vert),
        "inconsistent_normal_edges": _listing(inconsistent, items.edge),
        "islands": _listing(_islands(bm, items), lambda island: island),
        "creases": {
            "edges": sum(1 for e in bm.edges if e[crease_edge] > 0.0) if crease_edge is not None else 0,
            "vertices": sum(1 for v in bm.verts if v[crease_vert] > 0.0) if crease_vert is not None else 0,
        },
        "sharp_edges": sum(1 for e in bm.edges if not e.smooth),
        "symmetry": [_symmetry_info(bm, plane, items) for plane in planes],
        "subdivision_size": _subdivision_size(obj, bm, planes, is_edit_mode),
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
    is_edit_mode = mesh.is_editmode
    if is_edit_mode:
        # Read the edit-mesh so unsynced edits are included, it's only read, never modified.
        # NOTE: `BMesh.copy` is avoided as it renumbers the elements of the source.
        info = _topology(obj, bmesh.from_edit_mesh(mesh), is_edit_mode)
    else:
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            info = _topology(obj, bm, is_edit_mode)
        finally:
            bm.free()

    return Result(
        status="ok",
        name=obj.name,
        is_edit_mode=is_edit_mode,
        modifiers=[_modifier_info(index, mod) for index, mod in enumerate(obj.modifiers)],
        warnings=_modifier_warnings(obj),
        **info,
    )
