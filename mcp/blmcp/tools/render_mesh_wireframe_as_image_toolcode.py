# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for drawing a mesh cage with vertex indices in orthographic views, returned as a PNG image.
"""

__all__ = (
    "Params",
    "Result",
    "ViewName",
    "main",
)

from typing import Any, Literal, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_BMesh = Any
_Object = Any

ViewName = Literal["FRONT", "BACK", "RIGHT", "LEFT", "TOP", "BOTTOM"]


class Params(NamedTuple):
    name: str
    views: list[ViewName]
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None
    size_limit_in_bytes: int = 0


class Result(NamedTuple):
    status: str
    image_base64: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    is_edit_mode: bool | None = None
    views: list[dict[str, Any]] | None = None
    units_per_pixel: float | None = None
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None
    vertices_labeled: int | None = None
    vertices_unlabeled: list[int] | None = None
    vertices_hidden: int | None = None
    vertices_outside_bounds: int | None = None
    message: str | None = None


# @include_begin: _template_image_downscale_to_size_limit.py
def _image_downscale_to_size_limit(
        tmpdir: str, filepath: str, size_limit_in_bytes: int, size_tolerance_in_bytes: int = 0,
        use_hidpi_downscale: bool = True,
) -> bytes:
    return b''
# @include_end


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

# Image layout in pixels.
_IMAGE_HEIGHT_MAX = 1200
_IMAGE_WIDTH_MAX = 2400
_PANEL_MARGIN = 24
_PANEL_TITLE_HEIGHT = 28
_FONT_SIZE = 13

# Colors (RGBA).
_COLOR_BACKGROUND = (0.13, 0.13, 0.13, 1.0)
_COLOR_PANEL_BORDER = (0.3, 0.3, 0.3, 1.0)
_COLOR_EVALUATED = (0.26, 0.26, 0.28, 1.0)
_COLOR_CAGE_FACE = (0.36, 0.42, 0.5, 0.55)
_COLOR_EDGE_HIDDEN = (0.55, 0.55, 0.55, 0.35)
_COLOR_EDGE = (0.95, 0.75, 0.25, 1.0)
_COLOR_VERTEX = (1.0, 1.0, 1.0, 1.0)
_COLOR_LABEL = (1.0, 1.0, 1.0, 1.0)
_COLOR_LABEL_BACKGROUND = (0.0, 0.0, 0.0, 0.65)
_COLOR_TITLE = (0.85, 0.85, 0.85, 1.0)

# Faces are pushed back in depth (NDC units) so the edges on them pass the depth test.
_FACE_DEPTH_OFFSET = 0.004
# Tolerance (window depth units) when testing whether a vertex is in front of the faces.
_VERTEX_DEPTH_TOLERANCE = 0.002
# A vertex is compared with the closest face depth within this many pixels around it:
# vertices on the outline have no face drawn at their own pixel.
_VERTEX_DEPTH_RADIUS = 2
# The maximum number of unlabeled vertex indices listed in the result.
_UNLABELED_MAX = 100


class _Geometry(NamedTuple):
    """
    The cage and the evaluated mesh, in object space.
    """

    # Cage vertex coordinates, numbered by their order (the mesh indices).
    coords: list[tuple[float, float, float]]
    # Cage edges as vertex index pairs.
    edges: list[tuple[int, int]]
    # Cage faces as triangles of vertex indices.
    triangles: list[tuple[int, int, int]]
    # Evaluated mesh (after modifiers) triangles as coordinates.
    evaluated_triangles: list[tuple[float, float, float]]


class _Panel(NamedTuple):
    view: str
    # Screen axes as `(axis, sign)`.
    right: tuple[int, int]
    up: tuple[int, int]
    # Panel rectangle in the image (pixels, origin at the bottom left).
    x: int
    y: int
    width: int
    height: int
    # Center of the drawn region on the screen axes.
    center_right: float
    center_up: float


def _read_geometry(obj: _Object, bm: _BMesh) -> _Geometry:
    import bpy  # pylint: disable=import-error,no-name-in-module

    verts = {v: i for i, v in enumerate(bm.verts)}
    coords = [tuple(v.co) for v in bm.verts]
    edges = [(verts[e.verts[0]], verts[e.verts[1]]) for e in bm.edges]
    triangles = [tuple(verts[loop.vert] for loop in tri) for tri in bm.calc_loop_triangles()]

    evaluated_triangles: list[tuple[float, float, float]] = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    if obj_eval.is_evaluated:
        mesh = obj_eval.to_mesh()
        try:
            mesh.calc_loop_triangles()
            positions = [tuple(v.co) for v in mesh.vertices]
            for tri in mesh.loop_triangles:
                evaluated_triangles.extend(positions[i] for i in tri.vertices)
        finally:
            obj_eval.to_mesh_clear()
    return _Geometry(coords, edges, triangles, evaluated_triangles)  # type: ignore[arg-type]


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


def _range(values: list[float]) -> tuple[float, float]:
    return (min(values), max(values)) if values else (-1.0, 1.0)


def _layout(
        views: list[ViewName],
        bounds_min: list[float],
        bounds_max: list[float],
) -> tuple[list[_Panel], float, int, int]:
    """
    Place one panel per view side by side, all at the same scale.

    Return the panels, the units per pixel and the image size.
    """
    corners = [
        (x, y, z)
        for x in (bounds_min[0], bounds_max[0])
        for y in (bounds_min[1], bounds_max[1])
        for z in (bounds_min[2], bounds_max[2])
    ]
    extents = []
    for view in views:
        right, up = _VIEW_AXES[view]
        r_lo, r_hi = _range([_screen(c, right) for c in corners])
        u_lo, u_hi = _range([_screen(c, up) for c in corners])
        extents.append((max(r_hi - r_lo, 1e-6), max(u_hi - u_lo, 1e-6), (r_lo + r_hi) / 2.0, (u_lo + u_hi) / 2.0))

    inner_height = _IMAGE_HEIGHT_MAX - 2 * _PANEL_MARGIN - _PANEL_TITLE_HEIGHT
    inner_width = _IMAGE_WIDTH_MAX - 2 * _PANEL_MARGIN * len(views)
    pixels_per_unit = min(
        inner_height / max(e[1] for e in extents),
        inner_width / sum(e[0] for e in extents),
    )

    panel_height = round(max(e[1] for e in extents) * pixels_per_unit) + 2 * _PANEL_MARGIN + _PANEL_TITLE_HEIGHT
    panels = []
    x = 0
    for view, (width, _height, center_right, center_up) in zip(views, extents):
        right, up = _VIEW_AXES[view]
        panel_width = max(round(width * pixels_per_unit) + 2 * _PANEL_MARGIN, 160)
        panels.append(_Panel(view, right, up, x, 0, panel_width, panel_height, center_right, center_up))
        x += panel_width
    return panels, 1.0 / pixels_per_unit, x, panel_height


def _panel_matrix(
        panel: _Panel,
        units_per_pixel: float,
        depth_range: tuple[float, float],
        depth_offset: float = 0.0,
) -> Any:
    """
    Return the matrix projecting object space to the panel's normalized device coordinates.
    """
    from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module

    # The drawable area excludes the title at the top of the panel.
    half_width = panel.width * units_per_pixel / 2.0
    half_height = (panel.height - _PANEL_TITLE_HEIGHT) * units_per_pixel / 2.0
    # Shift the center down by half the title, keeping the drawing centered below it.
    center_up = panel.center_up + _PANEL_TITLE_HEIGHT * units_per_pixel / 2.0
    depth_lo, depth_hi = depth_range
    depth_half = max((depth_hi - depth_lo) / 2.0, 1e-6)
    depth_center = (depth_lo + depth_hi) / 2.0

    rows = []
    for (axis, sign), center, half in (
            (panel.right, panel.center_right, half_width),
            (panel.up, center_up, half_height),
    ):
        row = [0.0, 0.0, 0.0, -center / half]
        row[axis] = sign / half
        rows.append(row)
    # Closer to the viewer maps to a smaller depth.
    depth_axis = 3 - panel.right[0] - panel.up[0]
    depth_sign = _depth([1.0 if i == depth_axis else 0.0 for i in range(3)], panel.right, panel.up)
    row = [0.0, 0.0, 0.0, depth_center / depth_half + depth_offset]
    row[depth_axis] = -depth_sign / depth_half
    rows.append(row)
    rows.append([0.0, 0.0, 0.0, 1.0])
    return Matrix(rows)


def _to_pixel(co: Any, panel: _Panel, units_per_pixel: float) -> tuple[float, float]:
    """
    Return the image pixel position of *co* in *panel*.
    """
    center_up = panel.center_up + _PANEL_TITLE_HEIGHT * units_per_pixel / 2.0
    px = panel.x + panel.width / 2.0 + (_screen(co, panel.right) - panel.center_right) / units_per_pixel
    py = panel.y + (panel.height - _PANEL_TITLE_HEIGHT) / 2.0 + (_screen(co, panel.up) - center_up) / units_per_pixel
    # The title strip is at the top, the drawing area starts at the panel's bottom.
    return px, py


def _draw_panel(
        panel: _Panel,
        geometry: _Geometry,
        units_per_pixel: float,
        depth_range: tuple[float, float],
        in_bounds: list[bool],
        framebuffer: Any,
) -> list[bool]:
    """
    Draw the geometry of *panel*, return the visibility of each cage vertex.
    """
    import gpu  # pylint: disable=import-error,no-name-in-module
    from gpu_extras.batch import batch_for_shader  # pylint: disable=import-error,no-name-in-module

    gpu.state.viewport_set(panel.x, panel.y, panel.width, panel.height - _PANEL_TITLE_HEIGHT)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    polyline = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    polyline.uniform_float("viewportSize", (panel.width, panel.height - _PANEL_TITLE_HEIGHT))
    coords = geometry.coords

    def draw(sh: Any, kind: str, positions: Any, color: tuple[float, ...], indices: Any = None) -> None:
        if not positions:
            return
        sh.uniform_float("color", color)
        batch_for_shader(sh, kind, {"pos": positions}, indices=indices).draw(sh)

    with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
        from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module
        gpu.matrix.load_matrix(Matrix.Identity(4))

        # The evaluated mesh as a flat silhouette behind everything.
        gpu.matrix.load_projection_matrix(_panel_matrix(panel, units_per_pixel, depth_range))
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)
        draw(shader, 'TRIS', geometry.evaluated_triangles, _COLOR_EVALUATED)

        # Cage faces, pushed back so the edges on them stay in front.
        gpu.matrix.load_projection_matrix(
            _panel_matrix(panel, units_per_pixel, depth_range, depth_offset=_FACE_DEPTH_OFFSET))
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
        gpu.state.blend_set('ALPHA')
        draw(shader, 'TRIS', coords, _COLOR_CAGE_FACE, indices=geometry.triangles)

        # Read the depth of the faces to test which vertices are visible.
        width = panel.width
        height = panel.height - _PANEL_TITLE_HEIGHT
        depth_buffer = framebuffer.read_depth(panel.x, panel.y, width, height)
        depth_buffer.dimensions = width * height

        gpu.matrix.load_projection_matrix(_panel_matrix(panel, units_per_pixel, depth_range))
        gpu.state.depth_mask_set(False)
        # Hidden edges faintly (no depth test), then the visible edges over them.
        gpu.state.depth_test_set('NONE')
        polyline.uniform_float("lineWidth", 1.0)
        draw(polyline, 'LINES', coords, _COLOR_EDGE_HIDDEN, indices=geometry.edges)
        gpu.state.depth_test_set('LESS_EQUAL')
        polyline.uniform_float("lineWidth", 1.5)
        draw(polyline, 'LINES', coords, _COLOR_EDGE, indices=geometry.edges)
        gpu.state.depth_test_set('NONE')

    # A vertex is visible when it's not behind the faces.
    depth_lo, depth_hi = depth_range
    depth_half = max((depth_hi - depth_lo) / 2.0, 1e-6)
    depth_center = (depth_lo + depth_hi) / 2.0
    visible = []
    for co, inside in zip(coords, in_bounds):
        if not inside:
            visible.append(False)
            continue
        px, py = _to_pixel(co, panel, units_per_pixel)
        ix = int(px) - panel.x
        iy = int(py) - panel.y
        if not (0 <= ix < width and 0 <= iy < height):
            visible.append(False)
            continue
        ndc = (depth_center - _depth(co, panel.right, panel.up)) / depth_half
        window_depth = (ndc + 1.0) / 2.0
        face_depth = min(
            depth_buffer[y * width + x]
            for y in range(max(iy - _VERTEX_DEPTH_RADIUS, 0), min(iy + _VERTEX_DEPTH_RADIUS + 1, height))
            for x in range(max(ix - _VERTEX_DEPTH_RADIUS, 0), min(ix + _VERTEX_DEPTH_RADIUS + 1, width))
        )
        visible.append(window_depth <= face_depth + _VERTEX_DEPTH_TOLERANCE)
    return visible


def _draw_labels(
        panels: list[_Panel],
        geometry: _Geometry,
        units_per_pixel: float,
        visibility: list[list[bool]],
        image_size: tuple[int, int],
) -> tuple[set[int], set[int]]:
    """
    Draw the panel titles, vertex dots and index labels in image pixel space.

    Labels that would overlap an earlier one are skipped.
    Return the labeled and the unlabeled visible vertex indices (over all panels).
    """
    import blf  # pylint: disable=import-error,no-name-in-module
    import gpu  # pylint: disable=import-error,no-name-in-module
    from gpu_extras.batch import batch_for_shader  # pylint: disable=import-error,no-name-in-module
    from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module

    image_width, image_height = image_size
    gpu.state.viewport_set(0, 0, image_width, image_height)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    point_shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')

    labeled: set[int] = set()
    unlabeled: set[int] = set()
    font_id = 0
    blf.size(font_id, _FONT_SIZE)
    _, text_height = blf.dimensions(font_id, "0123456789")

    with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
        gpu.matrix.load_matrix(Matrix.Identity(4))
        gpu.matrix.load_projection_matrix(
            Matrix.Translation((-1.0, -1.0, 0.0)) @ Matrix.Diagonal((2.0 / image_width, 2.0 / image_height, 1.0, 1.0))
        )
        gpu.state.blend_set('ALPHA')

        for panel, visible in zip(panels, visibility):
            # Panel border and title.
            x0, y0 = panel.x + 0.5, panel.y + 0.5
            x1, y1 = panel.x + panel.width - 0.5, panel.y + panel.height - 0.5
            shader.uniform_float("color", _COLOR_PANEL_BORDER)
            batch_for_shader(shader, 'LINE_LOOP', {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]}).draw(shader)
            right_axis, right_sign = panel.right
            up_axis, up_sign = panel.up
            title = "{:s}  (right: {:s}{:s}, up: {:s}{:s})".format(
                panel.view,
                "+" if right_sign > 0 else "-", _AXIS_NAMES[right_axis],
                "+" if up_sign > 0 else "-", _AXIS_NAMES[up_axis],
            )
            blf.color(font_id, *_COLOR_TITLE)
            blf.position(font_id, panel.x + 8, panel.y + panel.height - _PANEL_TITLE_HEIGHT + 8, 0)
            blf.draw(font_id, title)

            points = []
            boxes: list[tuple[float, float, float, float]] = []
            label_draws = []
            for index, co in enumerate(geometry.coords):
                if not visible[index]:
                    continue
                px, py = _to_pixel(co, panel, units_per_pixel)
                points.append((px, py))
                text = str(index)
                text_width, _ = blf.dimensions(font_id, text)
                placed = None
                for dx, dy in ((4, 4), (4, -text_height - 4), (-text_width - 4, 4), (-text_width - 4, -text_height - 4)):
                    box = (px + dx - 2, py + dy - 2, px + dx + text_width + 2, py + dy + text_height + 2)
                    if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in boxes):
                        continue
                    placed = (box, px + dx, py + dy)
                    break
                if placed is None:
                    unlabeled.add(index)
                    continue
                boxes.append(placed[0])
                label_draws.append((text, placed[1], placed[2]))
                labeled.add(index)

            if points:
                gpu.state.point_size_set(5.0)
                point_shader.uniform_float("color", _COLOR_VERTEX)
                batch_for_shader(point_shader, 'POINTS', {"pos": points}).draw(point_shader)
            if boxes:
                shader.uniform_float("color", _COLOR_LABEL_BACKGROUND)
                rects = []
                for bx0, by0, bx1, by1 in boxes:
                    rects.extend([(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by0), (bx1, by1), (bx0, by1)])
                batch_for_shader(shader, 'TRIS', {"pos": rects}).draw(shader)
            blf.color(font_id, *_COLOR_LABEL)
            for text, tx, ty in label_draws:
                blf.position(font_id, tx, ty, 0)
                blf.draw(font_id, text)

        gpu.state.blend_set('NONE')

    # A vertex labeled in any view is not reported as unlabeled.
    return labeled, unlabeled - labeled


def _render(
        geometry: _Geometry,
        panels: list[_Panel],
        units_per_pixel: float,
        image_size: tuple[int, int],
        depth_ranges: list[tuple[float, float]],
        in_bounds: list[bool],
        filepath: str,
) -> tuple[set[int], set[int], int]:
    """
    Draw all panels and write the image to *filepath* as a PNG.

    Return the labeled and unlabeled vertex indices, and the number of vertices hidden in every view.
    """
    import gpu  # pylint: disable=import-error,no-name-in-module
    import imbuf  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module

    image_width, image_height = image_size
    offscreen = gpu.types.GPUOffScreen(image_width, image_height, format='RGBA8')
    try:
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            framebuffer.clear(color=_COLOR_BACKGROUND, depth=1.0)
            visibility = [
                _draw_panel(panel, geometry, units_per_pixel, depth_range, in_bounds, framebuffer)
                for panel, depth_range in zip(panels, depth_ranges)
            ]
            labeled, unlabeled = _draw_labels(panels, geometry, units_per_pixel, visibility, image_size)
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            pixels = framebuffer.read_color(0, 0, image_width, image_height, 4, 0, 'UBYTE')
    finally:
        offscreen.free()

    pixels.dimensions = image_width * image_height * 4
    im = imbuf.new((image_width, image_height))
    try:
        with im.with_buffer(write=True) as buf:
            buf.cast('B')[:] = bytes(pixels)
        imbuf.write(im, filepath=filepath)
    finally:
        im.free()

    hidden = sum(
        1 for index, inside in enumerate(in_bounds)
        if inside and not any(visible[index] for visible in visibility)
    )
    return labeled, unlabeled, hidden


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


def main(params: Params) -> Result:
    import base64
    import os
    import struct
    import tempfile

    import bmesh  # pylint: disable=import-error,no-name-in-module
    import bpy  # pylint: disable=import-error,no-name-in-module

    obj = bpy.data.objects.get(params.name)
    if obj is None:
        return Result(status="error", message="Object {!r} not found".format(params.name))
    if obj.type != 'MESH':
        return Result(status="error", message="Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    views = list(dict.fromkeys(params.views))
    if not views:
        return Result(status="error", message="At least one view is required")
    if (params.bounds_min is None) != (params.bounds_max is None):
        return Result(status="error", message="Pass both bounds_min and bounds_max, or neither")

    mesh = obj.data
    is_edit_mode = mesh.is_editmode
    if is_edit_mode:
        # Read the edit-mesh so unsynced edits are included, it's only read, never modified.
        geometry = _read_geometry(obj, bmesh.from_edit_mesh(mesh))
    else:
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            geometry = _read_geometry(obj, bm)
        finally:
            bm.free()
    if not geometry.coords:
        return Result(status="error", message="Object {!r} has no vertices".format(obj.name))

    all_coords = geometry.coords + geometry.evaluated_triangles
    if params.bounds_min is not None and params.bounds_max is not None:
        bounds_min = [min(a, b) for a, b in zip(params.bounds_min, params.bounds_max)]
        bounds_max = [max(a, b) for a, b in zip(params.bounds_min, params.bounds_max)]
    else:
        bounds_min = [min(c[axis] for c in all_coords) for axis in range(3)]
        bounds_max = [max(c[axis] for c in all_coords) for axis in range(3)]
        # Leave a little room around the mesh.
        pad = max(hi - lo for lo, hi in zip(bounds_min, bounds_max)) * 0.02
        bounds_min = [v - pad for v in bounds_min]
        bounds_max = [v + pad for v in bounds_max]
    in_bounds = [
        all(lo <= c <= hi for c, lo, hi in zip(co, bounds_min, bounds_max))
        for co in geometry.coords
    ]

    panels, units_per_pixel, image_width, image_height = _layout(views, bounds_min, bounds_max)

    # Each view clips the depth to the bounds along its own axis.
    depth_ranges = []
    for panel in panels:
        depths = [
            _depth((x, y, z), panel.right, panel.up)
            for x in (bounds_min[0], bounds_max[0])
            for y in (bounds_min[1], bounds_max[1])
            for z in (bounds_min[2], bounds_max[2])
        ]
        depth_ranges.append((min(depths), max(depths)))

    _gpu_ensure()
    with tempfile.TemporaryDirectory(prefix="blmcp_wireframe_") as tmpdir:
        filepath = os.path.join(tmpdir, "wireframe.png")
        labeled, unlabeled, hidden = _render(
            geometry, panels, units_per_pixel, (image_width, image_height), depth_ranges, in_bounds, filepath,
        )
        size_limit = params.size_limit_in_bytes if params.size_limit_in_bytes > 0 else _IMAGE_SIZE_LIMIT_IN_BYTES
        # Defined by `_template_image_downscale_to_size_limit.py`.
        image_data = _image_downscale_to_size_limit(
            tmpdir, filepath, size_limit_in_bytes=size_limit, use_hidpi_downscale=False,
        )

    # The size of the returned image, it may have been downscaled (from the PNG header).
    image_width, image_height = struct.unpack(">II", image_data[16:24])
    sorted_unlabeled = sorted(unlabeled)
    return Result(
        status="ok",
        image_base64=base64.b64encode(image_data).decode("ascii"),
        image_width=image_width,
        image_height=image_height,
        is_edit_mode=is_edit_mode,
        views=[
            {
                "view": panel.view,
                "right": ("+" if panel.right[1] > 0 else "-") + _AXIS_NAMES[panel.right[0]],
                "up": ("+" if panel.up[1] > 0 else "-") + _AXIS_NAMES[panel.up[0]],
                "rect": [panel.x, panel.y, panel.width, panel.height],
            }
            for panel in panels
        ],
        units_per_pixel=round(units_per_pixel, 8),
        bounds_min=[round(v, 6) for v in bounds_min],
        bounds_max=[round(v, 6) for v in bounds_max],
        vertices_labeled=len(labeled),
        vertices_unlabeled=sorted_unlabeled[:_UNLABELED_MAX],
        vertices_hidden=hidden,
        vertices_outside_bounds=in_bounds.count(False),
    )
