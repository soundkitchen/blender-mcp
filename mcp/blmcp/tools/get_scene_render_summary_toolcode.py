# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tool-code for returning the render settings, color management, world, lights
and camera of the current scene.
"""

__all__ = (
    "Result",
    "main",
)

from typing import Any, NamedTuple

# Placeholder types for Blender Python objects (no stubs available).
_Object = Any
_Scene = Any
_World = Any


class Result(NamedTuple):
    status: str
    scene_name: str
    render: dict[str, Any]
    color_management: dict[str, Any]
    world: dict[str, Any] | None
    lights: list[dict[str, Any]]
    camera: dict[str, Any] | None


def _engine_settings(scene: _Scene) -> tuple[int | None, dict[str, Any]]:
    """
    Return the final render sample count and the settings of the active engine only.
    """
    engine = scene.render.engine
    # NOTE: keep EEVEE engine IDs in sync with `_template_render_thumbnail_overrides.py`.
    if engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        eevee = scene.eevee
        return eevee.taa_render_samples, {
            "taa_render_samples": eevee.taa_render_samples,
            "use_raytracing": eevee.use_raytracing,
            "use_shadows": eevee.use_shadows,
            "use_volumetric_shadows": eevee.use_volumetric_shadows,
        }
    if engine == 'CYCLES':
        # Defined by the Cycles add-on, missing when it is disabled.
        cycles = getattr(scene, "cycles", None)
        if cycles is None:
            return None, {}
        return cycles.samples, {
            "samples": cycles.samples,
            "use_adaptive_sampling": cycles.use_adaptive_sampling,
            "adaptive_threshold": cycles.adaptive_threshold,
            "use_denoising": cycles.use_denoising,
            "denoiser": cycles.denoiser,
            "device": cycles.device,
            "max_bounces": cycles.max_bounces,
            "time_limit": cycles.time_limit,
        }
    if engine == 'BLENDER_WORKBENCH':
        display = scene.display
        # `render_aa` is an enum ('OFF', 'FXAA', '5', '8', ...), not a sample count.
        return None, {
            "render_aa": display.render_aa,
            "lighting": display.shading.light,
            "color_type": display.shading.color_type,
        }
    return None, {}


def _render_info(scene: _Scene) -> dict[str, Any]:
    rd = scene.render
    samples, engine_settings = _engine_settings(scene)
    return {
        "engine": rd.engine,
        "samples": samples,
        "engine_settings": engine_settings,
        "resolution": [rd.resolution_x, rd.resolution_y],
        "resolution_percentage": rd.resolution_percentage,
        "fps": rd.fps / rd.fps_base,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "film_transparent": rd.film_transparent,
        "use_motion_blur": rd.use_motion_blur,
    }


def _color_management_info(scene: _Scene) -> dict[str, Any]:
    view = scene.view_settings
    return {
        "display_device": scene.display_settings.display_device,
        "view_transform": view.view_transform,
        "look": view.look,
        "exposure": view.exposure,
        "gamma": view.gamma,
        "use_curve_mapping": view.use_curve_mapping,
    }


def _active_links(socket: Any) -> list[Any]:
    # Muted links pass nothing, the socket then uses its own value.
    return [link for link in socket.links if not link.is_muted]


def _linked_from(socket: Any) -> str | None:
    """
    Return the name of the node feeding *socket*, skipping Reroute nodes.
    """
    seen = set()
    while True:
        links = _active_links(socket)
        if not links:
            return None
        node = links[0].from_node
        if node.bl_idname != "NodeReroute":
            return node.name
        # Guard against Reroute cycles.
        if node.name in seen:
            return None
        seen.add(node.name)
        socket = node.inputs[0]


def _world_output_target(engine: str) -> str:
    """
    Return the ``get_output_node`` target for *engine*.

    An output targeting the engine takes precedence over one targeting all engines,
    which is used as a fallback.
    """
    if engine == 'CYCLES':
        return 'CYCLES'
    # NOTE: keep EEVEE engine IDs in sync with `_template_render_thumbnail_overrides.py`.
    if engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        return 'EEVEE'
    return 'ALL'


def _world_info(world: _World | None, engine: str) -> dict[str, Any] | None:
    if world is None:
        return None

    background: list[dict[str, Any]] = []
    environment_textures: list[dict[str, Any]] = []
    tree = world.node_tree
    if tree is not None:
        # Collect the Background nodes that feed the output used by the engine,
        # following links upstream (e.g. through Mix Shader nodes).
        output = tree.get_output_node(_world_output_target(engine))
        if output is not None:
            seen = set()
            stack = [output]
            while stack:
                node = stack.pop()
                if node.name in seen:
                    continue
                seen.add(node.name)
                if node.bl_idname == "ShaderNodeBackground":
                    color = node.inputs["Color"]
                    strength = node.inputs["Strength"]
                    background.append({
                        "node": node.name,
                        "color": list(color.default_value)[:3],
                        "color_linked_from": _linked_from(color),
                        "strength": strength.default_value,
                        "strength_linked_from": _linked_from(strength),
                    })
                for socket in node.inputs:
                    for link in _active_links(socket):
                        stack.append(link.from_node)
            background.sort(key=lambda b: b["node"])

        for node in tree.nodes:
            if node.bl_idname != "ShaderNodeTexEnvironment":
                continue
            image = node.image
            environment_textures.append({
                "node": node.name,
                "image": image.name if image else None,
                "filepath": image.filepath if image else None,
            })
        environment_textures.sort(key=lambda t: t["node"])

    return {
        "name": world.name,
        "background": background,
        "environment_textures": environment_textures,
    }


def _forward(obj: _Object) -> list[float]:
    # Lights and cameras point along their local -Z axis.
    from mathutils import Vector  # pylint: disable=import-error,no-name-in-module
    return list((obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized())


def _light_info(obj: _Object) -> dict[str, Any]:
    light = obj.data
    info: dict[str, Any] = {
        "name": obj.name,
        "type": light.type,
        "energy": light.energy,
        "color": list(light.color),
    }
    # Recent additions, guard in case an older supported version lacks them.
    if hasattr(light, "use_temperature"):
        info["use_temperature"] = light.use_temperature
        info["temperature"] = light.temperature
    if hasattr(light, "exposure"):
        info["exposure"] = light.exposure

    if light.type in {'POINT', 'SPOT'}:
        info["shadow_soft_size"] = light.shadow_soft_size
    if light.type == 'SPOT':
        info["spot_size"] = light.spot_size
        info["spot_blend"] = light.spot_blend
    elif light.type == 'AREA':
        info["shape"] = light.shape
        info["size"] = light.size
        if light.shape in {'RECTANGLE', 'ELLIPSE'}:
            info["size_y"] = light.size_y
    elif light.type == 'SUN':
        info["angle"] = light.angle

    info["location"] = list(obj.matrix_world.translation)
    # A point light has no direction.
    info["direction"] = None if light.type == 'POINT' else _forward(obj)
    info["hide_render"] = obj.hide_render
    info["visible"] = obj.visible_get()
    return info


def _camera_info(obj: _Object | None) -> dict[str, Any] | None:
    if obj is None:
        return None

    info: dict[str, Any] = {"name": obj.name}
    # `scene.camera` may be any object type (e.g. an empty), only cameras have lens data.
    if obj.type == 'CAMERA':
        cam = obj.data
        dof = cam.dof
        info.update({
            "type": cam.type,
            "lens": cam.lens,
            "lens_unit": cam.lens_unit,
            "angle": cam.angle,
            "sensor_fit": cam.sensor_fit,
            "sensor_width": cam.sensor_width,
            "sensor_height": cam.sensor_height,
            "ortho_scale": cam.ortho_scale,
            "clip_start": cam.clip_start,
            "clip_end": cam.clip_end,
            "shift": [cam.shift_x, cam.shift_y],
            "dof": {
                "use_dof": dof.use_dof,
                "focus_object": dof.focus_object.name if dof.focus_object else None,
                "focus_distance": dof.focus_distance,
                "aperture_fstop": dof.aperture_fstop,
            },
        })
    else:
        info["type"] = None
    info["location"] = list(obj.matrix_world.translation)
    info["direction"] = _forward(obj)
    return info


def main(params: None) -> Result:
    del params
    from bpy import context  # pylint: disable=import-error,no-name-in-module

    scene = context.scene
    lights = sorted(
        [_light_info(obj) for obj in scene.objects if obj.type == 'LIGHT'],
        key=lambda light: light["name"],
    )
    return Result(
        status="ok",
        scene_name=scene.name,
        render=_render_info(scene),
        color_management=_color_management_info(scene),
        world=_world_info(scene.world, scene.render.engine),
        lights=lights,
        camera=_camera_info(scene.camera),
    )
