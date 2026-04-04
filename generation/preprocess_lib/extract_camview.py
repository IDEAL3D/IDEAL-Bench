"""
相机 / 房间几何 / 资产集合；**in-view 由 ``metadata.json`` 的 ``object_index_map`` +
``instance_segmentation.png``（uint16）统计**，不再用 ``bpy.object.pass_index``。

``extract_gt.py``、``overlap_check`` 共用 ``collect_assets`` / ``factory_to_category`` / AABB 工具。

调试：在 ``PYTHONPATH`` 含 ``generation`` 下 ``blender -b scene.blend --python -c "..."`` 调用
``build_instance_seg_view_payload``。
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .defaults import GT_JSON_NAME, INSTANCE_SEGMENTATION_PNG, METADATA_JSON

_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

_IDL3D_ROOT = os.path.dirname(_GENERATION_DIR)
if _IDL3D_ROOT not in sys.path:
    sys.path.insert(0, _IDL3D_ROOT)
from configs.constant import MIN_VISIBLE_PIXELS, canonicalize_asset_category


class MissingMetadataInstanceMapError(RuntimeError):
    """``metadata.json`` 缺失，或 ``instance_map.object_index_map`` 非 dict / 为空。"""

    pass

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
        cat = re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()
    else:
        base = obj_name.split("(")[0].split(".")[0]
        cat = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower() or "unknown"
    return canonicalize_asset_category(cat)


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


# ── 资产收集（与 GT 同源）──────────────────────────────────────────────────


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


def _load_instance_segmentation_u16(path: str):
    """读取实例分割 PNG，须保留整型像素（16-bit PNG 仍为整型数组，不做 8-bit 衰减读取）。"""
    import numpy as np

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    arr = None
    err_chain: list[Exception] = []
    try:
        import imageio.v3 as iio

        arr = np.asarray(iio.imread(path))
    except Exception as e:
        err_chain.append(e)
        arr = None
    if arr is None:
        try:
            import cv2

            raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if raw is None:
                raise ValueError("cv2.imread returned None")
            arr = np.asarray(raw)
        except Exception as e:
            err_chain.append(e)
            arr = None
    if arr is None:
        try:
            from PIL import Image

            arr = np.array(Image.open(path))
        except Exception as e:
            err_chain.append(e)
            raise RuntimeError(
                f"无法读取实例分割图（须 uint16/整型）: {path}；已尝试 imageio、cv2.IMREAD_UNCHANGED、PIL"
            ) from (err_chain[0] if err_chain else None)
    if arr.ndim == 3:
        arr = arr[:, :, 0]
    if not np.issubdtype(arr.dtype, np.integer):
        raise TypeError(
            f"{path}: 实例分割须为整型 id 图，当前 dtype={arr.dtype}"
        )
    return arr


def _load_object_index_map(blend_dir: str) -> Dict[str, str]:
    path = os.path.join(blend_dir, METADATA_JSON)
    if not os.path.isfile(path):
        raise MissingMetadataInstanceMapError(
            f"缺少 {METADATA_JSON}（objects_in_view 依赖 instance_map.object_index_map）: {path}"
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MissingMetadataInstanceMapError(
            f"无法解析 {path}: {e}"
        ) from e
    inst = data.get("instance_map")
    if not isinstance(inst, dict):
        raise MissingMetadataInstanceMapError(
            f"{path}: 缺少或类型错误: instance_map（须为 object）"
        )
    oim = inst.get("object_index_map")
    if not isinstance(oim, dict) or len(oim) == 0:
        raise MissingMetadataInstanceMapError(
            f"{path}: instance_map.object_index_map 须为非空 dict，当前={oim!r}"
        )
    return {str(k): str(v) for k, v in oim.items()}


def _gt_id_to_metadata_base(gtid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(gtid)).strip("_").lower() or "object"


def _metadata_label_to_base(label: str) -> str:
    m = re.match(r"^(.+)_(\d{3})$", label)
    if m:
        return m.group(1)
    return label


def _build_metadata_base_to_gt_id(blend_dir: str) -> Dict[str, str]:
    path = os.path.join(blend_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"缺少 {GT_JSON_NAME}，无法将 metadata 物体名对齐 GT id: {path}"
        )
    with open(path, encoding="utf-8") as f:
        gt_data = json.load(f)
    objs = gt_data.get("scene", {}).get("objects")
    if not isinstance(objs, list):
        return {}
    base_to_id: Dict[str, str] = {}
    for o in objs:
        if not isinstance(o, dict) or o.get("id") is None:
            continue
        gid = str(o["id"])
        b = _gt_id_to_metadata_base(gid)
        if b in base_to_id and base_to_id[b] != gid:
            print(
                f"[extract_camview] WARN: metadata-base 冲突 {b!r} -> "
                f"保留 {base_to_id[b]!r}，忽略 {gid!r}"
            )
        elif b not in base_to_id:
            base_to_id[b] = gid
    return base_to_id


def _object_index_counts_from_png(seg_path: str) -> Dict[int, int]:
    import numpy as np

    arr = _load_instance_segmentation_u16(seg_path)
    vals, counts = np.unique(arr.ravel(), return_counts=True)
    out: Dict[int, int] = {}
    for v, c in zip(vals.tolist(), counts.tolist()):
        vi = int(v)
        if vi <= 0:
            continue
        out[vi] = int(c)
    return out


def _objects_from_gt_for_names(
    blend_dir: str, root_names_in_order: List[str]
) -> List[Dict[str, Any]]:
    """按 ``root_names_in_order`` 从 ``GT.json`` 拷贝 ``id`` 匹配的物体条目。"""
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
    id_to_row = {
        str(o.get("id")): o for o in objs if isinstance(o, dict) and o.get("id") is not None
    }

    out: List[Dict[str, Any]] = []
    for key in root_names_in_order:
        row = id_to_row.get(key)
        if row is None:
            print(f"[extract_camview] WARN: in-view root {key!r} has no GT id, skipped")
            continue
        out.append(copy.deepcopy(row))
    return out


def build_instance_seg_view_payload(scene_id: str, blend_dir: str) -> Dict[str, Any]:
    """
    自 ``metadata.json`` 的 ``object_index_map`` + ``instance_segmentation.png``（整型 / uint16）
    统计各类 id 像素数；``>= MIN_VISIBLE_PIXELS``、map 可查且 metadata 名与 ``GT.json`` id
    对齐的记入 in-view（等价于镜内像素 ∩ 全局 instance 表 ∩ GT 有效物体）。不读取 ``bpy.object.pass_index``。
    """
    seg_path = os.path.join(blend_dir, INSTANCE_SEGMENTATION_PNG)
    if not os.path.isfile(seg_path):
        raise FileNotFoundError(
            f"缺少 {INSTANCE_SEGMENTATION_PNG}，无法构建 objects_in_view: {seg_path}"
        )

    object_index_map = _load_object_index_map(blend_dir)
    base_to_gt = _build_metadata_base_to_gt_id(blend_dir)
    counts = _object_index_counts_from_png(seg_path)

    visible_gt_ids: set[str] = set()
    for pi, pc in sorted(counts.items()):
        if pc < MIN_VISIBLE_PIXELS:
            continue
        key = str(int(pi))
        meta_name = object_index_map.get(key)
        if meta_name is None:
            print(
                f"[extract_camview] WARN: pass_index {pi} ({pc}px) 不在 "
                f"object_index_map 中，跳过"
            )
            continue
        mb = _metadata_label_to_base(meta_name)
        gid = base_to_gt.get(mb)
        if gid is None:
            print(
                f"[extract_camview] WARN: metadata 物体 {meta_name!r} "
                f"(base {mb!r}) 无对应 GT id，跳过"
            )
            continue
        visible_gt_ids.add(gid)

    names_ordered = sorted(visible_gt_ids)
    objects_out = _objects_from_gt_for_names(blend_dir, names_ordered)
    for o in objects_out:
        c = o.get("category")
        if c is not None:
            o["category"] = canonicalize_asset_category(str(c))
    categories = sorted(
        {str(o["category"]) for o in objects_out if o.get("category") is not None}
    )

    print(
        f"[extract_camview] metadata + instance seg: {len(names_ordered)} GT 物体 in-view "
        f"(≥{MIN_VISIBLE_PIXELS}px, GT-aligned), "
        f"{len(categories)} categories"
    )
    for n in names_ordered:
        print(f"    • {factory_to_category(n):30s}  {n}")

    return {
        "scene_id": scene_id,
        "categories_in_view": categories,
        "categories_in_view_count": len(categories),
        "objects_in_view_count": len(objects_out),
        "objects": objects_out,
    }


if __name__ == "__main__":
    import bpy

    d = os.path.dirname(os.path.abspath(bpy.data.filepath)) if bpy.data.filepath else ""
    if d:
        sid = scene_id_from_output_dir(d)
        build_instance_seg_view_payload(sid, d)
