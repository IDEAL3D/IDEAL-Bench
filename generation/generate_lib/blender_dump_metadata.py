#!/usr/bin/env python3
"""
Blender batch helper: write camera and render-resolution JSON from the currently opened scene.
Used by ``metadata_builder.resolve_render_metadata`` / ``build_metadata`` via a headless Blender subprocess.

Usage::

    blender -b /path/to/scene.blend --python blender_dump_metadata.py -- /path/to/camera_fragment.json

Output JSON fields: resolution, camera_location, camera_rotation_euler, focal_length_mm, sensor_width,
rotation_unit (radians), rotation_order (XYZ). ``camera_rotation_euler`` matches ``rotation_order``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


def main() -> None:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    if not argv:
        print(
            "Usage: blender -b scene.blend --python blender_dump_metadata.py -- <out.json>",
            file=sys.stderr,
        )
        sys.exit(1)
    out_path = Path(argv[0])
    cam = bpy.context.scene.camera
    if cam is None:
        raise RuntimeError("No active camera in scene")

    loc, rot_quat, _scale = cam.matrix_world.decompose()
    euler = rot_quat.to_euler("XYZ")
    fragment = {
        "resolution": [
            int(bpy.context.scene.render.resolution_x),
            int(bpy.context.scene.render.resolution_y),
        ],
        "camera_location": [float(loc.x), float(loc.y), float(loc.z)],
        "camera_rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
        "focal_length_mm": float(cam.data.lens),
        "sensor_width": float(cam.data.sensor_width),
        "rotation_unit": "radians",
        "rotation_order": "XYZ",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fragment, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
