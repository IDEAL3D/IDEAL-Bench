"""
Filter 3 — Truncation Filter.

Runs inside a bpy process (called as a subprocess by ``filter_inview.py``).

Computes truncation_ratio = actual_pixels / projected_pixels for each candidate object,
where projected_pixels is the mesh silhouette rasterized onto an unbounded virtual canvas
(no occlusion, no image boundary clipping).

An object is filtered only when ALL THREE hold:
    1. Centroid of its instance-map pixels is within EDGE_CENTROID_MARGIN of any image border.
    2. truncation_ratio < TRUNCATION_RATIO_MIN.
    3. actual_pixels / (W * H) < OCCUPANCY_MIN  (rescue: keeps visually significant objects
       whose projected_pixels is inflated by near-camera depth ≈ 0).

For SingleBedFactory objects, the corresponding MattressFactory mesh is also rasterized
and its projected pixels merged, since bed + mattress share the same GT entry.

CLI:
    bpy_python -m preprocess_lib.truncation_filter \\
        --blend <scene.blend> \\
        --candidates-json <candidates.json> \\
        --out-json <result.json>

candidates.json: {scene_id, render_size:[W,H], objects:[{gt_id, actual_pixels, centroid:[cx,cy], mattress_gt_id?}]}
result.json:     {scene_id, computed, results:[{gt_id, actual_pixels, truncation_ratio, near_edge, passed}], behind_camera_tris, error}
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

from .defaults import EDGE_CENTROID_MARGIN, OCCUPANCY_MIN, TRUNCATION_RATIO_MIN


# ---------------------------------------------------------------------------
# Edge proximity check (centroid-based)
# ---------------------------------------------------------------------------

def _is_near_edge(
    cx: float,
    cy: float,
    img_w: int,
    img_h: int,
    margin: float = EDGE_CENTROID_MARGIN,
) -> bool:
    """Return True if the pixel centroid is within ``margin`` fraction of any image border."""
    margin_x = img_w * margin
    margin_y = img_h * margin
    return (
        cx < margin_x
        or cx > img_w - margin_x
        or cy < margin_y
        or cy > img_h - margin_y
    )


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def _build_camera_matrix(cam_obj) -> Any:
    """Return camera-from-world 4x4 matrix (view matrix)."""
    return cam_obj.matrix_world.inverted()


def _build_intrinsics(cam_obj, render_w: int, render_h: int) -> Tuple[float, float, float, float]:
    """Return (fx, fy, cx, cy) in pixels."""
    cam = cam_obj.data
    sensor_fit = cam.sensor_fit
    aspect = render_w / render_h

    if sensor_fit == "VERTICAL" or (sensor_fit == "AUTO" and aspect < 1.0):
        sensor_mm = cam.sensor_height
        fy = (cam.lens / sensor_mm) * render_h
        fx = fy
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

def _project_vertex(
    v_world,
    view_mat,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    render_w: int,
    render_h: int,
):
    """Project world-space vertex to (u, v, depth), clamped to [-W, 2W] x [-H, 2H].

    Returns None if vertex is behind camera (depth <= 0).
    """
    import mathutils

    v_cam = view_mat @ mathutils.Vector((v_world.x, v_world.y, v_world.z, 1.0))
    depth = -v_cam.z
    if depth <= 0:
        return None
    u = fx * (v_cam.x / depth) + cx
    v = -fy * (v_cam.y / depth) + cy
    u = max(float(-render_w), min(u, float(render_w * 2)))
    v = max(float(-render_h), min(v, float(render_h * 2)))
    return u, v, depth


def _rasterize_triangle(
    uv0: Tuple[float, float],
    uv1: Tuple[float, float],
    uv2: Tuple[float, float],
    pixel_set: set,
) -> None:
    """Add integer pixel centers inside the triangle to pixel_set (unbounded)."""
    u0, v0 = uv0
    u1, v1 = uv1
    u2, v2 = uv2

    u_min = int(math.floor(min(u0, u1, u2)))
    u_max = int(math.ceil(max(u0, u1, u2)))
    v_min = int(math.floor(min(v0, v1, v2)))
    v_max = int(math.ceil(max(v0, v1, v2)))

    denom = (v1 - v2) * (u0 - u2) + (u2 - u1) * (v0 - v2)
    if abs(denom) < 1e-6:
        return
    inv_denom = 1.0 / denom

    for pu in range(u_min, u_max + 1):
        for pv in range(v_min, v_max + 1):
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
    render_w: int,
    render_h: int,
) -> Tuple[int, int, int]:
    """Rasterize all mesh triangles of root_obj subtree.

    Returns (projected_pixel_count, total_tris, skipped_tris).
    skipped_tris: triangles with any vertex behind camera, conservatively skipped.
    """
    import bpy

    dg = bpy.context.evaluated_depsgraph_get()
    pixel_set: set = set()
    total_tris = 0
    skipped_tris = 0

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

            verts_uv: List[Optional[Tuple[float, float]]] = []
            for v in me.vertices:
                v_world = mw @ v.co
                result = _project_vertex(v_world, view_mat, fx, fy, cx, cy, render_w, render_h)
                verts_uv.append((result[0], result[1]) if result is not None else None)

            for lt in me.loop_triangles:
                i0, i1, i2 = lt.vertices
                uv0, uv1, uv2 = verts_uv[i0], verts_uv[i1], verts_uv[i2]
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
    render_w: int,
    render_h: int,
) -> Dict[str, Any]:
    """Compute truncation ratio for each candidate. See module docstring for filter logic."""
    import bpy
    from generation_common.asset_rules import find_render_camera, collect_assets

    result: Dict[str, Any] = {"computed": False, "results": [], "behind_camera_tris": [], "error": None}

    cam_obj = find_render_camera()
    if cam_obj is None:
        result["error"] = "render_camera_not_found"
        return result

    view_mat = _build_camera_matrix(cam_obj)
    fx, fy, cx, cy = _build_intrinsics(cam_obj, render_w, render_h)
    img_total_pixels = render_w * render_h

    gt_id_to_bpy: Dict[str, Any] = {obj.name: obj for obj in collect_assets()}

    obj_results: List[Dict[str, Any]] = []
    behind_cam_records: List[Dict[str, Any]] = []

    for cand in candidates:
        gt_id: str = cand["gt_id"]
        actual_pixels: int = int(cand["actual_pixels"])
        centroid: Optional[List[float]] = cand.get("centroid")
        mattress_gt_id: Optional[str] = cand.get("mattress_gt_id")

        near_edge = (
            _is_near_edge(centroid[0], centroid[1], render_w, render_h)
            if centroid is not None and len(centroid) == 2
            else False
        )

        def _find_bpy(target_id: str):
            obj = gt_id_to_bpy.get(target_id)
            if obj is None:
                for name, o in gt_id_to_bpy.items():
                    if target_id in name or name in target_id:
                        return o
            return obj

        bpy_obj = _find_bpy(gt_id)
        if bpy_obj is None:
            print(f"[truncation_filter] WARN: bpy object not found for gt_id={gt_id!r}, skip")
            obj_results.append({
                "gt_id": gt_id, "actual_pixels": actual_pixels,
                "truncation_ratio": None, "near_edge": near_edge,
                "passed": True, "note": "bpy_object_not_found",
            })
            continue

        projected, total_tris, skipped_tris = _compute_projected_pixels(
            bpy_obj, view_mat, fx, fy, cx, cy, render_w, render_h
        )

        # Merge mattress projection for beds
        mattress_bpy_obj = _find_bpy(mattress_gt_id) if mattress_gt_id else None
        if mattress_bpy_obj is not None:
            m_proj, m_total, m_skip = _compute_projected_pixels(
                mattress_bpy_obj, view_mat, fx, fy, cx, cy, render_w, render_h
            )
            projected += m_proj
            total_tris += m_total
            skipped_tris += m_skip

        if skipped_tris > 0:
            behind_cam_records.append({
                "gt_id": gt_id,
                "total_tris": total_tris,
                "skipped_tris": skipped_tris,
                "skipped_ratio": round(skipped_tris / max(total_tris, 1), 4),
            })

        if projected == 0:
            obj_results.append({
                "gt_id": gt_id, "actual_pixels": actual_pixels,
                "truncation_ratio": None, "near_edge": near_edge,
                "passed": True, "note": "projected_pixels_zero",
            })
            continue

        truncation_ratio = round(actual_pixels / projected, 4)
        rescued = (actual_pixels / img_total_pixels) >= OCCUPANCY_MIN
        passed = (not near_edge) or (truncation_ratio >= TRUNCATION_RATIO_MIN) or rescued

        obj_results.append({
            "gt_id": gt_id,
            "actual_pixels": actual_pixels,
            "truncation_ratio": truncation_ratio,
            "near_edge": near_edge,
            "passed": passed,
        })
        print(
            f"[truncation_filter] {gt_id}: actual_pixels={actual_pixels} "
            f"truncation_ratio={truncation_ratio} near_edge={near_edge} "
            f"rescued={rescued if (near_edge and truncation_ratio < TRUNCATION_RATIO_MIN) else '-'} "
            f"passed={passed}"
        )

    result["computed"] = True
    result["results"] = obj_results
    result["behind_camera_tris"] = behind_cam_records
    return result


# ---------------------------------------------------------------------------
# Subprocess CLI entry point
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    parser = argparse.ArgumentParser(description="Filter 3: truncation ratio via mesh rasterization.")
    parser.add_argument("--blend", required=True)
    parser.add_argument("--candidates-json", required=True)
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args()

    import bpy

    abs_blend = os.path.abspath(args.blend)
    if bpy.data.filepath != abs_blend:
        bpy.ops.wm.open_mainfile(filepath=abs_blend)

    with open(args.candidates_json, encoding="utf-8") as f:
        candidates_data = json.load(f)

    render_size = candidates_data.get("render_size", [1280, 720])
    result = compute_truncation(
        os.path.dirname(abs_blend),
        candidates_data.get("objects", []),
        int(render_size[0]),
        int(render_size[1]),
    )
    result["scene_id"] = candidates_data.get("scene_id", "")

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"[truncation_filter] wrote {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())