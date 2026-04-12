"""
Filter 3 — Truncation Filter.

Runs inside a bpy process (called as a subprocess by ``inview.py``).

For each candidate gt_id, projects the object's evaluated mesh triangles onto
the camera's image plane (unbounded, no occlusion) via software rasterization,
then computes:

    truncation_ratio = actual_pixels (from instance map) / projected_pixels (mesh silhouette)

Objects below ``TRUNCATION_RATIO_MIN`` are flagged as heavily truncated at the
camera edge and should be excluded from objects_in_view.

CLI (subprocess mode):
    bpy_python -m preprocess_lib.truncation_filter \\
        --blend <scene.blend> \\
        --candidates-json <path_to_candidates.json> \\
        --out-json <result_path.json>

candidates_json schema:
    {
      "scene_id": "...",
      "objects": [
        {"gt_id": "...", "actual_pixels": 1234},
        ...
      ]
    }

result_json schema:
    {
      "scene_id": "...",
      "computed": true,
      "results": [
        {
          "gt_id": "...",
          "actual_pixels": 1234,
          "projected_pixels": 5678,
          "truncation_ratio": 0.217,
          "passed": false
        },
        ...
      ],
      "behind_camera_tris": [
        {
          "gt_id": "...",
          "total_tris": 800,
          "skipped_tris": 12,
          "skipped_ratio": 0.015
        }
      ],
      "error": null
    }
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

_REPO_ROOT = os.path.dirname(_GENERATION_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from .defaults import TRUNCATION_RATIO_MIN


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def _build_camera_matrix(cam_obj) -> Any:
    """Return camera-from-world 4x4 matrix (view matrix)."""
    return cam_obj.matrix_world.inverted()


def _build_intrinsics(cam_obj, render_w: int, render_h: int) -> Tuple[float, float, float, float]:
    """Return (fx, fy, cx, cy) in pixels.

    Uses Blender's camera data directly — more reliable than re-deriving from
    metadata.json focal_length_mm / sensor_width.
    """
    cam = cam_obj.data
    # Blender stores focal length in mm and sensor size in mm.
    # fx = focal_length_mm / sensor_width_mm * image_width_px
    # sensor fit determines which axis focal_length is normalised against.
    sensor_fit = cam.sensor_fit  # 'AUTO', 'HORIZONTAL', 'VERTICAL'
    aspect = render_w / render_h

    if sensor_fit == "VERTICAL" or (sensor_fit == "AUTO" and aspect < 1.0):
        sensor_mm = cam.sensor_height
        fy = (cam.lens / sensor_mm) * render_h
        fx = fy  # square pixels assumed
    else:
        sensor_mm = cam.sensor_width
        fx = (cam.lens / sensor_mm) * render_w
        fy = fx

    cx = render_w / 2.0
    cy = render_h / 2.0
    return fx, fy, cx, cy


# ---------------------------------------------------------------------------
# Mesh projection + rasterization
# ---------------------------------------------------------------------------

def _project_vertex(v_world, view_mat, fx: float, fy: float, cx: float, cy: float):
    """Project a single world-space vertex to (u, v, depth).

    Returns (u, v, depth) where depth > 0 means in front of camera.
    u, v are pixel coordinates (may be outside image bounds — intentional).
    Returns None if the vertex is behind the camera (depth <= 0).
    """
    import mathutils

    v_cam = view_mat @ mathutils.Vector((v_world.x, v_world.y, v_world.z, 1.0))
    # Blender camera looks down -Z in camera space; depth is positive in front
    depth = -v_cam.z
    if depth <= 0:
        return None  # behind camera
    u = fx * (v_cam.x / depth) + cx
    v = -fy * (v_cam.y / depth) + cy  # flip Y: image Y goes down
    return u, v, depth


def _rasterize_triangle(
    uv0: Tuple[float, float],
    uv1: Tuple[float, float],
    uv2: Tuple[float, float],
    pixel_set: set,
) -> None:
    """Add all integer pixel centers inside the triangle to pixel_set.

    Uses a bounding-box scan with barycentric coordinate test.
    No image-boundary clipping — intentional (unbounded projection).
    """
    u0, v0 = uv0
    u1, v1 = uv1
    u2, v2 = uv2

    u_min = int(math.floor(min(u0, u1, u2)))
    u_max = int(math.ceil(max(u0, u1, u2)))
    v_min = int(math.floor(min(v0, v1, v2)))
    v_max = int(math.ceil(max(v0, v1, v2)))

    # Degenerate triangle guard
    denom = (v1 - v2) * (u0 - u2) + (u2 - u1) * (v0 - v2)
    if abs(denom) < 1e-6:
        return

    inv_denom = 1.0 / denom

    for pu in range(u_min, u_max + 1):
        for pv in range(v_min, v_max + 1):
            # Barycentric coordinates
            w0 = ((v1 - v2) * (pu - u2) + (u2 - u1) * (pv - v2)) * inv_denom
            w1 = ((v2 - v0) * (pu - u2) + (u0 - u2) * (pv - v2)) * inv_denom
            w2 = 1.0 - w0 - w1
            if w0 >= 0 and w1 >= 0 and w2 >= 0:
                pixel_set.add((pu, pv))


def _compute_projected_pixels(
    root_obj,
    view_mat,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
) -> Tuple[int, int, int]:
    """Rasterize all mesh triangles of ``root_obj`` subtree.

    Returns (projected_pixel_count, total_tris, skipped_tris).
    skipped_tris: triangles with any vertex behind camera (depth <= 0), conservatively skipped.
    """
    import bpy

    dg = bpy.context.evaluated_depsgraph_get()
    pixel_set: set = set()
    total_tris = 0
    skipped_tris = 0

    # Collect all mesh objects in the subtree (root + all children recursively)
    def _iter_meshes(obj):
        if obj.type == "MESH":
            yield obj
        for child in obj.children:
            yield from _iter_meshes(child)

    for mesh_obj in _iter_meshes(root_obj):
        try:
            oe = mesh_obj.evaluated_get(dg)
            me = oe.to_mesh()
            if me is None:
                oe.to_mesh_clear()
                continue
            me.calc_loop_triangles()
            mw = oe.matrix_world

            # Project all vertices once
            verts_uv: List[Optional[Tuple[float, float]]] = []
            for v in me.vertices:
                v_world = mw @ v.co
                result = _project_vertex(v_world, view_mat, fx, fy, cx, cy)
                if result is None:
                    verts_uv.append(None)  # behind camera
                else:
                    verts_uv.append((result[0], result[1]))

            for lt in me.loop_triangles:
                i0, i1, i2 = lt.vertices
                uv0 = verts_uv[i0]
                uv1 = verts_uv[i1]
                uv2 = verts_uv[i2]
                total_tris += 1
                if uv0 is None or uv1 is None or uv2 is None:
                    skipped_tris += 1
                    continue
                _rasterize_triangle(uv0, uv1, uv2, pixel_set)

            oe.to_mesh_clear()
        except Exception as e:
            print(f"[truncation_filter] WARN: mesh eval failed for {mesh_obj.name}: {e}")
            continue

    return len(pixel_set), total_tris, skipped_tris


# ---------------------------------------------------------------------------
# Main per-scene computation
# ---------------------------------------------------------------------------

def compute_truncation(
    blend_dir: str,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute truncation ratio for each candidate object.

    ``candidates``: list of {"gt_id": str, "actual_pixels": int}

    Returns result dict matching the result_json schema in the module docstring.
    """
    import bpy

    from generation_common.asset_rules import find_render_camera, collect_assets

    result: Dict[str, Any] = {
        "computed": False,
        "results": [],
        "behind_camera_tris": [],
        "error": None,
    }

    cam_obj = find_render_camera()
    if cam_obj is None:
        result["error"] = "render_camera_not_found"
        return result

    render = bpy.context.scene.render
    render_w = render.resolution_x
    render_h = render.resolution_y

    view_mat = _build_camera_matrix(cam_obj)
    fx, fy, cx, cy = _build_intrinsics(cam_obj, render_w, render_h)

    # Build gt_id → bpy object map from unique_assets
    all_assets = collect_assets()
    gt_id_to_bpy: Dict[str, Any] = {}
    for obj in all_assets:
        gt_id_to_bpy[obj.name] = obj

    obj_results: List[Dict[str, Any]] = []
    behind_cam_records: List[Dict[str, Any]] = []

    for cand in candidates:
        gt_id: str = cand["gt_id"]
        actual_pixels: int = int(cand["actual_pixels"])

        bpy_obj = gt_id_to_bpy.get(gt_id)
        if bpy_obj is None:
            # Try partial match — gt_id may differ slightly from bpy object name
            for name, obj in gt_id_to_bpy.items():
                if gt_id in name or name in gt_id:
                    bpy_obj = obj
                    break

        if bpy_obj is None:
            print(f"[truncation_filter] WARN: bpy object not found for gt_id={gt_id!r}, skip")
            # Treat as passed (conservative: don't filter objects we can't evaluate)
            obj_results.append(
                {
                    "gt_id": gt_id,
                    "actual_pixels": actual_pixels,
                    "projected_pixels": None,
                    "truncation_ratio": None,
                    "passed": True,
                    "note": "bpy_object_not_found",
                }
            )
            continue

        projected, total_tris, skipped_tris = _compute_projected_pixels(
            bpy_obj, view_mat, fx, fy, cx, cy
        )

        if skipped_tris > 0:
            behind_cam_records.append(
                {
                    "gt_id": gt_id,
                    "total_tris": total_tris,
                    "skipped_tris": skipped_tris,
                    "skipped_ratio": round(skipped_tris / max(total_tris, 1), 4),
                }
            )

        if projected == 0:
            # Entire mesh behind camera or degenerate — conservative pass
            obj_results.append(
                {
                    "gt_id": gt_id,
                    "actual_pixels": actual_pixels,
                    "projected_pixels": 0,
                    "truncation_ratio": None,
                    "passed": True,
                    "note": "projected_pixels_zero",
                }
            )
            continue

        ratio = actual_pixels / projected
        passed = ratio >= TRUNCATION_RATIO_MIN

        obj_results.append(
            {
                "gt_id": gt_id,
                "actual_pixels": actual_pixels,
                "projected_pixels": projected,
                "truncation_ratio": round(ratio, 4),
                "passed": passed,
            }
        )
        print(
            f"[truncation_filter] {gt_id}: actual={actual_pixels} projected={projected} "
            f"ratio={ratio:.3f} passed={passed}"
        )

    result["computed"] = True
    result["results"] = obj_results
    result["behind_camera_tris"] = behind_cam_records
    return result


# ---------------------------------------------------------------------------
# Subprocess CLI entry point
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    parser = argparse.ArgumentParser(
        description="Filter 3: truncation ratio via mesh rasterization (bpy subprocess)."
    )
    parser.add_argument("--blend", required=True, help="Path to scene.blend")
    parser.add_argument(
        "--candidates-json",
        required=True,
        help="JSON file with {scene_id, objects:[{gt_id, actual_pixels}]}",
    )
    parser.add_argument("--out-json", required=True, help="Output result JSON path")
    args = parser.parse_args()

    import bpy

    abs_blend = os.path.abspath(args.blend)
    if bpy.data.filepath != abs_blend:
        bpy.ops.wm.open_mainfile(filepath=abs_blend)

    with open(args.candidates_json, encoding="utf-8") as f:
        candidates_data = json.load(f)

    scene_id = candidates_data.get("scene_id", "")
    candidates = candidates_data.get("objects", [])
    blend_dir = os.path.dirname(abs_blend)

    result = compute_truncation(blend_dir, candidates)
    result["scene_id"] = scene_id

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"[truncation_filter] wrote {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())