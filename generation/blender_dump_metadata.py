#!/usr/bin/env python3
"""
在 Blender 内运行：从已打开的 scene 写出相机与分辨率 JSON（供 generate_box 写 metadata.json）。

用法::
    blender -b /path/to/scene.blend --python blender_dump_metadata.py -- /path/to/camera_fragment.json

写出为 JSON 对象，仅含键：resolution, camera_location, camera_rotation_euler, focal_length_mm, sensor_width
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
    euler = rot_quat.to_euler()
    fragment = {
        "resolution": [
            int(bpy.context.scene.render.resolution_x),
            int(bpy.context.scene.render.resolution_y),
        ],
        "camera_location": [float(loc.x), float(loc.y), float(loc.z)],
        "camera_rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
        "focal_length_mm": float(cam.data.lens),
        "sensor_width": float(cam.data.sensor_width),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fragment, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
