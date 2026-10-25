# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for comparing a mesh with a reference image in an orthographic view, returned as a JPEG image.
"""

__all__ = (
    "FitName",
    "Params",
    "Result",
    "ViewName",
    "main",
)

from typing import Any, Literal, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_Object = Any
# Placeholder for `numpy.ndarray` (numpy is only available in Blender).
_Array = Any

ViewName = Literal["FRONT", "BACK", "RIGHT", "LEFT", "TOP", "BOTTOM"]
FitName = Literal["HEIGHT", "WIDTH"]


class Params(NamedTuple):
    name: str
    image_path: str
    view: ViewName
    subject_box: list[float] | None = None
    fit: FitName = "HEIGHT"
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None
    size_limit_in_bytes: int = 0


class Result(NamedTuple):
    status: str
    image_base64: str | None = None
    image_format: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    view: str | None = None
    right: str | None = None
    up: str | None = None
    panels: list[dict[str, Any]] | None = None
    units_per_pixel: float | None = None
    objects: list[str] | None = None
    reference_size: list[int] | None = None
    reference_has_alpha: bool | None = None
    subject_box: list[float] | None = None
    subject_box_from_alpha: bool | None = None
    fit: str | None = None
    units_per_reference_pixel: float | None = None
    model_size: list[float] | None = None
    subject_size: list[float] | None = None
    size_ratio: list[float] | None = None
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None
    overlap: float | None = None
    model_only: float | None = None
    reference_only: float | None = None
    rows: list[dict[str, Any]] | None = None
    columns: list[dict[str, Any]] | None = None
    message: str | None = None


# @include_begin: _template_ortho_views.py
_VIEW_AXES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}


def _axis_label(axis_sign: tuple[int, int]) -> str:
    return ""


def _screen(co: Any, axis_sign: tuple[int, int]) -> float:
    return 0.0


def _depth(co: Any, right: tuple[int, int], up: tuple[int, int]) -> float:
    return 0.0


def _ortho_matrix(
        right: tuple[int, int],
        up: tuple[int, int],
        center: tuple[float, float],
        half_size: tuple[float, float],
        depth_range: tuple[float, float],
        depth_offset: float = 0.0,
) -> Any:
    return None


def _gpu_ensure() -> None:
    pass

# @include_end


# MCP messages are limited to 1,048,576 bytes (1 MB). The image is base64-encoded
# which expands data by 4/3. Subtract 8 KiB headroom for the JSON envelope and info.
# NOTE: the same calculation as in `render_mesh_wireframe_as_image`.
_IMAGE_SIZE_LIMIT_IN_BYTES = ((1_048_576 - 8192) * 3) // 4
# The image shows a photo: JPEG keeps it much smaller than PNG at the same size in pixels.
_JPEG_QUALITY = 85
# The smallest image (pixels on the short side) when downscaling to the size limit.
_IMAGE_SIZE_MIN = 64

# Image layout in pixels.
_PANEL_HEIGHT_MAX = 800
_PANEL_WIDTH_MAX = 1000
_PANEL_TITLE_HEIGHT = 28
_FONT_SIZE = 13
_OUTLINE_WIDTH = 3
# The resolution (pixels on the long side) used to measure the model's silhouette.
_MEASURE_SIZE = 1024
# Room around the subject and the model, as a fraction of their size.
_MARGIN = 0.08
# Pixels with at least this alpha (0-1) belong to the subject of a reference with transparency.
_ALPHA_THRESHOLD = 0.5
# The number of bands the silhouettes are split in, along each screen axis.
_BANDS = 10

# Colors (RGB, 0-1).
_COLOR_BACKGROUND = (0.13, 0.13, 0.13)
_COLOR_TITLE = (0.85, 0.85, 0.85)
_COLOR_PANEL_BORDER = (0.3, 0.3, 0.3)
_COLOR_MODEL = (0.8, 0.78, 0.75)
_COLOR_MODEL_OUTLINE = (1.0, 0.85, 0.1)
_COLOR_REFERENCE_OUTLINE = (0.2, 0.9, 1.0)
_COLOR_MODEL_ONLY = (1.0, 0.2, 0.2)
_COLOR_REFERENCE_ONLY = (0.2, 0.45, 1.0)
# Opacity of the colors marking the parts only in the model or only in the reference.
_DIFFERENCE_OPACITY = 0.5
# Shading: the ambient part, the rest comes from a light near the viewer.
_SHADE_AMBIENT = 0.25


class _Geometry(NamedTuple):
    """
    The evaluated triangles of the object and its mesh children, in the object's space.
    """

    # Triangle corners, ``(N * 3, 3)`` float32.
    positions: _Array
    # Normals per corner, ``(N * 3, 3)`` float32.
    normals: _Array
    # Names of the drawn objects.
    objects: list[str]


class _View(NamedTuple):
    """
    A region of the view drawn into an image of *width* x *height* pixels.
    """

    right: tuple[int, int]
    up: tuple[int, int]
    # Center of the region on the screen axes, in object units.
    center: tuple[float, float]
    units_per_pixel: float
    width: int
    height: int
    depth_range: tuple[float, float]


def _read_geometry(obj: _Object) -> _Geometry:
    """
    Read the evaluated meshes of *obj* and its visible mesh children (recursively).
    """
    import bpy  # pylint: disable=import-error,no-name-in-module
    import numpy as np  # pylint: disable=import-error

    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    world_to_object = obj_eval.matrix_world.inverted()
    objects = [obj] + [
        child for child in obj.children_recursive
        if child.type == 'MESH' and child.visible_get()
    ]
    positions = []
    normals = []
    names = []
    for ob in objects:
        ob_eval = ob.evaluated_get(depsgraph)
        matrix = world_to_object @ ob_eval.matrix_world
        normal_matrix = matrix.to_3x3().inverted_safe().transposed()
        mesh = ob_eval.to_mesh()
        try:
            mesh.calc_loop_triangles()
            tri_count = len(mesh.loop_triangles)
            if tri_count == 0:
                continue
            co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", co)
            corner_verts = np.empty(tri_count * 3, dtype=np.int32)
            mesh.loop_triangles.foreach_get("vertices", corner_verts)
            corners = np.empty(tri_count * 3, dtype=np.int32)
            mesh.loop_triangles.foreach_get("loops", corners)
            corner_normals = np.empty(len(mesh.corner_normals) * 3, dtype=np.float32)
            mesh.corner_normals.foreach_get("vector", corner_normals)
        finally:
            ob_eval.to_mesh_clear()
        m = np.array(matrix, dtype=np.float32)
        n = np.array(normal_matrix, dtype=np.float32)
        tri_co = co.reshape(-1, 3)[corner_verts]
        positions.append(tri_co @ m[:3, :3].T + m[:3, 3])
        tri_normals = corner_normals.reshape(-1, 3)[corners] @ n.T
        lengths = np.linalg.norm(tri_normals, axis=1, keepdims=True)
        normals.append(tri_normals / np.maximum(lengths, 1e-12))
        names.append(ob.name)
    if not positions:
        return _Geometry(np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.float32), names)
    return _Geometry(
        np.concatenate(positions).astype(np.float32),
        np.concatenate(normals).astype(np.float32),
        names,
    )


def _axis_vector(axis_sign: tuple[int, int]) -> list[float]:
    axis, sign = axis_sign
    return [float(sign) if i == axis else 0.0 for i in range(3)]


def _shade(geometry: _Geometry, right: tuple[int, int], up: tuple[int, int]) -> _Array:
    """
    Return an RGBA color per corner, lit from near the viewer (above and left of it).
    """
    import numpy as np  # pylint: disable=import-error

    toward_viewer = np.cross(_axis_vector(right), _axis_vector(up))
    light = toward_viewer + 0.6 * np.array(_axis_vector(up)) - 0.4 * np.array(_axis_vector(right))
    light = light / np.linalg.norm(light)
    # Both sides are lit: normals of mirrored or open parts may point away.
    intensity = _SHADE_AMBIENT + (1.0 - _SHADE_AMBIENT) * np.abs(geometry.normals @ light.astype(np.float32))
    colors = np.empty((len(geometry.positions), 4), dtype=np.float32)
    colors[:, :3] = intensity[:, None] * np.array(_COLOR_MODEL, dtype=np.float32)
    colors[:, 3] = 1.0
    return colors


def _draw_model(geometry: _Geometry, view: _View, x: int, y: int, shaded: bool) -> None:
    """
    Draw the geometry into the bound framebuffer, in the viewport at *x*, *y* of the view's size.
    """
    import gpu  # pylint: disable=import-error,no-name-in-module
    from gpu_extras.batch import batch_for_shader  # pylint: disable=import-error,no-name-in-module
    from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module

    if not len(geometry.positions):
        return
    gpu.state.viewport_set(x, y, view.width, view.height)
    with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
        gpu.matrix.load_matrix(Matrix.Identity(4))
        # Defined by `_template_ortho_views.py`.
        gpu.matrix.load_projection_matrix(_ortho_matrix(
            view.right,
            view.up,
            view.center,
            (view.width * view.units_per_pixel / 2.0, view.height * view.units_per_pixel / 2.0),
            view.depth_range,
        ))
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
        if shaded:
            shader = gpu.shader.from_builtin('SMOOTH_COLOR')
            batch = batch_for_shader(
                shader, 'TRIS', {"pos": geometry.positions, "color": _shade(geometry, view.right, view.up)},
            )
        else:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            batch = batch_for_shader(shader, 'TRIS', {"pos": geometry.positions})
        batch.draw(shader)
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)


def _gpu_state_save() -> tuple[Any, ...]:
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


def _model_mask(geometry: _Geometry, view: _View) -> _Array:
    """
    Return where the model covers the view, as a ``(height, width)`` bool array (rows from the bottom up).
    """
    import gpu  # pylint: disable=import-error,no-name-in-module
    import numpy as np  # pylint: disable=import-error

    state = _gpu_state_save()
    offscreen = gpu.types.GPUOffScreen(view.width, view.height, format='RGBA8')
    try:
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            framebuffer.clear(color=(0.0, 0.0, 0.0, 0.0), depth=1.0)
            _draw_model(geometry, view, 0, 0, shaded=False)
            depth = framebuffer.read_depth(0, 0, view.width, view.height)
    finally:
        offscreen.free()
        _gpu_state_restore(state)
    return np.asarray(depth, dtype=np.float32).reshape(view.height, view.width) < 1.0


def _clip_to_rect(mask: _Array, view: _View, rect: tuple[float, float, float, float] | None) -> _Array:
    """
    Clear *mask* outside *rect* (screen axes, object units), unchanged when *rect* is None.
    """
    import numpy as np  # pylint: disable=import-error

    if rect is None:
        return mask
    xs = view.center[0] + (np.arange(view.width) + 0.5 - view.width / 2.0) * view.units_per_pixel
    ys = view.center[1] + (np.arange(view.height) + 0.5 - view.height / 2.0) * view.units_per_pixel
    inside_x = (rect[0] <= xs) & (xs <= rect[2])
    inside_y = (rect[1] <= ys) & (ys <= rect[3])
    return mask & inside_y[:, None] & inside_x[None, :]


def _mask_extent(mask: _Array) -> tuple[int, int, int, int] | None:
    """
    Return the pixel extent ``(x0, y0, x1, y1)`` (end exclusive) of *mask*, None when empty.
    """
    import numpy as np  # pylint: disable=import-error

    cols = np.flatnonzero(mask.any(axis=0))
    rows = np.flatnonzero(mask.any(axis=1))
    if not len(cols):
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _outline(mask: _Array) -> _Array:
    """
    Return the outline of *mask*, ``_OUTLINE_WIDTH`` pixels wide on its inside.
    """
    import numpy as np  # pylint: disable=import-error

    r = _OUTLINE_WIDTH
    padded = np.pad(mask, r, mode='constant', constant_values=False)
    height, width = mask.shape
    inner = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy > r * r:
                continue
            inner &= padded[r + dy:r + dy + height, r + dx:r + dx + width]
    return mask & ~inner


def _load_reference(filepath: str) -> tuple[_Array, bool]:
    """
    Return the image at *filepath* as a ``(height, width, 4)`` float32 array with premultiplied alpha
    (rows from the bottom up), and whether it has transparent pixels.
    """
    import imbuf  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    import numpy as np  # pylint: disable=import-error

    im = imbuf.load(filepath)
    try:
        if im.buffer_type != 'BYTE':
            im.convert_buffer_type('BYTE')
        with im.with_buffer() as buf:
            pixels = np.asarray(buf, dtype=np.uint8).copy()
    finally:
        im.free()
    if pixels.shape[2] < 4:
        alpha = np.full(pixels.shape[:2] + (1,), 255, dtype=np.uint8)
        pixels = np.concatenate([pixels[:, :, :3], alpha], axis=2)
    image = pixels.astype(np.float32) / 255.0
    image[:, :, :3] *= image[:, :, 3:4]
    has_alpha = bool((image[:, :, 3] < _ALPHA_THRESHOLD).any())
    return image, has_alpha


def _sample_reference(
        image: _Array,
        region: tuple[float, float, float, float],
        width: int,
        height: int,
) -> _Array:
    """
    Return the *region* ``(x0, y0, x1, y1)`` of *image* (pixels, from the bottom left)
    resampled to *width* x *height*, premultiplied RGBA. Outside the image is transparent.
    """
    import numpy as np  # pylint: disable=import-error

    image_height, image_width = image.shape[:2]
    x0, y0, x1, y1 = region
    reference_per_pixel = (x1 - x0) / width
    # Average blocks of pixels first when shrinking, so thin features don't flicker.
    factor = max(1, int(reference_per_pixel))
    # Only the part of the image in the region is reduced.
    cx0 = max(0, int(np.floor(x0)) // factor * factor)
    cy0 = max(0, int(np.floor(y0)) // factor * factor)
    cx1 = min(image_width, int(np.ceil(x1)))
    cy1 = min(image_height, int(np.ceil(y1)))
    result = np.zeros((height, width, 4), dtype=np.float32)
    if cx1 - cx0 < factor or cy1 - cy0 < factor:
        return result
    crop = image[cy0:cy1, cx0:cx1]
    crop = crop[:crop.shape[0] // factor * factor, :crop.shape[1] // factor * factor]
    if factor > 1:
        crop = crop.reshape(crop.shape[0] // factor, factor, crop.shape[1] // factor, factor, 4).mean(axis=(1, 3))
    # The reduced pixel under the center of each output pixel.
    xs = (x0 + (np.arange(width) + 0.5) * reference_per_pixel - cx0) / factor
    ys = (y0 + (np.arange(height) + 0.5) * reference_per_pixel - cy0) / factor
    ix = np.floor(xs).astype(np.int64)
    iy = np.floor(ys).astype(np.int64)
    valid_x = (0 <= ix) & (ix < crop.shape[1])
    valid_y = (0 <= iy) & (iy < crop.shape[0])
    sampled = crop[np.clip(iy, 0, crop.shape[0] - 1)][:, np.clip(ix, 0, crop.shape[1] - 1)]
    valid = valid_y[:, None] & valid_x[None, :]
    result[valid] = sampled[valid]
    return result


def _band_fractions(
        model: _Array,
        reference: _Array,
        view: _View,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Split the extent of both silhouettes in bands along each screen axis, return per band
    the parts only in the model and only in the reference (fractions of the reference's area),
    rows from the top down and columns from the left to the right.
    Each band has its ``range`` along the axis of ``up`` (rows) or ``right`` (columns),
    in object coordinates.
    """
    import numpy as np  # pylint: disable=import-error

    extent = _mask_extent(model | reference)
    reference_area = float(reference.sum())
    if extent is None or reference_area == 0.0:
        return [], []
    x0, y0, x1, y1 = extent
    model_only = model & ~reference
    reference_only = reference & ~model

    def coordinate(pixel: float, axis: int) -> float:
        """
        Return the object coordinate at *pixel* along the screen *axis* (0: right, 1: up).
        """
        size = view.width if axis == 0 else view.height
        sign = (view.right, view.up)[axis][1]
        return (view.center[axis] + (pixel - size / 2.0) * view.units_per_pixel) * sign

    def bands(lo: int, hi: int, axis: int, top_down: bool) -> list[dict[str, Any]]:
        edges = np.rint(np.linspace(lo, hi, _BANDS + 1)).astype(np.int64).tolist()
        result = []
        for i in range(_BANDS):
            a, b = edges[i], edges[i + 1]
            if axis == 0:
                m, r = model_only[:, a:b], reference_only[:, a:b]
            else:
                m, r = model_only[a:b], reference_only[a:b]
            result.append({
                "range": sorted([round(coordinate(a, axis), 6), round(coordinate(b, axis), 6)]),
                "model_only": round(float(m.sum()) / reference_area, 4),
                "reference_only": round(float(r.sum()) / reference_area, 4),
            })
        return result[::-1] if top_down else result

    return bands(y0, y1, 1, True), bands(x0, x1, 0, False)


