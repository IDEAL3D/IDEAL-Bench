#!/usr/bin/env python3
"""
Blender batch helper: write render + environment JSON from the currently opened scene.
Used by ``metadata_builder.resolve_scene_metadata`` / ``build_metadata`` via a headless Blender subprocess.

Usage::

    blender -b /path/to/scene.blend --python blender_dump_metadata.py -- <out.json> <room_type>

Output JSON: ``{"render": {...}, "environment": {...}}``. ``render`` contains only base fields
(resolution, camera_location, camera_rotation_euler, focal_length_mm, sensor_width); the caller
adds rotation_unit, rotation_order, camera_height, and camera_distance_to_center.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


def _is_portal_light(light) -> bool:
    cyc = getattr(light, "cycles", None)
    if cyc is None:
        return False
    return bool(getattr(cyc, "is_portal", False))


def _collect_lighting() -> dict:
    light_count = 0
    has_window = False
    for light in bpy.data.lights:
        if _is_portal_light(light):
            has_window = True
        elif light.type == "AREA":
            light_count += 1
    return {"light_count": light_count, "has_window": has_window}


def _principled_surface_params(mat) -> dict | None:
    if not mat or not mat.use_nodes:
        return None
    nt = mat.node_tree
    if nt is None:
        return None
    principled = None
    for node in nt.nodes:
        if getattr(node, "bl_idname", "") == "ShaderNodeBsdfPrincipled":
            principled = node
            break
        if getattr(node, "type", "") == "BSDF_PRINCIPLED":
            principled = node
            break
    if principled is None:
        return None
    inp = principled.inputs
    try:
        bc = inp["Base Color"].default_value
    except (KeyError, IndexError):
        bc = inp[0].default_value
    base_color = [float(bc[i]) for i in range(min(4, len(bc)))]
    while len(base_color) < 4:
        base_color.append(1.0)

    def _fslot(names: tuple[str, ...], default: float = 0.0) -> float:
        for name in names:
            if name in inp:
                v = inp[name].default_value
                return float(v if isinstance(v, (int, float)) else v[0])
        return default

    roughness = _fslot(("Roughness",), 0.5)
    metallic = _fslot(("Metallic",), 0.0)
    specular = _fslot(
        ("Specular IOR Level", "Specular", "IOR Level"),
        0.5,
    )
    return {
        "base_color": base_color,
        "roughness": roughness,
        "metallic": metallic,
        "specular": specular,
    }


def _first_principled_for_name_substr(substr: str) -> dict | None:
    sub = substr.lower()
    for obj in bpy.data.objects:
        if sub not in obj.name.lower():
            continue
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            params = _principled_surface_params(mat)
            if params is not None:
                return params
    return None


def _collect_materials() -> dict:
    floor_p = _first_principled_for_name_substr("floor")
    wall_p = _first_principled_for_name_substr("wall")
    return {"floor": floor_p, "wall": wall_p}


def _collect_has_mirror() -> bool:
    return any("mirror" in o.name.lower() for o in bpy.data.objects)


def collect_environment(room_type: str) -> dict:
    return {
        "room_type": room_type,
        "lighting": _collect_lighting(),
        "materials": _collect_materials(),
        "has_mirror": _collect_has_mirror(),
    }


def collect_render_base(cam, scene) -> dict:
    loc, rot_quat, _scale = cam.matrix_world.decompose()
    euler = rot_quat.to_euler("XYZ")
    return {
        "resolution": [
            int(scene.render.resolution_x),
            int(scene.render.resolution_y),
        ],
        "camera_location": [float(loc.x), float(loc.y), float(loc.z)],
        "camera_rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
        "focal_length_mm": float(cam.data.lens),
        "sensor_width": float(cam.data.sensor_width),
    }


def main() -> None:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    if len(argv) < 1:
        print(
            "Usage: blender -b scene.blend --python blender_dump_metadata.py -- <out.json> [room_type]",
            file=sys.stderr,
        )
        sys.exit(1)
    out_path = Path(argv[0])
    room_type = argv[1] if len(argv) > 1 else "unknown"

    cam = bpy.context.scene.camera
    if cam is None:
        raise RuntimeError("No active camera in scene")

    scene = bpy.context.scene
    payload = {
        "render": collect_render_base(cam, scene),
        "environment": collect_environment(room_type),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
