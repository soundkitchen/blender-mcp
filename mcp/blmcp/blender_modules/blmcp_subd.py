# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Helpers for modeling subdivision surface cages with ``bmesh``.

They don't depend on the 3D view, the selection or the mode (unlike ``bpy.ops.mesh``
operators such as ``loopcut_slide``), so they work from ``execute_blender_code``.

The cage is the right half (``X >= 0``) of a mesh, open on the mirror plane ``X = 0``,
under a Mirror modifier followed by a Subdivision Surface modifier.

Example::

    import bpy
    import blmcp_subd as subd

    obj = bpy.data.objects["Character"]
    with subd.edit_mesh(obj) as bm:
        cap = subd.extrude(bm, [bm.faces[12]]).faces[0]
        for v in cap.verts:
            v.co.z += 0.1
        (loop,) = subd.insert_loop(bm, bm.edges[40])
        subd.scale_loop(loop, (1.2, 1.0, 1.2))
        result = {"loop": [v.index for v in loop]}
"""

__all__ = (
    "Extruded",
    "bridge_loops",
    "edge_ring",
    "edit_mesh",
    "extrude",
    "insert_loop",
    "make_half",
    "move_smooth",
    "scale_loop",
    "set_crease",
    "setup_mirror_subsurf",
    "symmetrize",
)

import contextlib
import heapq
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, NamedTuple

import bmesh  # pylint: disable=import-error
from mathutils import Vector  # pylint: disable=import-error
from mathutils.kdtree import KDTree  # pylint: disable=import-error

# Placeholder types for Blender Python objects (no stubs available).
_BMesh = Any
_BMVert = Any
_BMEdge = Any
_BMFace = Any
_Object = Any

# Vertices closer than this to `X = 0` are on the mirror plane.
_MIRROR_PLANE_TOLERANCE = 1e-6


def _on_mirror_plane(v: _BMVert) -> bool:
    return bool(abs(v.co.x) < _MIRROR_PLANE_TOLERANCE)


@contextlib.contextmanager
def edit_mesh(obj: _Object) -> Iterator[_BMesh]:
    """
    Give a ``bmesh`` of the mesh object *obj* and write it back when the block ends.

    Works in Object Mode and Edit Mode. The indices and lookup tables (``bm.verts[i]``)
    are ready at the start, the helpers of this module update them after changing the mesh.
    In Object Mode the ``bmesh`` is freed at the end of the block: read what you need
    (e.g. ``v.index``) inside it. An exception leaves the mesh unchanged in Object Mode,
    in Edit Mode the changes made before it stay in the edit-mesh.
    The edge crease attribute is created when missing (see :func:`set_crease`).
    """
    if obj.type != 'MESH':
        raise TypeError("Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    mesh = obj.data
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        try:
            _crease_layer(bm)
            _update_indices(bm)
            yield bm
        finally:
            # Also after an exception: the edit-mesh keeps the changes made before it,
            # its caches must match them.
            bmesh.update_edit_mesh(mesh)
        return
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        _crease_layer(bm)
        _update_indices(bm)
        yield bm
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()


def _update_indices(bm: _BMesh) -> None:
    # The indices match the mesh after writing it back (`to_mesh` keeps the order).
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()


class Extruded(NamedTuple):
    """
    The result of :func:`extrude`.
    """
    # The new faces at the end of the extrusion (the caps), in the order of the given faces.
    faces: list[_BMFace]
    # Each new vertex mapped to the vertex it was extruded from. Sources inside the region
    # (not on its border) are removed with the original faces: only use them as keys.
    source: dict[_BMVert, _BMVert]


def extrude(bm: _BMesh, faces: Iterable[_BMFace]) -> Extruded:
    """
    Extrude the connected *faces* as one region and remove the original faces.

    The new faces are where the old ones were: move their vertices afterwards.
    Faces left on the mirror plane (from extruding a face with an edge on ``X = 0``)
    and the edges left without faces are removed, they would be inside the mirrored mesh.
    Use ``source`` to carry information from the old vertices to the new ones, e.g. a
    role name: ``{v: role[src] for v, src in result.source.items()}``.
    Raises ``ValueError`` (leaving the mesh as it was) when a vertex inside the region
    overlaps another vertex of the faces: merge them first.
    The indices and lookup tables of *bm* are updated.
    """
    faces = list(faces)
    if not faces:
        raise ValueError("No faces to extrude")
    old_verts = {v for f in faces for v in f.verts}
    old_edges = {e for f in faces for e in f.edges}
    # The new vertices are created at the position of their source. Inner vertices (without
    # an edge to their source) are matched by position: it must be unique among the sources.
    # (A temporary attribute would be exact, but adding one invalidates the caller's vertices.)
    old_by_co: dict[tuple[float, ...], list[_BMVert]] = {}
    for v in old_verts:
        old_by_co.setdefault(v.co.to_tuple(), []).append(v)

    res = bmesh.ops.extrude_face_region(bm, geom=faces)
    new_faces = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
    new_verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
    source = {}
    for v in new_verts:
        src = next((e.other_vert(v) for e in v.link_edges if e.other_vert(v) in old_verts), None)
        if src is None:
            candidates = old_by_co.get(v.co.to_tuple(), [])
            if len(candidates) != 1:
                # Undo the extrusion: delete the new geometry, the original faces are still there.
                bmesh.ops.delete(bm, geom=new_verts, context='VERTS')
                _update_indices(bm)
                raise ValueError(
                    "Vertices {:s} of the faces to extrude overlap, merge them first".format(
                        str(sorted(c.index for c in candidates)))
                )
            src = candidates[0]
        source[v] = src
    # Sort the new faces like the given faces, matched by their source vertices.
    order = {frozenset(f.verts): i for i, f in enumerate(faces)}
    new_faces.sort(key=lambda f: order[frozenset(source[v] for v in f.verts)])

    bmesh.ops.delete(bm, geom=faces, context='FACES')
    new_face_set = set(new_faces)
    sides = {f for v in new_verts for f in v.link_faces if f not in new_face_set}
    on_plane = [f for f in sides if all(_on_mirror_plane(v) for v in f.verts)]
    for f in on_plane:
        old_edges.update(f.edges)
    bmesh.ops.delete(bm, geom=on_plane, context='FACES_ONLY')
    bmesh.ops.delete(bm, geom=[e for e in old_edges if e.is_valid and not e.link_faces], context='EDGES')
    _update_indices(bm)
    return Extruded(new_faces, source)


def edge_ring(edge: _BMEdge) -> list[_BMEdge]:
    """
    Return the edge ring through *edge*: the edges across a row of quads, in order.

    The ring stops at open edges (e.g. on the mirror plane) and faces that aren't quads.
    When the ring is closed, its first edge is *edge*.
    """
    def walk(loop: Any) -> tuple[list[_BMEdge], bool]:
        edges: list[_BMEdge] = []
        while len(loop.face.verts) == 4:
            opposite = loop.link_loop_next.link_loop_next
            if opposite.edge is edge:
                return edges, True
            edges.append(opposite.edge)
            radial = opposite.link_loop_radial_next
            # Stop at open edges and edges with more than 2 faces.
            if radial is opposite or len(opposite.edge.link_faces) != 2:
                break
            loop = radial
        return edges, False

    loops = list(edge.link_loops)
    if not loops:
        return [edge]
    forward, closed = walk(loops[0])
    if closed or len(loops) != 2:
        return [edge, *forward]
    backward, _closed = walk(loops[1])
    return [*reversed(backward), edge, *forward]


def insert_loop(bm: _BMesh, edge: _BMEdge, cuts: int = 1) -> list[list[_BMVert]]:
    """
    Insert *cuts* edge loops across the edge ring through *edge* (see :func:`edge_ring`).

    The new loops are evenly spaced and straight (subdivision smooths them).
    Return the new vertices, one list per loop, from the ``edge.verts[0]`` side to the
    ``edge.verts[1]`` side. Each list follows the ring, one vertex per ring edge.
    The indices and lookup tables of *bm* are updated.
    """
    if cuts < 1:
        raise ValueError("cuts must be 1 or more, not {:d}".format(cuts))
    ring = edge_ring(edge)
    if len(ring) == 1:
        raise ValueError("The edge has no quad to cut across")
    # The end of each ring edge on the side of `edge.verts[0]`, found by stepping from ring
    # edge to ring edge along the side edges of the quads between them.
    i = ring.index(edge)
    starts = {edge: edge.verts[0]}
    for prev, cur in [*zip(ring[i:], ring[i + 1:]), *zip(ring[i:0:-1], ring[i - 1::-1])]:
        starts[cur] = _end_beside(prev, cur, starts[prev])
    # The ring edges are replaced by the subdivision: keep their ends.
    ends = [(starts[e], e.other_vert(starts[e])) for e in ring]
    for start, end in ends:
        if (end.co - start.co).length_squared == 0.0:
            raise ValueError("The ring edge between vertices {:d} and {:d} has no length".format(
                start.index, end.index))
    # The vertices next to the ends before the cut: the walk below only steps on new ones.
    old_near = {w for pair in ends for v in pair for e in v.link_edges for w in e.verts}

    bmesh.ops.subdivide_edgering(bm, edges=ring, cuts=cuts)
    # Each ring edge is now a chain of edges: walk it from its start to its end.
    columns = []
    for start, end in ends:
        column = []
        prev, v = None, start
        while True:
            v_next = next((
                w for w in (e2.other_vert(v) for e2 in v.link_edges)
                if w is not prev and (w is end or w not in old_near) and _is_between(w, start, end)
            ), None)
            if v_next is None:
                raise RuntimeError("Could not follow the cut edge between vertices {:d} and {:d}".format(
                    start.index, end.index))
            if v_next is end:
                break
            column.append(v_next)
            prev, v = v, v_next
        columns.append(column)
    _update_indices(bm)
    return [list(row) for row in zip(*columns)]


def _end_beside(prev: _BMEdge, cur: _BMEdge, prev_start: _BMVert) -> _BMVert:
    # The end of `cur` next to `prev_start` in the quad between `prev` and `cur`.
    face = next((f for f in prev.link_faces if cur in f.edges), None)
    if face is not None:
        for loop in face.loops:
            if loop.vert is prev_start:
                for v in (loop.link_loop_next.vert, loop.link_loop_prev.vert):
                    if v in cur.verts:
                        return v
    raise RuntimeError("The edges of the ring are not joined by quads")


def _is_between(v: _BMVert, start: _BMVert, end: _BMVert) -> bool:
    # Whether `v` lies on the segment from `start` to `end`.
    seg = end.co - start.co
    length_sq = seg.length_squared
    if length_sq == 0.0:
        return False
    t = (v.co - start.co).dot(seg) / length_sq
    closest = start.co + seg * t
    return bool(-1e-6 <= t <= 1.0 + 1e-6 and (v.co - closest).length <= 1e-5 * (1.0 + seg.length))


def bridge_loops(bm: _BMesh, loop_a: Sequence[_BMVert], loop_b: Sequence[_BMVert]) -> list[_BMFace]:
    """
    Join two rows of vertices with quads and return the new faces.

    *loop_a* and *loop_b* are vertices joined by edges, in order, with the same number of
    vertices. A loop is closed when an edge joins its last vertex to its first.
    Open loops ending on the mirror plane bridge the right half of a ring.
    The new faces are oriented like the existing faces next to them (or outwards, when
    there are none), other faces are left as they are.
    The indices and lookup tables of *bm* are updated.
    """
    if len(loop_a) != len(loop_b):
        raise ValueError("The loops have {:d} and {:d} vertices".format(len(loop_a), len(loop_b)))
    if len(loop_a) < 2:
        raise ValueError("A loop needs 2 or more vertices")
    if len(set(loop_a) | set(loop_b)) != len(loop_a) * 2:
        raise ValueError("The loops use a vertex twice")
    closed = [len(loop) > 2 and bm.edges.get((loop[-1], loop[0])) is not None for loop in (loop_a, loop_b)]
    if closed[0] != closed[1]:
        raise ValueError("One loop is closed and the other is open")
    faces = []
    for i in range(len(loop_a) if closed[0] else len(loop_a) - 1):
        j = (i + 1) % len(loop_a)
        quad = (loop_a[i], loop_a[j], loop_b[j], loop_b[i])
        if bm.faces.get(quad) is not None:
            raise ValueError("A face already joins vertices {:s}".format(str([v.index for v in quad])))
        faces.append(bm.faces.new(quad))
    # The new faces wind the same way. Across an edge shared with an existing face, the
    # two faces must go through the edge in opposite directions, flip them otherwise.
    new_faces = set(faces)
    neighbor = next((
        (loop, other) for f in faces for loop in f.loops for other in loop.edge.link_loops
        if other.face not in new_faces
    ), None)
    if neighbor is None:
        bmesh.ops.recalc_face_normals(bm, faces=faces)
    elif neighbor[0].vert is neighbor[1].vert:
        for f in faces:
            f.normal_flip()
    for f in faces:
        f.normal_update()
    _update_indices(bm)
    return faces


def setup_mirror_subsurf(obj: _Object, levels: int = 2) -> dict[str, Any]:
    """
    Set up the modifiers of a half cage: Mirror on ``X`` (merge and clipping) then Subdivision Surface.

    Existing modifiers are reused (the first Mirror without a mirror object and the first
    Subdivision Surface), created when missing and moved so Mirror comes before Subdivision
    Surface. The ``Y`` and ``Z`` axes of a reused Mirror and other modifiers are left as they are.
    Return the names of the modifiers, in their final order.
    """
    if obj.type != 'MESH':
        raise TypeError("Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    mods = obj.modifiers
    mirror = next((m for m in mods if m.type == 'MIRROR' and m.mirror_object is None), None)
    if mirror is None:
        mirror = mods.new("Mirror", 'MIRROR')  # Only `X` is enabled for a new one.
    mirror.use_axis[0] = True
    mirror.use_mirror_merge = True
    mirror.use_clip = True
    subsurf = next((m for m in mods if m.type == 'SUBSURF'), None)
    if subsurf is None:
        subsurf = mods.new("Subdivision", 'SUBSURF')
    subsurf.levels = levels
    mirror_index = mods.find(mirror.name)
    subsurf_index = mods.find(subsurf.name)
    if mirror_index > subsurf_index:
        mods.move(mirror_index, subsurf_index)
    return {"modifiers": [m.name for m in mods], "mirror": mirror.name, "subdivision": subsurf.name}


def scale_loop(
        verts: Iterable[_BMVert],
        scale: float | Sequence[float],
        center: Sequence[float] | None = None,
        mirror: bool = True,
) -> tuple[float, float, float]:
    """
    Scale *verts* (e.g. a loop from :func:`insert_loop`) around *center*, per axis.

    *scale* is a factor or ``(x, y, z)`` factors. *center* defaults to the average of the
    vertices. With *mirror* (for a half cage under a Mirror modifier), when some of them are
    on the mirror plane (the right half of a ring, e.g. a waist loop), the center's ``X`` is 0
    (the center of the whole ring) and the vertices on the plane stay on it. Pass
    ``mirror=False`` for a whole mesh. Return the center used.
    """
    verts = list(verts)
    if not verts:
        raise ValueError("No vertices to scale")
    factors = (scale, scale, scale) if isinstance(scale, (int, float)) else tuple(scale)
    if len(factors) != 3:
        raise ValueError("scale must be a number or 3 numbers (X, Y, Z)")
    if center is None:
        middle = sum((v.co for v in verts), Vector()) / len(verts)
        if mirror and any(_on_mirror_plane(v) for v in verts):
            middle.x = 0.0
    else:
        middle = Vector(center)
    for v in verts:
        on_plane = mirror and _on_mirror_plane(v)
        v.co = Vector(m + (c - m) * f for c, m, f in zip(v.co, middle, factors))
        if on_plane:
            v.co.x = 0.0
    return (middle.x, middle.y, middle.z)


# The weight of a vertex at a distance `d` from the moved ones, with `t = 1 - d / radius`.
# Matches Blender's proportional editing falloffs.
_FALLOFFS: dict[str, Callable[[float], float]] = {
    'SMOOTH': lambda t: 3.0 * t * t - 2.0 * t * t * t,
    'SPHERE': lambda t: math.sqrt(max(0.0, 2.0 * t - t * t)),
    'ROOT': lambda t: math.sqrt(t),
    'SHARP': lambda t: t * t,
    'LINEAR': lambda t: t,
    'CONSTANT': lambda t: 1.0,
}


def move_smooth(
        bm: _BMesh,
        verts: Iterable[_BMVert],
        offset: Sequence[float],
        radius: float,
        falloff: str = 'SMOOTH',
        connected: bool = True,
        mirror: bool = True,
) -> dict[_BMVert, float]:
    """
    Move *verts* by *offset*, the vertices around them follow less and less up to *radius*.

    Like proportional editing: a vertex at a distance ``d`` from the nearest moved vertex
    moves by ``offset * weight``, the weight going from 1 to 0 at *radius* with *falloff*
    (``'SMOOTH'``, ``'SPHERE'``, ``'ROOT'``, ``'SHARP'``, ``'LINEAR'``, ``'CONSTANT'``).
    With *connected*, the distance is measured along the edges, so only vertices joined to
    the moved ones follow (e.g. moving an arm leaves the nearby torso in place). Otherwise
    it is the straight distance, any vertex in *radius* follows.
    With *mirror* (for a half cage under a Mirror modifier), vertices on the mirror plane stay
    on it, pass ``mirror=False`` for a whole mesh. Return the weight of each vertex that moved.
    """
    verts = list(verts)
    if not verts:
        raise ValueError("No vertices to move")
    if falloff not in _FALLOFFS:
        raise ValueError("falloff must be one of {:s}, not {!r}".format(", ".join(_FALLOFFS), falloff))
    if radius < 0.0:
        raise ValueError("radius must be 0 or more")
    move = Vector(offset)
    distances = _distances_along_edges(verts, radius) if connected else _distances_straight(bm, verts, radius)
    weights = {}
    for v, d in distances.items():
        weights[v] = 1.0 if d == 0.0 else _FALLOFFS[falloff](1.0 - d / radius)
    for v, w in weights.items():
        on_plane = mirror and _on_mirror_plane(v)
        v.co += move * w
        if on_plane:
            v.co.x = 0.0
    return weights


def _distances_along_edges(verts: list[_BMVert], radius: float) -> dict[_BMVert, float]:
    # Shortest distances along the edges from `verts`, below `radius` (Dijkstra).
    distances = {v: 0.0 for v in verts}
    queue = [(0.0, i, v) for i, v in enumerate(verts)]
    counter = len(queue)
    while queue:
        d, _i, v = heapq.heappop(queue)
        if d > distances[v]:
            continue
        for e in v.link_edges:
            w = e.other_vert(v)
            dw = d + e.calc_length()
            if dw < radius and dw < distances.get(w, math.inf):
                distances[w] = dw
                heapq.heappush(queue, (dw, counter, w))
                counter += 1
    return distances


def _distances_straight(bm: _BMesh, verts: list[_BMVert], radius: float) -> dict[_BMVert, float]:
    # Straight distances to the nearest of `verts`, below `radius`.
    distances = {v: 0.0 for v in verts}
    tree = KDTree(len(verts))
    for i, u in enumerate(verts):
        tree.insert(u.co, i)
    tree.balance()
    for v in bm.verts:
        if v in distances:
            continue
        _co, _index, d = tree.find(v.co)
        if d < radius:
            distances[v] = d
    return distances


def _crease_layer(bm: _BMesh) -> Any:
    # The edge crease attribute, created when missing.
    layer = bm.edges.layers.float.get("crease_edge")
    if layer is None:
        layer = bm.edges.layers.float.new("crease_edge")
    return layer


def set_crease(bm: _BMesh, edges: Iterable[_BMEdge], value: float) -> None:
    """
    Set the subdivision crease of *edges*: 0 is smooth, 1 is a sharp edge.

    Creases are kept by Subdivision Surface modifiers with ``use_creases`` (the default).
    A ``bmesh`` from :func:`edit_mesh` already has the crease attribute. On another one
    without it, the attribute is created here, which invalidates the edges held by the
    caller (get them again, e.g. by index): *edges* themselves are handled.
    """
    if not 0.0 <= value <= 1.0:
        raise ValueError("value must be from 0 to 1, not {!r}".format(value))
    layer = bm.edges.layers.float.get("crease_edge")
    if layer is None:
        bm.edges.index_update()
        indices = [e.index for e in edges]
        layer = _crease_layer(bm)
        bm.edges.ensure_lookup_table()
        edges = [bm.edges[i] for i in indices]
    for e in edges:
        e[layer] = value


def make_half(bm: _BMesh, tolerance: float = 0.001) -> dict[str, int]:
    """
    Make the right half cage of a whole mesh: cut it at ``X = 0`` and remove the ``-X`` side.

    For a mesh built without a Mirror modifier (e.g. converted from a Skin modifier), before
    :func:`setup_mirror_subsurf`. Vertices within *tolerance* of the plane are moved onto it
    and faces left on the plane are removed, so the cage is open there. On a half cage, it
    also moves the vertices just off the plane (``off_center_vertices`` of
    ``get_mesh_topology_summary``, with a large enough *tolerance*) back onto it.
    Return the number of the mesh's vertices removed (``removed_vertices``) and moved onto
    the plane (``snapped_vertices``), not counting the vertices made by the cut.
    Edges without faces (wire edges) on the ``+X`` side are kept.
    The indices and lookup tables of *bm* are updated.
    """
    original = set(bm.verts)
    wire = {e for e in bm.edges if not e.link_faces}
    bmesh.ops.bisect_plane(
        bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], dist=tolerance,
        plane_co=(0.0, 0.0, 0.0), plane_no=(1.0, 0.0, 0.0), clear_inner=True,
    )
    snapped = 0
    for v in bm.verts:
        if v.co.x != 0.0 and abs(v.co.x) <= tolerance:
            v.co.x = 0.0
            if v in original:
                snapped += 1
    on_plane = [f for f in bm.faces if all(_on_mirror_plane(v) for v in f.verts)]
    bmesh.ops.delete(bm, geom=on_plane, context='FACES_ONLY')
    # Only the edges left on the plane without faces, not the mesh's own wire edges.
    cut_wire = [e for e in bm.edges
                if not e.link_faces and e not in wire and all(_on_mirror_plane(v) for v in e.verts)]
    bmesh.ops.delete(bm, geom=cut_wire, context='EDGES')
    _update_indices(bm)
    removed = sum(1 for v in original if not v.is_valid)
    return {"removed_vertices": removed, "snapped_vertices": snapped}


def symmetrize(bm: _BMesh, source: str = '+X', tolerance: float = 0.001) -> None:
    """
    Make a whole mesh symmetric by copying its *source* side (``'+X'``, ``'-X'``, ``'+Y'``, ...)
    onto the other side.

    For a mesh without a Mirror modifier: the other side is replaced, faces crossing the
    plane are cut. Vertices within *tolerance* of the plane are merged with their copy.
    For a half cage under a Mirror modifier, use :func:`make_half` instead.
    The indices and lookup tables of *bm* are updated.
    """
    directions = {'+X': 'X', '-X': '-X', '+Y': 'Y', '-Y': '-Y', '+Z': 'Z', '-Z': '-Z'}
    if source not in directions:
        raise ValueError("source must be one of {:s}, not {!r}".format(", ".join(directions), source))
    bmesh.ops.symmetrize(
        bm, input=bm.verts[:] + bm.edges[:] + bm.faces[:], direction=directions[source], dist=tolerance,
        use_shapekey=True,
    )
    _update_indices(bm)