def _draw_titles(titles: list[tuple[str, int]], image_width: int, panel_height: int) -> None:
    """
    Draw each title at its x position above the panels, in image pixel space.
    """
    import blf  # pylint: disable=import-error,no-name-in-module
    import gpu  # pylint: disable=import-error,no-name-in-module
    from mathutils import Matrix  # pylint: disable=import-error,no-name-in-module

    image_height = panel_height + _PANEL_TITLE_HEIGHT
    gpu.state.viewport_set(0, 0, image_width, image_height)
    with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
        gpu.matrix.load_matrix(Matrix.Identity(4))
        gpu.matrix.load_projection_matrix(
            Matrix.Translation((-1.0, -1.0, 0.0)) @ Matrix.Diagonal((2.0 / image_width, 2.0 / image_height, 1.0, 1.0))
        )
        font_id = 0
        blf.size(font_id, _FONT_SIZE)
        blf.color(font_id, *_COLOR_TITLE, 1.0)
        for text, x in titles:
            blf.position(font_id, x + 8, panel_height + 8, 0)
            blf.draw(font_id, text)


def _blend(pixels: _Array, mask: _Array, color: tuple[float, float, float], opacity: float = 1.0) -> None:
    import numpy as np  # pylint: disable=import-error

    pixels[mask] = pixels[mask] * (1.0 - opacity) + np.array(color, dtype=np.float32) * opacity


