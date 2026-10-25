# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=C0114  # See tool doc-string.

__all__ = (
    "register",
)

from blmcp.tools_helpers import (
    toolcode_format_call,
    toolcode_load_from_filepath,
    toolcode_wrap_with_calling_convention,
)
from blmcp.tools_helpers.connection import send_code
from blmcp.tools_helpers.render_image import render_response_as_image_and_info
from blmcp.tools.render_reference_comparison_as_image_toolcode import FitName, Params, ViewName
from mcp.server.mcpserver import MCPServer, Image  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

_TOOL_CALL = toolcode_wrap_with_calling_convention(toolcode_load_from_filepath(__file__))


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Render Reference Comparison as Image",
            read_only_hint=True,
        ),
        # The result is unstructured (image + JSON text), see `render_viewport_as_image`.
        structured_output=False,
    )
    def render_reference_comparison_as_image(
        name: str,
        image_path: str,
        view: ViewName,
        subject_box: list[float] | None = None,
        fit: FitName = "HEIGHT",
        bounds_min: list[float] | None = None,
        bounds_max: list[float] | None = None,
        size_limit_in_bytes: int = 0,
    ) -> tuple[Image, str]:
        """
        Compare the mesh object *name* with a reference image of the subject, returned as a JPEG image.

        Use to check a model's shape against a photo or drawing: the widths along each part,
        how parts join the body, the shape of the head. The model is drawn after modifiers,
        with its visible mesh children (e.g. eyes), in an orthographic *view* in object space
        (``FRONT``, ``BACK``, ``RIGHT``, ``LEFT``, ``TOP``, ``BOTTOM`` as in Blender's numpad
        views), scaled and placed onto the subject in the reference.

        The image has two panels at the same scale and region: the reference with the model's
        outline (yellow) on it, and the shaded model. Pick the reference taken most straight
        on from the view's side: references are usually in perspective, so expect small
        differences, mostly in the parts closest to and furthest from the camera.

        *image_path* is the absolute path of the reference (PNG, JPEG...), read from disk.
        *subject_box* is the subject's box in it, ``[left, top, right, bottom]`` in pixels
        from the top left of the image. It may be left out for a reference with a transparent
        background: the box is then taken from the opaque pixels.

        *fit* picks the size matched between the model and the subject: ``HEIGHT`` (default)
        or ``WIDTH``, e.g. for a subject lying down. Their centers are aligned.

        *bounds_min* & *bounds_max* (object space, pass both) limit the model to a box, to
        compare a part (e.g. the head) with a *subject_box* around the same part.

        With a transparent background, the subject's opaque pixels are compared with the
        model: the reference panel colors the parts only in the model (red) and only in the
        reference (blue), the model panel shows the reference's outline (cyan).

        Returns the image followed by JSON info: ``panels`` (``rect`` in the image: x, y from
        the bottom left, width, height), ``right`` & ``up`` (the screen axes),
        ``units_per_pixel``, ``objects`` drawn, ``subject_box`` used,
        ``units_per_reference_pixel``, ``model_size`` & ``subject_size`` (width and height
        on the screen axes, in object units) and ``size_ratio`` (model / subject, the
        fitted one is 1). With a transparent background also: ``overlap`` (intersection over
        union), ``model_only`` & ``reference_only`` (fractions of the subject's area), and
        ``rows`` (top down) & ``columns`` (left to right): both silhouettes split in 10 bands,
        each with its ``range`` along the ``up`` or ``right`` axis in object coordinates and
        its ``model_only`` & ``reference_only`` parts.

        *size_limit_in_bytes* is the target image size in bytes.
        Zero (the default) uses the MCP message size limit.
        """
        p = Params(
            name=name,
            image_path=image_path,
            view=view,
            subject_box=subject_box,
            fit=fit,
            bounds_min=bounds_min,
            bounds_max=bounds_max,
            size_limit_in_bytes=size_limit_in_bytes,
        )
        response = send_code(toolcode_format_call(_TOOL_CALL, p), strict_json=True)
        return render_response_as_image_and_info(response)
