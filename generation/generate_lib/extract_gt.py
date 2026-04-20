"""
Write ``GT.json`` from the currently opened Blender scene (single-room box pipeline).

Used by **generation finalize** (``generate_lib.core`` loads the scene with in-process ``bpy`` and
calls ``write_gt_json``) and any other Blender script that imports it. The CLI below also works with
any Python that has ``bpy`` and an already-open ``scene.blend``.

Object listing and room metrics use ``generation_common.asset_rules`` and ``generation_common.scene_geometry``.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

# 常量定义
ROOM_FLOOR_COLLECTION = "unique_assets:room_floor"
BLACKBOARD_CATEGORY = "blackboard"
SIDE_TABLE_CATEGORY = "side_table"
# TableDiningFactory 系（餐桌/会议桌/茶几）：出厂局部长边沿 X、语义正面沿短边；fallback +X 与 yaw/W-D 需同一套 +90° 修正。
TABLE_DINING_FAMILY_YAW_FIX = frozenset(
    {"coffee_table", "table_dining", "conference_table"}
)

def _rotation_yaw_not_applicable(category: str) -> bool:
    if category == "lamp" or category.endswith("_lamp"):
        return True
    if "_plant_" in f"_{category}_":
        return True
    if category == "rug":
        return True
    return False

# 导入项目内部依赖
_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

from configs.constants import ROOM_TYPES
from generation_common.asset_rules import (
    collect_assets,
    factory_to_category,
    find_render_camera,
    identify_camera_room,
    scene_id_from_output_dir,
)
from generation_common.scene_geometry import (
    combined_aabb,
    combined_aabb_root_local,
    is_floor,
    room_height,
    world_aabb,
)


def room_type_from_scene_dir(scene_dir: str) -> str:
    base = os.path.basename(os.path.normpath(scene_dir))
    if not base:
        return "unknown"
    prefix = base.split("_")[0].lower() or "unknown"
    if prefix not in ROOM_TYPES:
        return "unknown"
    return prefix


def _room_metrics_from_floor_collection() -> Optional[Dict[str, float]]:
    """Merge all ``*.floor`` meshes under ``unique_assets:room_floor``; fallback when camera raycast fails."""
    import bpy

    col = bpy.data.collections.get(ROOM_FLOOR_COLLECTION)
    if col is None:
        return None
    floor_meshes = [o for o in col.all_objects if o.type == "MESH" and is_floor(o)]
    if not floor_meshes:
        return None
    lo, hi = combined_aabb(floor_meshes)
    W = round(hi.x - lo.x, 2)
    D = round(hi.y - lo.y, 2)
    ztop = hi.z
    rid = floor_meshes[0].name.split(".")[0]
    H = round(room_height(rid, ztop), 2)
    return {"W": W, "D": D, "H": H}


def resolve_room_metrics_box(scene_dir: str | None = None) -> Optional[Dict[str, Any]]:
    """Room ``type`` + ``[W,D,H]`` from camera raycast to floor; else merged floor AABB (see PREPROCESS_DESIGN)."""
    import bpy

    if scene_dir is None:
        fp = getattr(bpy.data, "filepath", "") or ""
        if not fp:
            return None
        scene_dir = os.path.dirname(os.path.normpath(os.path.abspath(fp)))
    else:
        scene_dir = os.path.normpath(os.path.abspath(os.fspath(scene_dir)))

    room_type = room_type_from_scene_dir(scene_dir)
    info = identify_camera_room()
    if info is not None:
        return {
            "room_type": room_type,
            "W": info["W"],
            "D": info["D"],
            "H": info["H"],
        }
    fallback = _room_metrics_from_floor_collection()
    if fallback is None:
        return None
    return {"room_type": room_type, **fallback}


def _horizontal_unit(vx: float, vy: float) -> Optional[Tuple[float, float]]:
    hlen = math.hypot(vx, vy)
    if hlen < 1e-8:
        return None
    return (vx / hlen, vy / hlen)

def _global_polygon_normal_world(obj, polygon) -> Tuple[float, float, float]:
    _loc, rot, _scale = obj.matrix_world.decompose()
    rot_m = rot.to_matrix()
    n = rot_m @ polygon.normal
    n.normalize()
    return (float(n.x), float(n.y), float(n.z))

def _semantic_forward_xy(root, category: Optional[str] = None) -> Tuple[float, float]:
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
                sx += nx
                sy += ny
    except ImportError:
        pass

    hu = _horizontal_unit(sx, sy)
    if hu is not None:
        return hu

    mw = root.matrix_world
    # Fallback 逻辑
    if category == BLACKBOARD_CATEGORY:
        lv = mw.to_3x3() @ Vector((0.0, 1.0, 0.0))
    else:
        lv = mw.to_3x3() @ Vector((1.0, 0.0, 0.0))
    
    hu = _horizontal_unit(float(lv.x), float(lv.y))
    return hu if hu is not None else (0.0, 1.0)

def semantic_yaw_degrees(root, category: Optional[str] = None) -> int:
    fx, fy = _semantic_forward_xy(root, category=category)
    deg = math.degrees(math.atan2(-fx, fy))
    result = int(round(deg)) % 360
    if category in TABLE_DINING_FAMILY_YAW_FIX:
        result = (result + 90) % 360
    return result

def _canonical_dim_from_local_aabb(obj, meshes, category: str) -> List[float]:
    """
    核心改进：根据语义正面在局部空间中的指向，自动决定是否对调 X/Y。
    """
    # 1. 获取局部包围盒尺寸 (相当于 N 面板尺寸)
    lo, hi = combined_aabb_root_local(obj, meshes)
    local_dx = hi.x - lo.x
    local_dy = hi.y - lo.y
    local_dz = hi.z - lo.z

    # 2. 获取当前世界空间下的语义正面向量
    fw_x, fw_y = _semantic_forward_xy(obj, category=category)
    # 与 semantic_yaw_degrees 中 TABLE_DINING_FAMILY 的 +90° 一致，XY 上前向逆时针旋转 90° 再算 fw_local。
    if category in TABLE_DINING_FAMILY_YAW_FIX:
        fw_x, fw_y = -fw_y, fw_x
    fw_world = Vector((fw_x, fw_y, 0.0))

    # 3. 将世界正面向量逆旋转回局部空间，看它主要指向局部 X 还是局部 Y
    fw_local = obj.matrix_world.to_3x3().inverted() @ fw_world

    # 4. 对调逻辑
    # 如果正面沿局部 X 轴 (abs_x > abs_y)，说明局部 X 是深度 D，局部 Y 是宽度 W
    if abs(fw_local.x) > abs(fw_local.y):
        return [round(local_dy, 2), round(local_dx, 2), round(local_dz, 2)]
    else:
        # 如果正面沿局部 Y 轴，无需对调
        return [round(local_dx, 2), round(local_dy, 2), round(local_dz, 2)]

def build_gt_payload(room_metrics: Dict[str, Any], scene_dir: str) -> Dict[str, Any]:
    assets = collect_assets()
    objects_out: List[Dict[str, Any]] = []

    for obj in sorted(assets, key=lambda o: o.name):
        cat = factory_to_category(obj.name)

        hier = [obj] + list(obj.children_recursive)
        meshes = [o for o in hier if o.type == "MESH"]

        # ── 修复：用局部 AABB 几何中心转换到世界坐标替换 origin 作为 position ──
        # 原 origin 不保证在几何中心，约半数资产存在系统性偏移。
        # 统一约定：position = 局部空间 AABB 几何中心经世界矩阵变换后的世界坐标。
        # 相比直接用世界 AABB 中心，此方法不受旋转膨胀效应影响，对非对称模型更严谨。
        if meshes:
            lo_local, hi_local = combined_aabb_root_local(obj, meshes)
            local_center = Vector((
                (lo_local.x + hi_local.x) / 2.0,
                (lo_local.y + hi_local.y) / 2.0,
                (lo_local.z + hi_local.z) / 2.0,
            ))
            world_center = obj.matrix_world @ local_center
            pos = [round(world_center.x, 2), round(world_center.y, 2), round(world_center.z, 2)]
        else:
            loc = obj.matrix_world.to_translation()
            pos = [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)]

        # 1. 计算语义 Yaw
        yaw_deg = semantic_yaw_degrees(obj, category=cat)
        
        # SideTable 正方形判别逻辑
        if cat == SIDE_TABLE_CATEGORY and meshes:
            lo_tmp, hi_tmp = combined_aabb_root_local(obj, meshes)
            if abs((hi_tmp.x - lo_tmp.x) - (hi_tmp.y - lo_tmp.y)) < 0.05:
                yaw_deg = 0

        # 2. 计算规范化尺寸
        if not meshes:
            dim = [round(obj.dimensions.x, 2), round(obj.dimensions.y, 2), round(obj.dimensions.z, 2)]
        elif _rotation_yaw_not_applicable(cat):
            lo, hi = combined_aabb(meshes)
            dim = [round(hi.x-lo.x, 2), round(hi.y-lo.y, 2), round(hi.z-lo.z, 2)]
        else:
            # 使用新的对调逻辑提取尺寸
            dim = _canonical_dim_from_local_aabb(obj, meshes, cat)

        rotation_yaw_out = None if _rotation_yaw_not_applicable(cat) else yaw_deg

        objects_out.append({
            "id": obj.name,
            "category": cat,
            "position": pos,
            "dimensions": dim,
            "rotation_yaw": rotation_yaw_out,
        })

    return {
        "scene_id": scene_id_from_output_dir(scene_dir),
        "scene": {
            "room": {
                "type": room_metrics.get("room_type", "unknown"),
                "size": [room_metrics["W"], room_metrics["D"], room_metrics["H"]],
                "asset_count": len(objects_out),
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


def write_gt_json_from_blend_path(scene_dir: str | os.PathLike[str], blend_path: str | os.PathLike[str]) -> str:
    """Open ``blend_path`` with ``bpy``, resolve room metrics, write ``GT.json`` under ``scene_dir``.

    ``scene_dir`` is the output folder (same directory that contains ``scene.blend`` in typical
    layouts). Overwrites an existing ``GT.json``. Requires in-process ``bpy``.
    """
    scene_dir = os.path.normpath(os.path.abspath(os.fspath(scene_dir)))
    blend_fp = os.path.normpath(os.path.abspath(os.fspath(blend_path)))
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "write_gt_json_from_blend_path requires a Python that can import bpy "
            "(use the same conda env as Infinigen generation)."
        ) from e
    bpy.ops.wm.open_mainfile(filepath=blend_fp)
    metrics = resolve_room_metrics_box(scene_dir)
    if metrics is None:
        raise RuntimeError("resolve_room_metrics_box returned None after opening blend")
    return write_gt_json(scene_dir, metrics)


def write_gt_json_for_loaded_scene(scene_dir: str | os.PathLike[str]) -> str:
    """Like ``write_gt_json`` but assumes ``scene.blend`` is already open in the current ``bpy`` session."""
    scene_dir = os.path.normpath(os.path.abspath(os.fspath(scene_dir)))
    metrics = resolve_room_metrics_box(scene_dir)
    if metrics is None:
        raise RuntimeError("resolve_room_metrics_box returned None")
    return write_gt_json(scene_dir, metrics)


def _cli_main() -> None:
    """CLI when ``scene.blend`` is already open: ``python extract_gt.py -- <scene_dir>`` (requires ``bpy``)."""
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    if len(argv) < 1:
        print(
            "Usage: python extract_gt.py -- <scene_dir>  (requires bpy; blend must already be open)",
            file=sys.stderr,
        )
        sys.exit(1)
    scene_dir = os.path.normpath(os.path.abspath(argv[0]))
    metrics = resolve_room_metrics_box(scene_dir)
    if metrics is None:
        print("[extract_gt] ERROR: resolve_room_metrics_box returned None", file=sys.stderr)
        sys.exit(2)
    write_gt_json(scene_dir, metrics)


if __name__ == "__main__":
    _cli_main()