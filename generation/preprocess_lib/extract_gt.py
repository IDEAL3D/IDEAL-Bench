"""
Write ``GT.json`` from the currently opened Blender scene (single-room box pipeline).

Enumerates top-level assets under ``unique_assets`` after excluding structure, doors, windows, etc.

- ``scene_id``: scene directory name (aligned with ``objects_in_view.json``).
- ``position``: root object ``matrix_world.translation``.
- ``dimensions``: merged subtree MESH AABB in **asset-root local space** as ``[dx,dy,dz]`` (``combined_aabb_root_local``); see repo docs (e.g. ``IDEAL/docs/geometric_rules_floor.md``).
- ``rotation_yaw``: semantic forward vs. +Y, ``atan2(-fx,fy)`` in degrees; see ``semantic_yaw_degrees``. Category ``blackboard`` falls back to **root-local +Y** when Subpart.Front is missing (not default +X). Category ``side_table`` (``SideTableFactory`` default ``(w,w,h)`` square top) **forces 0** when horizontal ``dimensions`` are nearly square. Lamps (``lamp`` / ``*_lamp``) and potted plants with ``_plant_`` in the category name have **no stable semantic heading**; JSON stores **`null`** (eval should skip yaw).
- ``resolve_room_metrics_box``: derives ``scene.room.size`` ``[W,D,H]`` from ``unique_assets:room_floor`` et al. (legacy ``room_geometry.py`` role).
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOM_FLOOR_COLLECTION = "unique_assets:room_floor"

# Without Subpart.Front, default projects root-local +X onto XY; blackboard meshes often lack tags—board normal disagrees with +X fallback, so use +Y (GT only; does not alter Infinigen).
BLACKBOARD_CATEGORY = "blackboard"
# SideTableFactory (…/tables/dining_table.py) defaults dimensions=(w,w,h): square horizontal footprint, yaw indistinguishable at 90° steps.
SIDE_TABLE_CATEGORY = "side_table"


def _rotation_yaw_not_applicable(category: str) -> bool:
    """Lamps and plants: no meaningful semantic 'front' for yaw; GT uses JSON null."""
    if category == "lamp" or category.endswith("_lamp"):
        return True
    # Token-boundary style: matches large_plant_container, flower_plant; not substring of e.g. "implant".
    if "_plant_" in f"_{category}_":
        return True
    return False


_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

_REPO_ROOT = os.path.dirname(_GENERATION_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from configs.constant import ROOM_TYPES

from preprocess_lib.extract_camview import (
    collect_assets,
    combined_aabb,
    combined_aabb_root_local,
    factory_to_category,
    find_render_camera,
    identify_camera_room,
    is_floor,
    room_height,
    scene_id_from_output_dir,
    world_aabb,
)


def room_type_from_scene_dir(scene_dir: str) -> str:
    """First segment of directory name split on ``_``; must be in ``configs.constant.ROOM_TYPES``, else ``unknown``."""
    base = os.path.basename(os.path.normpath(scene_dir))
    if not base:
        return "unknown"
    prefix = base.split("_")[0].lower() or "unknown"
    if prefix == "unknown":
        return "unknown"
    if prefix in ROOM_TYPES:
        return prefix
    print(
        f"[WARN] unexpected room_type prefix {prefix!r} from {scene_dir}, falling back to \"unknown\""
    )
    return "unknown"


def _horizontal_unit(vx: float, vy: float) -> Optional[Tuple[float, float]]:
    hlen = math.hypot(vx, vy)
    if hlen < 1e-8:
        return None
    return (vx / hlen, vy / hlen)


def _global_polygon_normal_world(obj, polygon) -> Tuple[float, float, float]:
    """Unit normal of *polygon* in world space (same idea as infinigen butil.global_polygon_normal)."""
    import numpy as np
    from mathutils import Vector

    _loc, rot, _scale = obj.matrix_world.decompose()
    rot_m = rot.to_matrix()
    n = rot_m @ polygon.normal
    ln = float(np.linalg.norm(n))
    if ln < 1e-12:
        return (0.0, 0.0, 1.0)
    n = n / ln
    return (float(n.x), float(n.y), float(n.z))


def _semantic_forward_xy(root, category: Optional[str] = None) -> Tuple[float, float]:
    from mathutils import Vector

    sx, sy = 0.0, 0.0

    try:
        from infinigen.core import tagging
        from infinigen.core import tags as t

        for o in [root] + list(root.children_recursive):
            if o.type != "MESH" or not o.data or len(o.data.polygons) == 0:
                continue
            if tagging.COMBINED_ATTR_NAME not in o.data.attributes:
                continue
            try:
                mask = tagging.tagged_face_mask(o, {t.Subpart.Front})
            except Exception:
                continue
            if not mask.any():
                continue
            for poly in o.data.polygons:
                if not mask[poly.index]:
                    continue
                nx, ny, _nz = _global_polygon_normal_world(o, poly)
                wh = math.hypot(nx, ny)
                if wh < 1e-8:
                    continue
                sx += nx
                sy += ny
    except ImportError:
        pass

    hu = _horizontal_unit(sx, sy)
    if hu is not None:
        return hu

    mw = root.matrix_world
    if category == BLACKBOARD_CATEGORY:
        lv = mw.to_3x3() @ Vector((0.0, 1.0, 0.0))
    else:
        lv = mw.to_3x3() @ Vector((1.0, 0.0, 0.0))
    hu = _horizontal_unit(float(lv.x), float(lv.y))
    if hu is not None:
        return hu

    return (0.0, 1.0)


def semantic_yaw_degrees(root, category: Optional[str] = None) -> int:
    fx, fy = _semantic_forward_xy(root, category=category)
    deg = math.degrees(math.atan2(-fx, fy))
    return int(round(deg)) % 360


def _near_square_xy(dim: List[float], *, rel_tol: float = 0.12, abs_tol: float = 0.05) -> bool:
    """True if root-local horizontal AABB span dx, dy are ~equal (guards non-default rectangular side tables)."""
    if len(dim) < 2:
        return False
    dx, dy = float(dim[0]), float(dim[1])
    m = max(dx, dy, 1e-9)
    return abs(dx - dy) <= max(abs_tol, rel_tol * m)


def _floor_meshes_from_room_floor_collection():
    import bpy

    col = bpy.data.collections.get(ROOM_FLOOR_COLLECTION)
    if col is None:
        return []
    return [
        o
        for o in col.all_objects
        if o.type == "MESH" and is_floor(o)
    ]


def room_metrics_from_floor_collection() -> Optional[Dict[str, Any]]:
    import bpy

    meshes = _floor_meshes_from_room_floor_collection()
    if not meshes:
        return None

    lo, hi = combined_aabb(meshes)
    rid = meshes[0].name.split(".")[0]
    ztop = hi.z
    W = round(hi.x - lo.x, 2)
    D = round(hi.y - lo.y, 2)
    H = round(room_height(rid, ztop), 2)

    return {
        "room_id": rid,
        "floor_meshes": meshes,
        "floor_aabb_lo": lo,
        "floor_aabb_hi": hi,
        "W": W,
        "D": D,
        "H": H,
    }


def resolve_room_metrics_box() -> Optional[Dict[str, Any]]:
    """
    Single-room box: prefer merged AABB from ``unique_assets:room_floor`` for W/D/H;
    else camera ray-cast to floor; else largest ``*.floor`` mesh.

    Returned dict includes ``cam_obj`` (render camera). ``extract_info`` does not rely on camera fields when writing JSON.
    """
    import bpy

    cam = find_render_camera()

    m = room_metrics_from_floor_collection()
    if m is not None:
        m["cam_obj"] = cam
        print(
            f"[extract_gt] room={m['room_id']} size=({m['W']}, {m['D']}, {m['H']}) "
            f"from {ROOM_FLOOR_COLLECTION}"
        )
        return m

    ri = identify_camera_room()
    if ri is not None:
        print(f"[extract_gt] room={ri['room_id']} from camera ray-cast")
        return ri

    floors: List = [
        o for o in bpy.data.objects if o.type == "MESH" and is_floor(o)
    ]
    if not floors:
        return None
    best = None
    best_area = -1.0
    for fo in floors:
        lo, hi = world_aabb(fo)
        area = (hi.x - lo.x) * (hi.y - lo.y)
        if area > best_area:
            best_area = area
            best = fo
    rid = best.name.split(".")[0]
    flo, fhi = world_aabb(best)
    ztop = fhi.z
    W = round(fhi.x - flo.x, 2)
    D = round(fhi.y - flo.y, 2)
    H = round(room_height(rid, ztop), 2)
    print(f"[extract_gt] room={rid} size=({W}, {D}, {H}) from largest .floor fallback")
    return {
        "room_id": rid,
        "floor_aabb": (flo, fhi),
        "W": W,
        "D": D,
        "H": H,
        "cam_obj": cam,
    }


def build_gt_payload(room_metrics: Dict[str, Any], scene_dir: str) -> Dict[str, Any]:
    assets = collect_assets()
    objects_out: List[Dict[str, Any]] = []

    for obj in sorted(assets, key=lambda o: o.name):
        cat = factory_to_category(obj.name)
        loc = obj.matrix_world.to_translation()
        pos = [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)]

        hier = [obj] + list(obj.children_recursive)
        meshes = [o for o in hier if o.type == "MESH"]
        if meshes:
            lo, hi = combined_aabb_root_local(obj, meshes)
            dim = [
                round(hi.x - lo.x, 2),
                round(hi.y - lo.y, 2),
                round(hi.z - lo.z, 2),
            ]
        else:
            import bpy

            dg = bpy.context.evaluated_depsgraph_get()
            ev = obj.evaluated_get(dg)
            bb = ev.bound_box
            xs = [c[0] for c in bb]
            ys = [c[1] for c in bb]
            zs = [c[2] for c in bb]
            dim = [
                round(max(xs) - min(xs), 2),
                round(max(ys) - min(ys), 2),
                round(max(zs) - min(zs), 2),
            ]

        yaw_deg = semantic_yaw_degrees(obj, category=cat)
        if cat == SIDE_TABLE_CATEGORY and _near_square_xy(dim):
            yaw_deg = 0
        rotation_yaw_out: int | None = None if _rotation_yaw_not_applicable(cat) else yaw_deg

        objects_out.append(
            {
                "id": obj.name,
                "category": cat,
                "position": pos,
                "dimensions": dim,
                "rotation_yaw": rotation_yaw_out,
            }
        )

    room_type = room_type_from_scene_dir(scene_dir)
    n_assets = len(objects_out)

    return {
        "scene_id": scene_id_from_output_dir(scene_dir),
        "scene": {
            "room": {
                "type": room_type,
                "size": [room_metrics["W"], room_metrics["D"], room_metrics["H"]],
                "asset_count": n_assets,
            },
            "objects": objects_out,
        },
    }


def write_gt_json(blend_dir: str, room_metrics: Dict[str, Any]) -> str:
    path = os.path.join(blend_dir, "GT.json")
    payload = build_gt_payload(room_metrics, blend_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    n = len(payload["scene"]["objects"])
    print(f"[extract_gt] wrote {path} ({n} objects)")
    return path
