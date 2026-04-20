"""
metrics_grid.py

维度五：排列结构识别（Layout Pattern Recognition）
仅对 classroom / office / library 三种场景激活。

公开接口：
  evaluate_grid(gt_payload, pred_payload) -> dict

三个并列指标：
  1. grid_coverage_score      网格系数
  2. in_grid_position_error   格内位置偏差（米）
  3. yaw_error                Yaw 误差（度）
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from extract_grid import (
    GridParams,
    GridTarget,
    _median,
    _xy,
    extract_grid_params,
    get_room_type,
    get_targets,
    load_objects_from_payload,
)

SUPPORTED_ROOMS = frozenset({"classroom", "office", "library"})


# ── 工具 ──────────────────────────────────────────────────────

def _safe_yaw(obj: dict) -> Optional[float]:
    yaw = obj.get("rotation_yaw")
    if yaw is None:
        return None
    try:
        return float(yaw) % 360.0
    except (TypeError, ValueError):
        return None


def _angular_diff(a: float, b: float) -> float:
    """两个 yaw 的最小角度差，范围 [0, 180]。"""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _euclidean_xy(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


# ── 指标一：网格系数 ──────────────────────────────────────────

def grid_coverage_score(
    gt_centers: List[Tuple[float, float]],
    pred_centers: List[Tuple[float, float]],
    params: GridParams,
) -> Dict[str, Any]:
    """
    网格系数 = 命中格子数 / 有GT的格子数G。

    有GT的格子：该格子内至少有一个 GT 中心点落入。
    命中：该有GT格子内至少有一个 pred 中心点落入。
    一个格子多个 pred 落入只算命中一次。
    pred 落在无GT格子里不计入分子也不惩罚。
    """
    # 找出有GT的格子
    gt_cells: List[Tuple[float, float, float, float]] = []
    for cell in params.cells:
        cx, cy, hw, hh = cell
        has_gt = any(abs(gx - cx) <= hw and abs(gy - cy) <= hh
                     for gx, gy in gt_centers)
        if has_gt:
            gt_cells.append(cell)

    G = len(gt_cells)
    if G == 0:
        return {"grid_coverage_score": None, "hit_cells": 0,
                "gt_cells": 0, "warning": "no GT cells found"}

    hit = 0
    for cell in gt_cells:
        cx, cy, hw, hh = cell
        if any(abs(px - cx) <= hw and abs(py - cy) <= hh
               for px, py in pred_centers):
            hit += 1

    return {
        "grid_coverage_score": hit / G,
        "hit_cells": hit,
        "gt_cells": G,
    }


# ── 指标二：格内位置偏差 ──────────────────────────────────────

def in_grid_position_error(
    gt_centers: List[Tuple[float, float]],
    pred_centers: List[Tuple[float, float]],
    params: GridParams,
) -> Dict[str, Any]:
    """
    格内位置偏差 = 命中格子中，最近 pred 到 GT 中心点的 XY 欧氏距离中位数。

    对每个有GT的命中格子：
      - 找出落入该格子的所有 pred 点
      - 取距离该格子 GT 中心点最近的那个 pred
      - 计算欧氏距离
    取所有命中格子的距离中位数。
    """
    distances: List[float] = []

    for cell in params.cells:
        cx, cy, hw, hh = cell

        # 找该格子内的 GT 点
        gt_in_cell = [(gx, gy) for gx, gy in gt_centers
                      if abs(gx - cx) <= hw and abs(gy - cy) <= hh]
        if not gt_in_cell:
            continue  # 无GT的格子跳过

        # 找该格子内的 pred 点
        pred_in_cell = [(px, py) for px, py in pred_centers
                        if abs(px - cx) <= hw and abs(py - cy) <= hh]
        if not pred_in_cell:
            continue  # 未命中，跳过

        # GT 中心点（理论上只有一个，取第一个）
        gx, gy = gt_in_cell[0]

        # 取距离 GT 最近的 pred
        best_dist = min(_euclidean_xy(px, py, gx, gy) for px, py in pred_in_cell)
        distances.append(best_dist)

    if not distances:
        return {"in_grid_position_error_m": None,
                "evaluated_cells": 0,
                "warning": "no hit cells, cannot compute in-grid PE"}

    return {
        "in_grid_position_error_m": _median(distances),
        "evaluated_cells": len(distances),
    }


# ── 指标三：Yaw 误差 ──────────────────────────────────────────

def metric_yaw_error(
    gt_objects: List[dict],
    pred_objects: List[dict],
    label: str,
) -> Dict[str, Any]:
    """
    Yaw 误差 = pred yaw 与 GT 统一 yaw 值的角度差中位数。

    GT 同类物体 yaw 唯一（程序生成保证），取第一个有效值为参考。
    对所有 pred 物体计算角度差，取中位数。
    """
    # 取 GT 统一 yaw
    gt_yaw: Optional[float] = None
    for o in gt_objects:
        y = _safe_yaw(o)
        if y is not None:
            gt_yaw = y
            break

    if gt_yaw is None:
        return {"yaw_error_deg": None, "evaluated_count": 0,
                "warning": "GT yaw is null for all objects"}

    errors: List[float] = []
    for o in pred_objects:
        py = _safe_yaw(o)
        if py is not None:
            errors.append(_angular_diff(gt_yaw, py))

    if not errors:
        return {"yaw_error_deg": None, "evaluated_count": 0,
                "warning": "no valid pred yaw found"}

    return {
        "yaw_error_deg": _median(errors),
        "gt_yaw_reference": gt_yaw,
        "evaluated_count": len(errors),
    }


# ── 总入口 ────────────────────────────────────────────────────

def evaluate_grid(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    维度五总入口。

    返回结构：
    {
        "applicable": bool,          # 该场景是否适用维度五
        "room_type": str,
        "targets": [                 # 每个 target 的结果
            {
                "label": str,
                "skip_reason": str,  # 非空表示跳过，原因见此
                "note": str,
                "grid_coverage": {...},
                "in_grid_pe": {...},
                "yaw_error": {...},
            }
        ]
    }
    """
    room_type = get_room_type(gt_payload)

    if room_type not in SUPPORTED_ROOMS:
        return {"applicable": False, "room_type": room_type}

    gt_objects   = load_objects_from_payload(gt_payload)
    pred_objects = load_objects_from_payload(pred_payload)

    targets: List[GridTarget] = get_targets(room_type, gt_objects)

    results = []
    for target in targets:
        entry: Dict[str, Any] = {
            "label":       target.label,
            "skip_reason": target.skip_reason,
            "note":        target.note,
        }

        if target.skip_reason:
            # 数量不足或无目标，跳过计算
            entry["grid_coverage"] = None
            entry["in_grid_pe"]    = None
            entry["yaw_error"]     = None
            results.append(entry)
            continue

        # GT 中心点
        gt_centers = [(x, y) for x, y, _ in target.hits]

        # Pred 中筛选同类物体（category 匹配）
        gt_label_lower = target.label.lower()

        def _match_label(obj: dict) -> bool:
            cat = str(obj.get("category", "")).strip().lower()
            oid = str(obj.get("id", ""))
            # category 直接匹配，或 id 含 desk 关键字（兼容 pred 不规范输出）
            if gt_label_lower in ("student_desk", "desk"):
                return "desk" in cat or "desk" in oid.lower()
            return cat == gt_label_lower

        pred_matched = [o for o in pred_objects if _match_label(o)]
        pred_centers = [(_xy(o)[0], _xy(o)[1]) for o in pred_matched if _xy(o)]

        # 提取网格参数
        params = extract_grid_params(gt_centers)
        if params is None:
            entry["skip_reason"]   = "n_a_insufficient(n<2 after extraction)"
            entry["grid_coverage"] = None
            entry["in_grid_pe"]    = None
            entry["yaw_error"]     = None
            results.append(entry)
            continue

        entry["grid_params"] = {
            "R": params.R, "C": params.C,
            "row_spacing": params.row_spacing,
            "col_spacing": params.col_spacing,
        }

        entry["grid_coverage"] = grid_coverage_score(gt_centers, pred_centers, params)
        entry["in_grid_pe"]    = in_grid_position_error(gt_centers, pred_centers, params)
        entry["yaw_error"]     = metric_yaw_error(
            [o for _, _, o in target.hits],
            pred_matched,
            target.label,
        )

        results.append(entry)

    return {
        "applicable": True,
        "room_type":  room_type,
        "targets":    results,
    }