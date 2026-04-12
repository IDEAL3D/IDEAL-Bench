"""
IDEAL 评测指标（几何与空间准确性，2.3.1）

本文件按“每个指标一个函数”的方式组织，支持如下核心量：
- N: GT 物体总数
- K: 成功匹配到 GT 的预测物体数（识别成功）
- M: 幻觉物体数（预测中无法匹配到任何 GT）
- Q: 发生不可接受几何重叠的预测物体数
- P: 与房间包络线相交或越界的预测物体数

默认假设：
1) 以 category 作为第一层语义对齐，类别内再做几何匹配。
2) 位置误差默认只计算 XY 平面欧氏距离（与用户需求一致，避免 Z 锚点定义差异影响）。
3) BBox-IoU 按 3D AABB 计算；如果给了 yaw，会先将局部 [W, D] 旋转到世界轴，再取世界轴对齐包围盒。
4) 旋转误差仅在“旋转敏感类别”上统计。

可通过 CLI 直接运行：
python benchmarks/evaluation/metrics.py \
  --gt path/to/GT_or_objects_in_view.json \
  --pred path/to/pred.json
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


# ------------------------------
# 数据结构与基础工具
# ------------------------------


@dataclass(frozen=True)
class AABB:
    """3D 轴对齐包围盒。"""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float


@dataclass(frozen=True)
class MatchedPair:
    """GT 与 Pred 的匹配对。"""

    gt_idx: int
    pred_idx: int


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_objects(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    兼容两类输入格式：
    - 标准预测/GT：{"scene": {"objects": [...]}}
    - objects_in_view：{"objects": [...]}
    """
    scene = payload.get("scene")
    if isinstance(scene, dict) and isinstance(scene.get("objects"), list):
        return scene["objects"]
    objs = payload.get("objects")
    return objs if isinstance(objs, list) else []


def _extract_room_size(payload: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    """
    尝试从 payload 中读取 room.size = [W, D, H]。
    读取失败返回 None。
    """
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        return None
    room = scene.get("room")
    if not isinstance(room, dict):
        return None
    size = room.get("size")
    if (
        isinstance(size, list)
        and len(size) == 3
        and all(isinstance(v, (int, float)) for v in size)
    ):
        return float(size[0]), float(size[1]), float(size[2])
    return None


def _safe_category(obj: Dict[str, Any]) -> str:
    c = obj.get("category", "")
    return str(c).strip().lower()


def _safe_position(obj: Dict[str, Any]) -> Tuple[float, float, float]:
    p = obj.get("position", [0.0, 0.0, 0.0])
    if not isinstance(p, list) or len(p) != 3:
        return 0.0, 0.0, 0.0
    return float(p[0]), float(p[1]), float(p[2])


def _safe_dimensions(obj: Dict[str, Any]) -> Tuple[float, float, float]:
    d = obj.get("dimensions", [0.0, 0.0, 0.0])
    if not isinstance(d, list) or len(d) != 3:
        return 0.0, 0.0, 0.0
    # 避免负值/异常值导致 AABB 计算反转
    return max(0.0, float(d[0])), max(0.0, float(d[1])), max(0.0, float(d[2]))


def _safe_yaw(obj: Dict[str, Any]) -> Optional[float]:
    yaw = obj.get("rotation_yaw", None)
    if yaw is None:
        return None
    try:
        return float(yaw) % 360.0
    except (TypeError, ValueError):
        return None


def euclidean_distance(
    a: Sequence[float],
    b: Sequence[float],
    *,
    use_xy_only: bool = True,
) -> float:
    """欧氏距离（默认仅 XY）。"""
    if use_xy_only:
        return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))
    dx = float(a[0]) - float(b[0])
    dy = float(a[1]) - float(b[1])
    dz = float(a[2]) - float(b[2])
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _oriented_xy_to_world_aabb_half_extents(
    width: float,
    depth: float,
    yaw_deg: Optional[float],
) -> Tuple[float, float]:
    """
    将局部平面尺寸 [W, D] 根据 yaw 旋转到世界轴后，计算世界轴对齐 AABB 的半长。

    若 yaw 缺失，则按未旋转处理。
    """
    hx_local = width / 2.0
    hy_local = depth / 2.0
    if yaw_deg is None:
        return hx_local, hy_local

    rad = math.radians(yaw_deg)
    c = abs(math.cos(rad))
    s = abs(math.sin(rad))
    hx_world = c * hx_local + s * hy_local
    hy_world = s * hx_local + c * hy_local
    return hx_world, hy_world