def _encode_jpeg(rgba: _Array, filepath: str, size_limit_in_bytes: int) -> tuple[bytes, int, int]:
    """
    Encode the ``(height, width, 4)`` uint8 array *rgba* (rows from the bottom up) as a JPEG,
    downscaled to stay under *size_limit_in_bytes* when needed.

    Return the file contents and the size of the encoded image.
    """
    import math

    import imbuf  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module

    height, width = rgba.shape[:2]
    im = imbuf.new((width, height))
    try:
        with im.with_buffer(write=True) as buf:
            buf.cast('B')[:] = rgba.tobytes()
        im.file_type = 'JPEG'
        im.quality = _JPEG_QUALITY
        while True:
            imbuf.write(im, filepath=filepath)
            with open(filepath, "rb") as fh:
                data = fh.read()
            if len(data) <= size_limit_in_bytes:
                break
            # The size scales roughly with the pixel count, shrink a little more than needed.
            factor = math.sqrt(size_limit_in_bytes / len(data)) * 0.95
            new_width = int(im.size[0] * factor)
            new_height = int(im.size[1] * factor)
            if min(new_width, new_height) < _IMAGE_SIZE_MIN:
                break
            im.resize((new_width, new_height), method='BILINEAR')
        return data, im.size[0], im.size[1]
    finally:
        im.free()


