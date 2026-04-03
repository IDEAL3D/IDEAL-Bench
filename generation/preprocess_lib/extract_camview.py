"""
相机 / 视锥 / 场景几何工具：渲染相机与房间、视锥内资产枚举、AABB 与射线等。

**镜头内资产与相机块**（``categories_in_view``、``asset_count_in_view``、``objects``、``intrinsic`` /
``extrinsic`` / ``blender``）由 ``build_camview_payload_v2`` 汇总；家具体素重叠见 ``overlap_check``；
完整 ``info.json`` 的组装与落盘见 ``extract_info.py``。

``extract_gt.py`` 从本模块引用共享几何/分类函数。

独立调试：``import preprocess_lib.extract_camview as m`` 并调用 ``m.find_visible_assets(cam)``；或
``blender -b scene.blend --python preprocess_lib/extract_camview.py``（需 ``PYTHONPATH`` 含 ``generation`` 目录）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .defaults import GT_JSON_NAME

_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

# ── 原 preprocess/config.py：GT/视锥 共用集合规则（无 bpy 依赖）────────────────
ROOM_STRUCTURE_COLLECTIONS = frozenset(
    {
        "unique_assets:room_wall",
        "unique_assets:room_floor",
        "unique_assets:room_ceiling",
        "unique_assets:room_exterior",
        "unique_assets:staircases",
    }
)
ASSET_GT_EXCLUDE_COLLECTIONS = ROOM_STRUCTURE_COLLECTIONS | frozenset(
    {
        "unique_assets:doors",
        "unique_assets:windows",
        "unique_assets:door_casings",
        "skirting",
    }
)
FULL_DELETE_COLLECTION_PREFIXES = (
    "door_base_elements",
)

# 可选：仅保留「大件」类别时传给 ``find_visible_assets(..., category_whitelist=...)``

def scene_id_from_output_dir(dir_path: str) -> str:
    """与含 ``scene.blend`` 的场景目录文件夹名一致（如 ``kitchen_medium_s1759680675``）。"""
    return os.path.basename(os.path.normpath(os.path.abspath(dir_path)))


KEEP_CATEGORIES = frozenset(
    {
        "chair",
        "office_chair",
        "bar_chair",
        "sofa",
        "table_dining",
        "table_cocktail",
        "simple_desk",
        "table_top",
        "large_shelf",
        "cell_shelf",
        "simple_bookcase",
        "triangle_shelf",
        "single_cabinet",
        "cabinet",
        "cabinet_drawer",
        "kitchen_cabinet",
        "kitchen_space",
        "countertop",
        "wall_shelf",
        "bed",
        "lamp",
        "floor_lamp",
        "desk_lamp",
        "ceiling_light",
        "ceiling_classic_lamp",
        "tv",
        "beverage_fridge",
        "microwave",
        "oven",
        "dishwasher",
        "toilet",
        "sink",
        "bathtub",
        "rug",
        "wall_art",
        "plant_container",
        "large_plant_container",
        "aquarium_tank",
        "range_hood",
    }
)


# ── 原 utils.py ────────────────────────────────────────────────────────────


def in_blender() -> bool:
    try:
        import bpy  # noqa: F401

        return True
    except Exception:
        return False


def is_floor(obj) -> bool:
    return getattr(obj, "type", None) == "MESH" and obj.name.endswith(".floor")


def is_ceiling(obj) -> bool:
    return getattr(obj, "type", None) == "MESH" and obj.name.endswith(".ceiling")


def world_aabb(obj):
    import bpy
    import mathutils

    dg = bpy.context.evaluated_depsgraph_get()
    oe = obj.evaluated_get(dg)
    me = oe.to_mesh()
    if not me or not me.vertices:
        oe.to_mesh_clear()
        loc = oe.matrix_world.translation.copy()
        return loc, loc.copy()

    mw = oe.matrix_world
    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    for v in me.vertices:
        w = mw @ v.co
        for i in range(3):
            if w[i] < lo[i]:
                lo[i] = w[i]
            if w[i] > hi[i]:
                hi[i] = w[i]
    oe.to_mesh_clear()
    return lo, hi


def combined_aabb(objs):
    import mathutils

    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    valid = False
    for o in objs:
        if o.type != "MESH":
            continue
        try:
            a, b = world_aabb(o)
        except Exception:
            continue
        valid = True
        for i in range(3):
            if a[i] < lo[i]:
                lo[i] = a[i]
            if b[i] > hi[i]:
                hi[i] = b[i]
    if not valid:
        z = objs[0].matrix_world.to_translation() if objs else mathutils.Vector()
        return z.copy(), z.copy()
    return lo, hi


def combined_aabb_root_local(root, mesh_objs) -> Tuple[Any, Any]:
    """
    将子树内各 MESH 的**已求值**顶点变到 **root 的局部坐标系**，再合并为轴对齐 min/max。

    ``dimensions = hi - lo`` 的分量沿 **根物体局部 X/Y/Z**，与根上 yaw 对齐，避免世界系 AABB
    在斜放家具上沿世界轴「外接膨胀」。若 ``matrix_world`` 不可逆则回退为 ``combined_aabb``（世界系）。
    """
    import bpy
    import mathutils

    try:
        inv = root.matrix_world.inverted()
    except Exception:
        return combined_aabb(mesh_objs)

    dg = bpy.context.evaluated_depsgraph_get()
    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    valid = False
    for o in mesh_objs:
        if o.type != "MESH":
            continue
        try:
            oe = o.evaluated_get(dg)
            me = oe.to_mesh()
            if not me or not me.vertices:
                oe.to_mesh_clear()
                continue
            mw = oe.matrix_world
            for v in me.vertices:
                pw = mw @ v.co
                pl = inv @ pw
                valid = True
                for i in range(3):
                    if pl[i] < lo[i]:
                        lo[i] = pl[i]
                    if pl[i] > hi[i]:
                        hi[i] = pl[i]
            oe.to_mesh_clear()
        except Exception:
            continue
    if not valid:
        z = mathutils.Vector((0.0, 0.0, 0.0))
        return z.copy(), z.copy()
    return lo, hi


def raycast_floor(origin, max_depth: float = 100.0, eps: float = 1e-3):
    import bpy
    import mathutils

    dg = bpy.context.evaluated_depsgraph_get()
    d = mathutils.Vector((0, 0, -1))
    cur = origin.copy()
    rem = max_depth
    while rem > 0:
        hit, loc, *_, obj, _ = bpy.context.scene.ray_cast(dg, cur, d, distance=rem)
        if not hit:
            return None, None
        if is_floor(obj):
            return obj, loc
        t = cur.z - loc.z
        if t <= 0:
            t = eps
        rem -= t
        cur = loc + d * eps
    return None, None


def room_height(room_id: str, floor_z_top: float) -> float:
    import bpy

    for o in bpy.data.objects:
        if is_ceiling(o) and o.name.startswith(room_id + "."):
            cmin, _ = world_aabb(o)
            return round(cmin.z - floor_z_top, 4)
    return 2.7


def factory_to_category(obj_name: str) -> str:
    m = re.match(r"^([A-Za-z]+?)Factory", obj_name)
    if m:
        camel = m.group(1)
        return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()
    base = obj_name.split("(")[0].split(".")[0]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower() or "unknown"


def extract_room_type(blend_path: str) -> str:
    scene_dir = os.path.basename(os.path.dirname(os.path.dirname(blend_path)))
    parts = scene_dir.split("_")
    return parts[0] if parts else "unknown"


# ── 原 identify_room.py ─────────────────────────────────────────────────────

RENDER_CAMERA_NAME = "camera_0_0"


def find_render_camera():
    import bpy

    obj = bpy.data.objects.get(RENDER_CAMERA_NAME)
    if obj is None:
        return None
    if obj.type == "CAMERA":
        return obj
    for c in getattr(obj, "children", []):
        if c.type == "CAMERA":
            return c
    return None


def identify_camera_room() -> Optional[Dict[str, Any]]:
    cam = find_render_camera()
    if cam is None:
        print("[extract_camview] ERROR: render camera not found.")
        return None

    cam_loc = cam.matrix_world.to_translation()
    floor_obj, _ = raycast_floor(cam_loc)
    if floor_obj is None:
        print("[extract_camview] ERROR: no floor below camera.")
        return None

    rid = floor_obj.name.split(".")[0]
    flo, fhi = world_aabb(floor_obj)
    cx = 0.5 * (flo.x + fhi.x)
    cy = 0.5 * (flo.y + fhi.y)
    ztop = fhi.z
    W = round(fhi.x - flo.x, 2)
    D = round(fhi.y - flo.y, 2)
    H = round(room_height(rid, ztop), 2)

    print(f"[extract_camview] room={rid}  size=({W}, {D}, {H}) (ray-cast)")
    return {
        "cam_obj": cam,
        "room_id": rid,
        "floor_center": (cx, cy, ztop),
        "floor_aabb": (flo, fhi),
        "W": W,
        "D": D,
        "H": H,
    }


# ── 视锥与资产收集（原 find_visible.py）────────────────────────────────────


def collect_assets() -> list:
    """``unique_assets`` 下顶层物体，排除结构/门窗/踢脚线等子 collection（与 GT 资产集一致）。"""
    import bpy

    ua = bpy.data.collections.get("unique_assets")
    if ua is None:
        return []

    skip_names: set = set()
    for col_name in ASSET_GT_EXCLUDE_COLLECTIONS:
        col = bpy.data.collections.get(col_name)
        if col:
            for o in col.all_objects:
                skip_names.add(o.name)

    for col in bpy.data.collections:
        if any(col.name.startswith(p) for p in FULL_DELETE_COLLECTION_PREFIXES):
            for o in col.all_objects:
                skip_names.add(o.name)

    return [
        o for o in ua.all_objects if o.name not in skip_names and o.parent is None
    ]


def in_frustum(obj, cam) -> bool:
    """True if any corner / centre of the asset AABB projects into the camera."""
    import bpy
    import mathutils
    from bpy_extras.object_utils import world_to_camera_view

    hierarchy = [obj] + list(obj.children_recursive)
    meshes = [o for o in hierarchy if o.type == "MESH"]

    if meshes:
        lo, hi = combined_aabb(meshes)
        pts = [
            mathutils.Vector((x, y, z))
            for x in (lo.x, hi.x)
            for y in (lo.y, hi.y)
            for z in (lo.z, hi.z)
        ]
        pts.append((lo + hi) / 2)
    else:
        pts = [obj.matrix_world.to_translation()]
        lo = hi = None

    sc = bpy.context.scene
    for p in pts:
        ndc = world_to_camera_view(sc, cam, p)
        if 0 <= ndc.x <= 1 and 0 <= ndc.y <= 1 and ndc.z > 0:
            return True

    if lo is not None:
        cl = cam.matrix_world.to_translation()
        if lo.x <= cl.x <= hi.x and lo.y <= cl.y <= hi.y and lo.z <= cl.z <= hi.z:
            return True

    return False


def find_visible_assets(cam_obj, category_whitelist: frozenset | None = None) -> List:
    """
    先 ``collect_assets()``，再按相机视锥筛选。

    category_whitelist
        ``None``：不过滤类别（与 GT 一致）。否则仅保留白名单类别（如 ``KEEP_CATEGORIES``）。
    """
    all_assets = collect_assets()

    if category_whitelist is None:
        kept = all_assets
    else:
        kept, skipped_cat = [], []
        for a in all_assets:
            cat = factory_to_category(a.name)
            if cat in category_whitelist:
                kept.append(a)
            else:
                skipped_cat.append((cat, a.name))
        if skipped_cat:
            print(f"[extract_camview] skipped {len(skipped_cat)} assets (category whitelist):")
            for cat, name in skipped_cat:
                print(f"    ✕ {cat:30s}  {name}")

    visible = [a for a in kept if in_frustum(a, cam_obj)]
    tag = "whitelisted " if category_whitelist is not None else ""
    print(
        f"[extract_camview] {len(visible)}/{len(kept)} {tag}assets visible "
        f"(from {len(all_assets)} total in unique_assets)"
    )
    for a in visible:
        print(f"    • {factory_to_category(a.name):30s}  {a.name}")
    return visible


# ── 镜头内语义 + 相机块（供 ``extract_info`` 并入完整 ``info.json``）────────────────

def _matrix_to_nested_v2(m) -> List[List[float]]:
    return [[float(x) for x in row] for row in m]


def _objects_from_gt_for_visible(blend_dir: str, visible) -> List[Dict[str, Any]]:
    """按 ``find_visible_assets`` 顺序，从 ``GT.json`` 取同 ``id``（根物体名）的物体条目拷贝。"""
    path = os.path.join(blend_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        print(f"[extract_camview] WARN: missing {GT_JSON_NAME}, objects=[]")
        return []
    try:
        with open(path, encoding="utf-8") as f:
            gt_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[extract_camview] WARN: could not read GT: {e}")
        return []
    objs = gt_data.get("scene", {}).get("objects")
    if not isinstance(objs, list):
        return []
    id_to_row = {str(o.get("id")): o for o in objs if isinstance(o, dict) and o.get("id") is not None}

    out: List[Dict[str, Any]] = []
    for o in visible:
        key = o.name
        row = id_to_row.get(key)
        if row is None:
            print(
                f"[extract_camview] WARN: visible asset {key!r} has no matching GT id, skipped"
            )
            continue
        out.append(copy.deepcopy(row))
    return out


def build_camview_payload_v2(cam_obj, scene_id: str, blend_dir: str) -> Dict[str, Any]:
    import bpy

    from infinigen.core.util.camera import (
        get_3x4_RT_matrix_from_blender,
        get_calibration_matrix_K_from_blender,
    )

    visible = find_visible_assets(cam_obj)
    categories = sorted({factory_to_category(o.name) for o in visible})
    objects_out = _objects_from_gt_for_visible(blend_dir, visible)

    K = get_calibration_matrix_K_from_blender(cam_obj.data)
    RT = get_3x4_RT_matrix_from_blender(cam_obj)
    K_list = _matrix_to_nested_v2(K)
    RT_list = _matrix_to_nested_v2(RT)

    return {
        "scene_id": scene_id,
        "categories_in_view": categories,
        "asset_count_in_view": len(visible),
        "objects": objects_out,
        "intrinsic": {
            "K": K_list,
            "fx": K_list[0][0],
            "fy": K_list[1][1],
            "cx": K_list[0][2],
            "cy": K_list[1][2],
        },
        "extrinsic": {
            "world_to_camera_rt": RT_list,
        },
        "blender": {
            "matrix_world": _matrix_to_nested_v2(cam_obj.matrix_world),
        },
    }


if __name__ == "__main__":
    cam = find_render_camera()
    if cam:
        find_visible_assets(cam)