def object_to_aabb(
    obj: Dict[str, Any],
    *,
    z_anchor: str = "center",
) -> AABB:
    """
    把单个对象转为 3D AABB。

    参数:
    - z_anchor:
      - "center": position.z 视为几何中心（更贴近当前 GT 导出注释）
      - "bottom": position.z 视为底中心（旧约定兼容）
    """
    x, y, z = _safe_position(obj)
    w, d, h = _safe_dimensions(obj)
    yaw = _safe_yaw(obj)

    hx, hy = _oriented_xy_to_world_aabb_half_extents(w, d, yaw)

    if z_anchor == "bottom":
        min_z = z
        max_z = z + h
    else:
        hz = h / 2.0
        min_z = z - hz
        max_z = z + hz

    return AABB(
        min_x=x - hx,
        min_y=y - hy,
        min_z=min_z,
        max_x=x + hx,
        max_y=y + hy,
        max_z=max_z,
    )


def aabb_intersection_volume(a: AABB, b: AABB) -> float:
    ix = max(0.0, min(a.max_x, b.max_x) - max(a.min_x, b.min_x))
    iy = max(0.0, min(a.max_y, b.max_y) - max(a.min_y, b.min_y))
    iz = max(0.0, min(a.max_z, b.max_z) - max(a.min_z, b.min_z))
    return ix * iy * iz


def aabb_volume(box: AABB) -> float:
    return max(0.0, box.max_x - box.min_x) * max(0.0, box.max_y - box.min_y) * max(
        0.0, box.max_z - box.min_z
    )


def bbox_iou_3d(a: AABB, b: AABB) -> float:
    inter = aabb_intersection_volume(a, b)
    if inter <= 0.0:
        return 0.0
    union = aabb_volume(a) + aabb_volume(b) - inter
    if union <= 0.0:
        return 0.0
    return inter / union


def angular_difference_deg(a_deg: float, b_deg: float) -> float:
    """两个 yaw 的最小角度差，范围 [0, 180]。"""
    d = abs((a_deg - b_deg) % 360.0)
    return min(d, 360.0 - d)


# ------------------------------
# 匹配与全局计数（N/K/M）
# ------------------------------


def match_by_category_and_distance(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    *,
    use_xy_only: bool = True,
) -> Tuple[List[MatchedPair], Set[int], Set[int]]:
    """
    先按 category 分桶，再在每个桶内做“最短距离优先”贪心匹配。

    返回:
    - matched_pairs: 匹配对列表
    - unmatched_gt: 未被匹配的 GT 索引集合
    - unmatched_pred: 未匹配到 GT 的 Pred 索引集合（可视作 hallucination 候选）

    说明:
    - 该实现不依赖 scipy，便于在最小环境中运行。
    - 若后续追求最优全局匹配，可替换为 Hungarian。
    """
    gt_by_cat: Dict[str, List[int]] = {}
    pred_by_cat: Dict[str, List[int]] = {}

    for i, obj in enumerate(gt_objects):
        gt_by_cat.setdefault(_safe_category(obj), []).append(i)
    for j, obj in enumerate(pred_objects):
        pred_by_cat.setdefault(_safe_category(obj), []).append(j)

    matched: List[MatchedPair] = []
    used_gt: Set[int] = set()
    used_pred: Set[int] = set()

    all_cats = set(gt_by_cat.keys()) | set(pred_by_cat.keys())
    for cat in all_cats:
        g_idx = gt_by_cat.get(cat, [])
        p_idx = pred_by_cat.get(cat, [])
        if not g_idx or not p_idx:
            continue

        candidates: List[Tuple[float, int, int]] = []
        for gi in g_idx:
            gp = _safe_position(gt_objects[gi])
            for pj in p_idx:
                pp = _safe_position(pred_objects[pj])
                dist = euclidean_distance(gp, pp, use_xy_only=use_xy_only)
                candidates.append((dist, gi, pj))

        # 最短距离优先贪心配对
        candidates.sort(key=lambda x: x[0])
        for _, gi, pj in candidates:
            if gi in used_gt or pj in used_pred:
                continue
            used_gt.add(gi)
            used_pred.add(pj)
            matched.append(MatchedPair(gt_idx=gi, pred_idx=pj))

    unmatched_gt = set(range(len(gt_objects))) - used_gt
    unmatched_pred = set(range(len(pred_objects))) - used_pred
    return matched, unmatched_gt, unmatched_pred


