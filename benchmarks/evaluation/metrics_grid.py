"""
metrics_grid.py — Dimension 5: layout pattern (grid) metrics for classroom / office / library.

Public: evaluate_grid(gt_payload, pred_payload) -> dict

Scores: grid_coverage_score, in_grid_position_error (m), yaw_error (deg).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from grid_utils import (
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


def _safe_yaw(obj: dict) -> Optional[float]:
    yaw = obj.get("rotation_yaw")
    if yaw is None:
        return None
    try:
        return float(yaw) % 360.0
    except (TypeError, ValueError):
        return None


def _angular_diff(a: float, b: float) -> float:
    """Smallest circular difference between two yaw angles in [0, 180]."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _euclidean_xy(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def grid_coverage_score(
    gt_centers: List[Tuple[float, float]],
    pred_centers: List[Tuple[float, float]],
    params: GridParams,
) -> Dict[str, Any]:
    """Hit cells with pred / cells that contain GT; pred-only cells ignored."""
    gt_cells: List[Tuple[float, float, float, float]] = []
    for cell in params.cells:
        cx, cy, hw, hh = cell
        has_gt = any(
            abs(gx - cx) <= hw and abs(gy - cy) <= hh for gx, gy in gt_centers
        )
        if has_gt:
            gt_cells.append(cell)

    G = len(gt_cells)
    if G == 0:
        return {
            "grid_coverage_score": None,
            "hit_cells": 0,
            "gt_cells": 0,
            "warning": "no GT cells found",
        }

    hit = 0
    for cell in gt_cells:
        cx, cy, hw, hh = cell
        if any(abs(px - cx) <= hw and abs(py - cy) <= hh for px, py in pred_centers):
            hit += 1

    return {
        "grid_coverage_score": hit / G,
        "hit_cells": hit,
        "gt_cells": G,
    }


def in_grid_position_error(
    gt_centers: List[Tuple[float, float]],
    pred_centers: List[Tuple[float, float]],
    params: GridParams,
) -> Dict[str, Any]:
    """Median XY distance from GT center to nearest pred in the same hit cell."""
    distances: List[float] = []

    for cell in params.cells:
        cx, cy, hw, hh = cell

        gt_in_cell = [
            (gx, gy)
            for gx, gy in gt_centers
            if abs(gx - cx) <= hw and abs(gy - cy) <= hh
        ]
        if not gt_in_cell:
            continue

        pred_in_cell = [
            (px, py)
            for px, py in pred_centers
            if abs(px - cx) <= hw and abs(py - cy) <= hh
        ]
        if not pred_in_cell:
            continue

        gx, gy = gt_in_cell[0]
        best_dist = min(_euclidean_xy(px, py, gx, gy) for px, py in pred_in_cell)
        distances.append(best_dist)

    if not distances:
        return {
            "in_grid_position_error_m": None,
            "evaluated_cells": 0,
            "warning": "no hit cells, cannot compute in-grid PE",
        }

    return {
        "in_grid_position_error_m": _median(distances),
        "evaluated_cells": len(distances),
    }


def metric_yaw_error(
    gt_objects: List[dict],
    pred_objects: List[dict],
) -> Dict[str, Any]:
    """Median angular error vs a single GT reference yaw (first valid GT)."""
    gt_yaw: Optional[float] = None
    for o in gt_objects:
        y = _safe_yaw(o)
        if y is not None:
            gt_yaw = y
            break

    if gt_yaw is None:
        return {
            "yaw_error_deg": None,
            "evaluated_count": 0,
            "warning": "GT yaw is null for all objects",
        }

    errors: List[float] = []
    for o in pred_objects:
        py = _safe_yaw(o)
        if py is not None:
            errors.append(_angular_diff(gt_yaw, py))

    if not errors:
        return {
            "yaw_error_deg": None,
            "evaluated_count": 0,
            "warning": "no valid pred yaw found",
        }

    return {
        "yaw_error_deg": _median(errors),
        "gt_yaw_reference": gt_yaw,
        "evaluated_count": len(errors),
    }


def evaluate_grid(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Run grid metrics; returns applicable flag, room_type, per-target scores."""
    room_type = get_room_type(gt_payload)

    if room_type not in SUPPORTED_ROOMS:
        return {"applicable": False, "room_type": room_type}

    gt_objects = load_objects_from_payload(gt_payload)
    pred_objects = load_objects_from_payload(pred_payload)

    targets: List[GridTarget] = get_targets(room_type, gt_objects)

    results = []
    for target in targets:
        entry: Dict[str, Any] = {
            "label": target.label,
            "skip_reason": target.skip_reason,
            "note": target.note,
        }

        if target.skip_reason:
            entry["grid_coverage"] = None
            entry["in_grid_pe"] = None
            entry["yaw_error"] = None
            results.append(entry)
            continue

        gt_centers = [(x, y) for x, y, _ in target.hits]

        gt_label_lower = target.label.lower()

        def _match_label(obj: dict) -> bool:
            cat = str(obj.get("category", "")).strip().lower()
            oid = str(obj.get("id", ""))
            if gt_label_lower in ("student_desk", "desk"):
                return "desk" in cat or "desk" in oid.lower()
            return cat == gt_label_lower

        pred_matched = [o for o in pred_objects if _match_label(o)]
        pred_centers = [(_xy(o)[0], _xy(o)[1]) for o in pred_matched if _xy(o)]

        params = extract_grid_params(gt_centers)
        if params is None:
            entry["skip_reason"] = "n_a_insufficient(n<2 after extraction)"
            entry["grid_coverage"] = None
            entry["in_grid_pe"] = None
            entry["yaw_error"] = None
            results.append(entry)
            continue

        entry["grid_params"] = {
            "R": params.R,
            "C": params.C,
            "row_spacing": params.row_spacing,
            "col_spacing": params.col_spacing,
        }

        entry["grid_coverage"] = grid_coverage_score(gt_centers, pred_centers, params)
        entry["in_grid_pe"] = in_grid_position_error(gt_centers, pred_centers, params)
        entry["yaw_error"] = metric_yaw_error(
            [o for _, _, o in target.hits],
            pred_matched,
        )

        results.append(entry)

    return {
        "applicable": True,
        "room_type": room_type,
        "targets": results,
    }