def _parse_box(
        subject_box: list[float],
        image_height: int,
) -> tuple[float, float, float, float] | str:
    """
    Convert *subject_box* (left, top, right, bottom from the top left) to ``(x0, y0, x1, y1)``
    from the bottom left, or return an error message.
    """
    import math

    if len(subject_box) != 4 or not all(math.isfinite(v) for v in subject_box):
        return "subject_box must be 4 finite numbers (left, top, right, bottom)"
    left, top, right, bottom = subject_box
    if right <= left or bottom <= top:
        return "subject_box must have right > left and bottom > top (pixels from the top left)"
    return left, image_height - bottom, right, image_height - top


def main(params: Params) -> Result:
    import base64
    import math
    import os
    import tempfile

    import bpy  # pylint: disable=import-error,no-name-in-module
    import numpy as np  # pylint: disable=import-error

    obj = bpy.data.objects.get(params.name)
    if obj is None:
        return Result(status="error", message="Object {!r} not found".format(params.name))
    if obj.type != 'MESH':
        return Result(status="error", message="Object {!r} is a {:s}, not a mesh".format(obj.name, obj.type))
    if params.view not in _VIEW_AXES:
        return Result(status="error", message="Unknown view {!r}".format(params.view))
    if params.fit not in ("HEIGHT", "WIDTH"):
        return Result(status="error", message="Unknown fit {!r}, use HEIGHT or WIDTH".format(params.fit))
    if (params.bounds_min is None) != (params.bounds_max is None):
        return Result(status="error", message="Pass both bounds_min and bounds_max, or neither")
    for bounds in (params.bounds_min, params.bounds_max):
        if bounds is not None and (len(bounds) != 3 or not all(math.isfinite(v) for v in bounds)):
            return Result(status="error", message="bounds_min and bounds_max must be 3 finite numbers (X, Y, Z)")
    image_path = os.path.expanduser(params.image_path)
    if not os.path.isabs(image_path):
        return Result(status="error", message="image_path must be an absolute path: {!r}".format(params.image_path))
    if not os.path.isfile(image_path):
        return Result(status="error", message="Image not found: {!r}".format(params.image_path))

    try:
        reference, has_alpha = _load_reference(image_path)
    except Exception as ex:
        return Result(status="error", message="Can't read the image {!r}: {:s}".format(params.image_path, str(ex)))
    reference_height, reference_width = reference.shape[:2]

    # The subject in the reference, pixels from the bottom left.
    reference_mask_full = reference[:, :, 3] >= _ALPHA_THRESHOLD
    if params.subject_box is not None:
        box_or_error = _parse_box(params.subject_box, reference_height)
        if isinstance(box_or_error, str):
            return Result(status="error", message=box_or_error)
        box = box_or_error
        if box[2] <= 0 or box[0] >= reference_width or box[3] <= 0 or box[1] >= reference_height:
            return Result(status="error", message="subject_box is outside the image ({:d} x {:d} pixels)".format(
                reference_width, reference_height))
    elif has_alpha:
        alpha_extent = _mask_extent(reference_mask_full)
        if alpha_extent is None:
            return Result(status="error", message="The image is fully transparent")
        box = tuple(float(v) for v in alpha_extent)  # type: ignore[assignment]
    else:
        return Result(
            status="error",
            message="Pass subject_box: the image has no transparent background to find the subject in",
        )

    right, up = _VIEW_AXES[params.view]
    geometry = _read_geometry(obj)
    if not len(geometry.positions):
        return Result(status="error", message="Object {!r} has no faces".format(obj.name))
    positions = geometry.positions
    depth_axis = 3 - right[0] - up[0]
    depth_sign = _depth(_axis_vector((depth_axis, 1)), right, up)

    # The region of the model drawn, on the screen axes (object units).
    bounds_rect = None
    if params.bounds_min is not None and params.bounds_max is not None:
        bounds_min = [min(a, b) for a, b in zip(params.bounds_min, params.bounds_max)]
        bounds_max = [max(a, b) for a, b in zip(params.bounds_min, params.bounds_max)]
        rect_lo = [min(_screen(bounds_min, a), _screen(bounds_max, a)) for a in (right, up)]
        rect_hi = [max(_screen(bounds_min, a), _screen(bounds_max, a)) for a in (right, up)]
        bounds_rect = (rect_lo[0], rect_lo[1], rect_hi[0], rect_hi[1])
        depths = sorted(d * depth_sign for d in (bounds_min[depth_axis], bounds_max[depth_axis]))
    else:
        bounds_min = positions.min(axis=0).tolist()
        bounds_max = positions.max(axis=0).tolist()
        rect_lo = [float((positions[:, a] * s).min()) for a, s in (right, up)]
        rect_hi = [float((positions[:, a] * s).max()) for a, s in (right, up)]
        depth_values = positions[:, depth_axis] * depth_sign
        depths = [float(depth_values.min()), float(depth_values.max())]
    depth_pad = max(depths[1] - depths[0], 1e-6) * 0.01
    depth_range = (depths[0] - depth_pad, depths[1] + depth_pad)
    rect_size = [max(hi - lo, 1e-6) for lo, hi in zip(rect_lo, rect_hi)]

    _gpu_ensure()

    # Measure the model's silhouette in the region.
    measure_units_per_pixel = max(rect_size) * (1.0 + 2.0 * _MARGIN) / _MEASURE_SIZE
    measure_view = _View(
        right, up,
        ((rect_lo[0] + rect_hi[0]) / 2.0, (rect_lo[1] + rect_hi[1]) / 2.0),
        measure_units_per_pixel,
        max(1, math.ceil(rect_size[0] * (1.0 + 2.0 * _MARGIN) / measure_units_per_pixel)),
        max(1, math.ceil(rect_size[1] * (1.0 + 2.0 * _MARGIN) / measure_units_per_pixel)),
        depth_range,
    )
    measure_mask = _clip_to_rect(_model_mask(geometry, measure_view), measure_view, bounds_rect)
    measure_extent = _mask_extent(measure_mask)
    if measure_extent is None:
        return Result(status="error", message="Nothing of {!r} is drawn{:s}".format(
            obj.name, " within the bounds" if bounds_rect else ""))
    model_lo = [
        measure_view.center[i] + (measure_extent[i] - (measure_view.width, measure_view.height)[i] / 2.0)
        * measure_units_per_pixel
        for i in range(2)
    ]
    model_hi = [
        measure_view.center[i] + (measure_extent[i + 2] - (measure_view.width, measure_view.height)[i] / 2.0)
        * measure_units_per_pixel
        for i in range(2)
    ]
    model_size = [hi - lo for lo, hi in zip(model_lo, model_hi)]
    model_center = [(lo + hi) / 2.0 for lo, hi in zip(model_lo, model_hi)]

    # Fit the model to the subject: the same size along the fitted axis, centers aligned.
    box_size = [box[2] - box[0], box[3] - box[1]]
    box_center = [(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0]
    fit_axis = 1 if params.fit == "HEIGHT" else 0
    units_per_reference_pixel = model_size[fit_axis] / box_size[fit_axis]

    # The region shown, in reference pixels: the subject and the model, with room around them.
    model_box_lo = [box_center[i] - model_size[i] / units_per_reference_pixel / 2.0 for i in range(2)]
    model_box_hi = [box_center[i] + model_size[i] / units_per_reference_pixel / 2.0 for i in range(2)]
    region_lo = [min(box[i], model_box_lo[i]) for i in range(2)]
    region_hi = [max(box[i + 2], model_box_hi[i]) for i in range(2)]
    margin = max(hi - lo for lo, hi in zip(region_lo, region_hi)) * _MARGIN
    region_lo = [v - margin for v in region_lo]
    region_hi = [v + margin for v in region_hi]
    region_size = [hi - lo for lo, hi in zip(region_lo, region_hi)]

    pixels_per_reference_pixel = min(_PANEL_WIDTH_MAX / region_size[0], _PANEL_HEIGHT_MAX / region_size[1])
    panel_width = max(1, round(region_size[0] * pixels_per_reference_pixel))
    panel_height = max(1, round(region_size[1] * pixels_per_reference_pixel))
    # Make the region match the rounded panel size exactly.
    reference_per_pixel = 1.0 / pixels_per_reference_pixel
    region_center = [(lo + hi) / 2.0 for lo, hi in zip(region_lo, region_hi)]
    region = (
        region_center[0] - panel_width * reference_per_pixel / 2.0,
        region_center[1] - panel_height * reference_per_pixel / 2.0,
        region_center[0] + panel_width * reference_per_pixel / 2.0,
        region_center[1] + panel_height * reference_per_pixel / 2.0,
    )
    view = _View(
        right, up,
        (
            model_center[0] + (region_center[0] - box_center[0]) * units_per_reference_pixel,
            model_center[1] + (region_center[1] - box_center[1]) * units_per_reference_pixel,
        ),
        units_per_reference_pixel * reference_per_pixel,
        panel_width,
        panel_height,
        depth_range,
    )

    # The reference panel: the image over the background.
    sampled = _sample_reference(reference, region, panel_width, panel_height)
    background = np.array(_COLOR_BACKGROUND, dtype=np.float32)
    reference_rgb = sampled[:, :, :3] + background * (1.0 - sampled[:, :, 3:4])
    reference_mask = sampled[:, :, 3] >= _ALPHA_THRESHOLD if has_alpha else None

    # The model panel, drawn under the titles.
    image_width = panel_width * 2
    image_height = panel_height + _PANEL_TITLE_HEIGHT
    title_view = "{:s} (right: {:s}, up: {:s})".format(params.view, _axis_label(right), _axis_label(up))
    titles = [
        (
            "REFERENCE  yellow: model outline" + ("  red: only model  blue: only reference" if has_alpha else ""),
            0,
        ),
        ("MODEL  " + title_view + ("  cyan: reference outline" if has_alpha else ""), panel_width),
    ]
    import gpu  # pylint: disable=import-error,no-name-in-module
    state = _gpu_state_save()
    offscreen = gpu.types.GPUOffScreen(image_width, image_height, format='RGBA8')
    try:
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            framebuffer.clear(color=(*_COLOR_BACKGROUND, 1.0), depth=1.0)
            _draw_model(geometry, view, panel_width, 0, shaded=True)
            depth = framebuffer.read_depth(panel_width, 0, panel_width, panel_height)
            gpu.state.blend_set('ALPHA')
            _draw_titles(titles, image_width, panel_height)
            gpu.state.blend_set('NONE')
            color = framebuffer.read_color(0, 0, image_width, image_height, 4, 0, 'UBYTE')
    finally:
        offscreen.free()
        _gpu_state_restore(state)
    color.dimensions = image_width * image_height * 4
    pixels = np.frombuffer(bytes(color), dtype=np.uint8).reshape(image_height, image_width, 4)
    pixels = pixels[:, :, :3].astype(np.float32) / 255.0
    model_mask = _clip_to_rect(
        np.asarray(depth, dtype=np.float32).reshape(panel_height, panel_width) < 1.0, view, bounds_rect,
    )

    model_panel = pixels[:panel_height, panel_width:]
    model_panel[~model_mask] = background
    if reference_mask is not None:
        _blend(model_panel, _outline(reference_mask), _COLOR_REFERENCE_OUTLINE)
        _blend(reference_rgb, model_mask & ~reference_mask, _COLOR_MODEL_ONLY, _DIFFERENCE_OPACITY)
        _blend(reference_rgb, reference_mask & ~model_mask, _COLOR_REFERENCE_ONLY, _DIFFERENCE_OPACITY)
    _blend(reference_rgb, _outline(model_mask), _COLOR_MODEL_OUTLINE)
    pixels[:panel_height, :panel_width] = reference_rgb
    # The border between the panels and below the titles.
    pixels[:panel_height, panel_width - 1:panel_width + 1] = _COLOR_PANEL_BORDER
    pixels[panel_height:panel_height + 1] = _COLOR_PANEL_BORDER

    rgba = np.empty((image_height, image_width, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(pixels * 255.0 + 0.5, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = 255

    size_limit = params.size_limit_in_bytes if params.size_limit_in_bytes > 0 else _IMAGE_SIZE_LIMIT_IN_BYTES
    with tempfile.TemporaryDirectory(prefix="blmcp_reference_") as tmpdir:
        image_data, encoded_width, image_height = _encode_jpeg(
            rgba, os.path.join(tmpdir, "comparison.jpg"), size_limit,
        )

    # The returned image may have been downscaled to fit the size limit, scale the layout to match.
    scale = encoded_width / image_width
    image_width = encoded_width

    metrics: dict[str, Any] = {}
    if reference_mask is not None:
        union = float((model_mask | reference_mask).sum())
        reference_area = float(reference_mask.sum())
        if union > 0.0 and reference_area > 0.0:
            rows, columns = _band_fractions(model_mask, reference_mask, view)
            metrics = {
                "overlap": round(float((model_mask & reference_mask).sum()) / union, 4),
                "model_only": round(float((model_mask & ~reference_mask).sum()) / reference_area, 4),
                "reference_only": round(float((reference_mask & ~model_mask).sum()) / reference_area, 4),
                "rows": rows,
                "columns": columns,
            }

    subject_size = [v * units_per_reference_pixel for v in box_size]
    return Result(
        status="ok",
        image_base64=base64.b64encode(image_data).decode("ascii"),
        image_format="jpeg",
        image_width=image_width,
        image_height=image_height,
        view=params.view,
        right=_axis_label(right),
        up=_axis_label(up),
        panels=[
            {"content": content, "rect": [round(v * scale) for v in (x, 0, panel_width, panel_height)]}
            for content, x in (("reference", 0), ("model", panel_width))
        ],
        units_per_pixel=round(view.units_per_pixel / scale, 8),
        objects=geometry.objects,
        reference_size=[reference_width, reference_height],
        reference_has_alpha=has_alpha,
        subject_box=[
            round(v, 1) for v in (box[0], reference_height - box[3], box[2], reference_height - box[1])
        ],
        subject_box_from_alpha=params.subject_box is None,
        fit=params.fit,
        units_per_reference_pixel=round(units_per_reference_pixel, 8),
        model_size=[round(v, 6) for v in model_size],
        subject_size=[round(v, 6) for v in subject_size],
        size_ratio=[round(m / s, 4) for m, s in zip(model_size, subject_size)],
        bounds_min=[round(v, 6) for v in bounds_min],
        bounds_max=[round(v, 6) for v in bounds_max],
        **metrics,
    )