# ------------------------------
# 指标 1: 位置错误率
# ------------------------------


def metric_position_error_rate(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    use_xy_only: bool = True,
    room_size: Optional[Tuple[float, float, float]] = None,
) -> Dict[str, Any]:
    """
    位置误差（核心几何指标）。

    输出:
    - mean_error_m: 匹配对的平均欧氏误差（米）
    - median_error_m: 中位误差（米）
    - position_error_rate: 归一化误差率（mean_error / room_diag），若缺 room_size 则为 None
    - evaluated_pairs: 参与计算的匹配对数量

    注:
    - 按你的要求，默认 use_xy_only=True（仅 XY 平面）。
    """
    errors: List[float] = []
    for pair in matched_pairs:
        gp = _safe_position(gt_objects[pair.gt_idx])
        pp = _safe_position(pred_objects[pair.pred_idx])
        errors.append(euclidean_distance(gp, pp, use_xy_only=use_xy_only))

    if not errors:
        return {
            "mean_error_m": None,
            "median_error_m": None,
            "position_error_rate": None,
            "evaluated_pairs": 0,
            "use_xy_only": use_xy_only,
        }

    errors_sorted = sorted(errors)
    n = len(errors_sorted)
    mean_err = sum(errors_sorted) / n
    median_err = (
        errors_sorted[n // 2]
        if n % 2 == 1
        else (errors_sorted[n // 2 - 1] + errors_sorted[n // 2]) / 2.0
    )

    rate = None
    if room_size is not None:
        w, d, h = room_size
        diag = math.hypot(w, d) if use_xy_only else math.sqrt(w * w + d * d + h * h)
        if diag > 1e-8:
            rate = mean_err / diag

    return {
        "mean_error_m": mean_err,
        "median_error_m": median_err,
        "position_error_rate": rate,
        "evaluated_pairs": n,
        "use_xy_only": use_xy_only,
    }


# ------------------------------
# 指标 2: BBOX-IoU
# ------------------------------


def metric_bbox_iou(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    iou_threshold: float = 0.1,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """
    BBox-IoU（仅统计已匹配对象的 IoU）。

    输出:
    - mean_iou: 匹配对 IoU 均值
    - iou_list: 每个匹配对 IoU
    - iou_pass_count: IoU >= threshold 的数量
    - iou_pass_rate_over_N: 通过数量 / N（N=GT物体数，符合“理想有 N 个 IoU”语义）
    - iou_pass_rate_over_K: 通过数量 / K（K=匹配数）
    """
    pairs = list(matched_pairs)
    ious: List[float] = []

    for pair in pairs:
        gt_box = object_to_aabb(gt_objects[pair.gt_idx], z_anchor=z_anchor)
        pr_box = object_to_aabb(pred_objects[pair.pred_idx], z_anchor=z_anchor)
        ious.append(bbox_iou_3d(gt_box, pr_box))

    k = len(pairs)
    n = len(gt_objects)
    pass_count = sum(1 for v in ious if v >= iou_threshold)
    mean_iou = (sum(ious) / k) if k > 0 else None

    return {
        "mean_iou": mean_iou,
        "iou_threshold": iou_threshold,
        "iou_list": ious,
        "iou_pass_count": pass_count,
        "iou_pass_rate_over_N": (pass_count / n) if n > 0 else None,
        "iou_pass_rate_over_K": (pass_count / k) if k > 0 else None,
        "evaluated_pairs": k,
    }


# ------------------------------
# 指标 3: 旋转错误率（可选）
# ------------------------------


def metric_rotation_error_rate(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    rotation_sensitive_categories: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    旋转误差率（建议只在旋转敏感类别上算）。

    默认敏感类别: bed / sofa / chair / table / desk / cabinet / bookcase

    输出:
    - mean_abs_error_deg: 平均绝对角度差（度）
    - rotation_error_rate: 归一化角度误差（mean_abs_error_deg / 180）
    - evaluated_pairs: 实际参与计算的匹配对数量
    """
    if rotation_sensitive_categories is None:
        rotation_sensitive_categories = {
            "bed",
            "sofa",
            "chair",
            "table",
            "desk",
            "cabinet",
            "side_table",
            "simple_bookcase",
            "single_cabinet",
            "bookcase",
        }

    errors: List[float] = []
    for pair in matched_pairs:
        gt_obj = gt_objects[pair.gt_idx]
        pr_obj = pred_objects[pair.pred_idx]

        cat = _safe_category(gt_obj)
        if cat not in rotation_sensitive_categories:
            continue

        gt_yaw = _safe_yaw(gt_obj)
        pr_yaw = _safe_yaw(pr_obj)
        # 任一方 yaw 为空（如 lamp/plant）则跳过
        if gt_yaw is None or pr_yaw is None:
            continue

        errors.append(angular_difference_deg(gt_yaw, pr_yaw))

    if not errors:
        return {
            "mean_abs_error_deg": None,
            "rotation_error_rate": None,
            "evaluated_pairs": 0,
            "rotation_sensitive_categories": sorted(rotation_sensitive_categories),
        }

    mean_abs = sum(errors) / len(errors)
    return {
        "mean_abs_error_deg": mean_abs,
        "rotation_error_rate": mean_abs / 180.0,
        "evaluated_pairs": len(errors),
        "rotation_sensitive_categories": sorted(rotation_sensitive_categories),
    }


# ------------------------------
# 指标 4: 物体碰撞率（与 GT 无关）
# ------------------------------


def metric_object_collision_rate(
    pred_objects: List[Dict[str, Any]],
    *,
    denominator_n: int,
    overlap_iou_threshold: float = 0.03,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """
    物体碰撞率 Q/N。

    定义:
    - 若两个预测物体 3D IoU >= overlap_iou_threshold，认为发生“不可接受重叠”。
    - Q 记为“参与至少一次重叠的预测物体数量（去重）”。

    参数:
    - denominator_n: 你定义中的 N（通常用 GT 物体数）。
    """
    colliding_obj_indices: Set[int] = set()
    n_pred = len(pred_objects)

    boxes = [object_to_aabb(o, z_anchor=z_anchor) for o in pred_objects]
    for i in range(n_pred):
        for j in range(i + 1, n_pred):
            iou = bbox_iou_3d(boxes[i], boxes[j])
            if iou >= overlap_iou_threshold:
                colliding_obj_indices.add(i)
                colliding_obj_indices.add(j)

    q = len(colliding_obj_indices)
    return {
        "Q_colliding_objects": q,
        "collision_rate_Q_over_N": (q / denominator_n) if denominator_n > 0 else None,
        "overlap_iou_threshold": overlap_iou_threshold,
    }


# ------------------------------
# 指标 5: 物体出界率（与 GT 无关）
# ------------------------------


def _is_box_out_of_room(box: AABB, room_size: Tuple[float, float, float]) -> bool:
    """
    判断 AABB 是否与房间包络线相交或越界。

    房间包络假设:
    - XY 平面以 (0,0) 为中心
    - X 范围 [-W/2, W/2]
    - Y 范围 [-D/2, D/2]
    - Z 范围 [0, H]
    """
    w, d, h = room_size
    x_min, x_max = -w / 2.0, w / 2.0
    y_min, y_max = -d / 2.0, d / 2.0
    z_min, z_max = 0.0, h

    # 与墙体相交或在外部均算违规
    return (
        box.min_x < x_min
        or box.max_x > x_max
        or box.min_y < y_min
        or box.max_y > y_max
        or box.min_z < z_min
        or box.max_z > z_max
    )


def metric_out_of_bounds_rate(
    pred_objects: List[Dict[str, Any]],
    *,
    room_size: Tuple[float, float, float],
    denominator_n: int,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """
    物体出界率 P/N。

    - P: 越界（含穿墙/超出房间包络）的预测物体数量
    - N: 全局定义中的 GT 物体数
    """
    violating_indices: List[int] = []
    for idx, obj in enumerate(pred_objects):
        box = object_to_aabb(obj, z_anchor=z_anchor)
        if _is_box_out_of_room(box, room_size):
            violating_indices.append(idx)

    p = len(violating_indices)
    return {
        "P_out_of_bounds_objects": p,
        "out_of_bounds_rate_P_over_N": (p / denominator_n) if denominator_n > 0 else None,
        "violating_indices": violating_indices,
    }


# ------------------------------
# 总评估入口
# ------------------------------


def evaluate_scene(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
    *,
    use_xy_only_for_position: bool = True,
    iou_threshold: float = 0.1,
    collision_iou_threshold: float = 0.03,
    z_anchor: str = "bottom",
) -> Dict[str, Any]:
    """
    汇总单场景所有指标。

    返回结构包含:
    - global_counts: N/K/M/FN
    - geometry_spatial_accuracy: 位置、IoU、旋转
    - gt_independent: 碰撞率、出界率
    """
    gt_objects = _extract_objects(gt_payload)
    pred_objects = _extract_objects(pred_payload)

    matched_pairs, unmatched_gt, unmatched_pred = match_by_category_and_distance(
        gt_objects,
        pred_objects,
        use_xy_only=use_xy_only_for_position,
    )

    n = len(gt_objects)
    k = len(matched_pairs)
    m = len(unmatched_pred)
    fn = len(unmatched_gt)

    # room_size 优先用 GT（保证评估基准一致），否则回退预测 room
    room_size = _extract_room_size(gt_payload) or _extract_room_size(pred_payload)

    position = metric_position_error_rate(
        gt_objects,
        pred_objects,
        matched_pairs,
        use_xy_only=use_xy_only_for_position,
        room_size=room_size,
    )

    iou = metric_bbox_iou(
        gt_objects,
        pred_objects,
        matched_pairs,
        iou_threshold=iou_threshold,
        z_anchor=z_anchor,
    )

    rotation = metric_rotation_error_rate(gt_objects, pred_objects, matched_pairs)

    collision = metric_object_collision_rate(
        pred_objects,
        denominator_n=n,
        overlap_iou_threshold=collision_iou_threshold,
        z_anchor=z_anchor,
    )

    out_of_bounds = (
        metric_out_of_bounds_rate(
            pred_objects,
            room_size=room_size,
            denominator_n=n,
            z_anchor=z_anchor,
        )
        if room_size is not None
        else {
            "P_out_of_bounds_objects": None,
            "out_of_bounds_rate_P_over_N": None,
            "violating_indices": [],
            "warning": "room_size is missing in both GT and prediction payload",
        }
    )

    return {
        "global_counts": {
            "N_gt_objects": n,
            "K_matched_objects": k,
            "M_hallucinated_objects": m,
            "FN_missed_gt_objects": fn,
            "recognition_rate_K_over_N": (k / n) if n > 0 else None,
            "hallucination_rate_M_over_N": (m / n) if n > 0 else None,
        },
        "geometry_spatial_accuracy": {
            "position_error": position,
            "bbox_iou": iou,
            "rotation_error": rotation,
        },
        "gt_independent": {
            "object_collision": collision,
            "object_out_of_bounds": out_of_bounds,
        },
        "matching": {
            "matched_pairs": [
                {"gt_idx": p.gt_idx, "pred_idx": p.pred_idx} for p in matched_pairs
            ],
            "unmatched_gt_indices": sorted(unmatched_gt),
            "unmatched_pred_indices": sorted(unmatched_pred),
        },
        "settings": {
            "use_xy_only_for_position": use_xy_only_for_position,
            "iou_threshold": iou_threshold,
            "collision_iou_threshold": collision_iou_threshold,
            "z_anchor": z_anchor,
        },
    }


# ------------------------------
# CLI
# ------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IDEAL 几何与空间准确性评测（2.3.1）"
    )
    parser.add_argument("--gt", required=True, help="GT JSON 路径")
    parser.add_argument("--pred", required=True, help="预测 JSON 路径")
    parser.add_argument(
        "--use-xyz",
        action="store_true",
        help="位置误差使用 XYZ（默认仅 XY）",
    )
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=0.1,
        help="BBox-IoU 通过阈值（默认 0.1）",
    )
    parser.add_argument(
        "--collision-iou-threshold",
        type=float,
        default=0.03,
        help="预测对象碰撞判定 IoU 阈值（默认 0.03）",
    )
    parser.add_argument(
        "--z-anchor",
        choices=["center", "bottom"],
        default="bottom",
        help="AABB 的 Z 锚点假设（默认 bottom）",
    )

    args = parser.parse_args()

    gt_payload = _load_json(args.gt)
    pred_payload = _load_json(args.pred)

    result = evaluate_scene(
        gt_payload,
        pred_payload,
        use_xy_only_for_position=not args.use_xyz,
        iou_threshold=args.iou_threshold,
        collision_iou_threshold=args.collision_iou_threshold,
        z_anchor=args.z_anchor,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
