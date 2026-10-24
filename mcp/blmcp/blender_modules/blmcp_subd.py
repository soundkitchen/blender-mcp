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
        result = {"loop": [v.index for v in loop]}
"""

__all__ = (
    "Extruded",
    "bridge_loops",
    "edge_ring",
    "edit_mesh",
    "extrude",
    "insert_loop",
    "setup_mirror_subsurf",
)

import contextlib
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, NamedTuple

import bmesh  # pylint: disable=import-error

# Placeholder types for Blender Python objects (no stubs available).
_BMesh = Any
_BMVert = Any
_BMEdge = Any
_BMFace = Any
_Object = Any

# Vertices closer than this to `X = 0` are on the mirror plane.
_MIRROR_PLANE_TOLERANCE = 1e-6


@contextlib.contextmanager
def edit_mesh(obj: _Object) -> Iterator[_BMesh]:
    """
    Give a ``bmesh`` of the mesh object *obj* and write it back when the block ends.

    Works in Object Mode and Edit Mode. The indices and lookup tables (``bm.verts[i]``)
    are ready at the start, the helpers of this module update them after changing the mesh.
    In Object Mode the ``bmesh`` is freed at the end of the block: read what you need
    (e.g. ``v.index``) inside it. An exception leaves the mesh unchanged in Object Mode,
    in Edit Mode the changes made before it stay in the edit-mesh.
    """
    if obj.type != 'MESH':
        raise TypeError("Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    mesh = obj.data
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        _update_indices(bm)
        yield bm
        bmesh.update_edit_mesh(mesh)
        return
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
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
    The indices and lookup tables of *bm* are updated.
    """
    faces = list(faces)
    if not faces:
        raise ValueError("No faces to extrude")
    old_verts = {v for f in faces for v in f.verts}
    old_edges = {e for f in faces for e in f.edges}
    # The new vertices are created at the position of their source. Inner vertices
    # (without an edge to their source) are matched by position.
    old_by_co = {v.co.to_tuple(): v for v in old_verts}

    res = bmesh.ops.extrude_face_region(bm, geom=faces)
    new_faces = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
    new_verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
    source = {}
    for v in new_verts:
        src = next((e.other_vert(v) for e in v.link_edges if e.other_vert(v) in old_verts), None)
        source[v] = src if src is not None else old_by_co[v.co.to_tuple()]
    # Sort the new faces like the given faces, matched by their source vertices.
    order = {frozenset(f.verts): i for i, f in enumerate(faces)}
    new_faces.sort(key=lambda f: order[frozenset(source[v] for v in f.verts)])

    bmesh.ops.delete(bm, geom=faces, context='FACES')
    new_face_set = set(new_faces)
    sides = {f for v in new_verts for f in v.link_faces if f not in new_face_set}
    on_plane = [f for f in sides if all(abs(v.co.x) < _MIRROR_PLANE_TOLERANCE for v in f.verts)]
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

    old_verts = set(bm.verts)
    bmesh.ops.subdivide_edgering(bm, edges=ring, cuts=cuts)
    # Each ring edge is now a chain of edges: walk it from its start to its end.
    columns = []
    for start, end in ends:
        column = []
        prev, v = None, start
        while True:
            v_next = next(
                w for w in (e2.other_vert(v) for e2 in v.link_edges)
                if w is not prev and (w is end or w not in old_verts) and _is_between(w, start, end)
            )
            if v_next is end:
                break
            column.append(v_next)
            prev, v = v, v_next
        columns.append(column)
    _update_indices(bm)
    return [list(row) for row in zip(*columns)]


def _end_beside(prev: _BMEdge, cur: _BMEdge, prev_start: _BMVert) -> _BMVert:
    # The end of `cur` joined to `prev_start` by an edge.
    for v in cur.verts:
        if any(e.other_vert(v) is prev_start for e in v.link_edges):
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
    The normals of the faces connected to the new ones are recalculated to point outwards.
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
    # Orient the new faces like the faces around them.
    bmesh.ops.recalc_face_normals(bm, faces=list(_connected_faces(faces)))
    _update_indices(bm)
    return faces


def _connected_faces(faces: Iterable[_BMFace]) -> set[_BMFace]:
    seen = set(faces)
    stack = list(seen)
    while stack:
        f = stack.pop()
        for e in f.edges:
            for f2 in e.link_faces:
                if f2 not in seen:
                    seen.add(f2)
                    stack.append(f2)
    return seen


def setup_mirror_subsurf(obj: _Object, levels: int = 2) -> dict[str, Any]:
    """
    Set up the modifiers of a half cage: Mirror on ``X`` (merge and clipping) then Subdivision Surface.

    Existing modifiers are reused (the first Mirror without a mirror object and the first
    Subdivision Surface), created when missing and moved so Mirror comes before Subdivision
    Surface. Other modifiers are left as they are.
    Return the names of the modifiers, in their final order.
    """
    if obj.type != 'MESH':
        raise TypeError("Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    mods = obj.modifiers
    mirror = next((m for m in mods if m.type == 'MIRROR' and m.mirror_object is None), None)
    if mirror is None:
        mirror = mods.new("Mirror", 'MIRROR')
    mirror.use_axis = (True, False, False)
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
