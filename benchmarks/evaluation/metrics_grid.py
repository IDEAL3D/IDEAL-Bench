"""
D5 grid metrics (classroom / office / library): GCR and GSR.

Public API: ``evaluate_grid(gt_payload, pred_payload) -> dict``

Metrics
-------
GCR (Grid Coverage Rate)
    hit / G  where hit = number of GT cells with at least one pred inside,
    G = number of GT cells (cells with at least one GT object).
    Measures whether predicted objects are placed at the correct grid positions.

GSR (Grid Self-consistency Rate)
    Fraction of pred target objects that fall within τ = 0.3 m of a point on
    the grid inferred from the pred positions themselves.
    Measures how "grid-like" the predicted arrangement is, independent of GT
    position, scale, or orientation.

    Algorithm:
      1. Cluster pred x-coords and y-coords independently with τ_cluster = 0.3 m
         (1-D single-linkage).
      2. Self-fitted grid = all (x_cluster_center, y_cluster_center) pairs.
      3. Degenerate checks:
         - both axes yield 1 cluster → "degenerate_cluster" (all points heaped)
         - pred count N < 3 → "n_pred_insufficient" (2 points always get GSR = 1)
         - fill ratio N / (n_rows * n_cols) ≤ 0.5 → "sparse_grid" (too many
           lattice points vs preds; includes full scatter N×N lattice)
      4. For each pred point find nearest grid point; hit if distance ≤ τ_hit = 0.1 m.
      5. GSR = hit_count / N.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import sys
from pathlib import Path

from extract_grid import (
    GridParams,
    GridTarget,
    _xy,
    extract_grid_params,
    get_room_type,
    get_targets,
    load_objects_from_payload,
)

SUPPORTED_ROOMS = frozenset({"classroom", "office", "library"})

TAU_CLUSTER = 0.3  # metres — clustering tolerance (how close to be same row/col)
TAU_HIT     = 0.1  # metres — hit tolerance (how close to grid point counts as hit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _euclidean_xy(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def _cluster_1d(coords: List[float], eps: float) -> List[float]:
    """1D single-linkage clustering.

    Groups consecutive sorted values whose gap is <= eps into one cluster.
    Returns the mean of each cluster as the representative coordinate.
    """
    if not coords:
        return []
    sorted_c = sorted(coords)
    clusters: List[List[float]] = [[sorted_c[0]]]
    for c in sorted_c[1:]:
        if c - clusters[-1][-1] <= eps:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return [sum(cl) / len(cl) for cl in clusters]


# ---------------------------------------------------------------------------
# GCR helpers
# ---------------------------------------------------------------------------


def _select_hit_pred_objects(
    gt_centers: List[Tuple[float, float]],
    pred_centers: List[Tuple[float, float]],
    pred_objects: List[dict],
    params: GridParams,
) -> Tuple[int, List[Tuple[float, dict]]]:
    """For each GT cell, find the nearest pred inside it (if any).

    Returns (G, hit_pairs) where:
      G         = number of GT cells (cells with at least one GT center)
      hit_pairs = list of (distance_to_GT_center, pred_object) — one entry per hit cell.
    """
    hit_pairs: List[Tuple[float, dict]] = []
    G = 0

    for cell in params.cells:
        cx, cy, hw, hh = cell

        gt_in_cell = [(gx, gy) for gx, gy in gt_centers
                      if abs(gx - cx) <= hw and abs(gy - cy) <= hh]
        if not gt_in_cell:
            continue
        G += 1

        pred_in_cell = [
            (px, py, o)
            for (px, py), o in zip(pred_centers, pred_objects)
            if abs(px - cx) <= hw and abs(py - cy) <= hh
        ]
        if not pred_in_cell:
            continue

        gx, gy = gt_in_cell[0]
        best = min(pred_in_cell, key=lambda t: _euclidean_xy(t[0], t[1], gx, gy))
        hit_pairs.append((_euclidean_xy(best[0], best[1], gx, gy), best[2]))

    return G, hit_pairs


# ---------------------------------------------------------------------------
# GCR
# ---------------------------------------------------------------------------


def grid_coverage_score(G: int, hit: int) -> Dict[str, Any]:
    """GCR = hit / G."""
    if G == 0:
        return {"gcr": None, "hit_cells": 0, "gt_cells": 0,
                "warning": "no GT cells found"}
    return {"gcr": hit / G, "hit_cells": hit, "gt_cells": G}


# ---------------------------------------------------------------------------
# GSR
# ---------------------------------------------------------------------------


def metric_gsr(pred_centers: List[Tuple[float, float]]) -> Dict[str, Any]:
    """GSR (Grid Self-consistency Rate): fraction of pred points on their own best-fit grid.

    Parameters
    ----------
    pred_centers : XY positions of all pred target objects for this label group.

    Returns
    -------
    dict with keys: gsr (float|None), skip_reason (str), hit_pred, n_pred, n_rows, n_cols.
    """
    N = len(pred_centers)
    if N < 3:
        return {
            "gsr": None,
            "skip_reason": "n_pred_insufficient",
            "n_pred": N,
            "hit_pred": None,
            "n_rows": None,
            "n_cols": None,
        }

    xs = [p[0] for p in pred_centers]
    ys = [p[1] for p in pred_centers]

    x_centers = _cluster_1d(xs, TAU_CLUSTER)
    y_centers = _cluster_1d(ys, TAU_CLUSTER)

    n_cols = len(x_centers)
    n_rows = len(y_centers)

    if n_rows == 1 and n_cols == 1:
        return {
            "gsr": None,
            "skip_reason": "degenerate_cluster",
            "n_pred": N,
            "hit_pred": None,
            "n_rows": 1,
            "n_cols": 1,
        }

    # Fill ratio: N / (n_rows * n_cols). For scattered points each becomes its
    # own row+col → n_rows*n_cols >> N → fill ≈ 0 → every pred sits on its own
    # grid point → GSR would be trivially 1.0. Reject such cases.
    fill_ratio = N / (n_rows * n_cols)
    if fill_ratio <= 0.5:
        return {
            "gsr": None,
            "skip_reason": "sparse_grid",
            "n_pred": N,
            "hit_pred": None,
            "n_rows": n_rows,
            "n_cols": n_cols,
        }

    # Self-fitted grid: all (x_center, y_center) combinations
    grid_pts: List[Tuple[float, float]] = [
        (cx, ry) for ry in y_centers for cx in x_centers
    ]

    hits = sum(
        1 for px, py in pred_centers
        if min(math.hypot(px - gx, py - gy) for gx, gy in grid_pts) <= TAU_HIT
    )

    return {
        "gsr": hits / N,
        "skip_reason": "",
        "hit_pred": hits,
        "n_pred": N,
        "n_rows": n_rows,
        "n_cols": n_cols,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate_grid(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """D5 driver: one result entry per layout target group."""
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
            entry["gcr"] = None
            entry["gsr"] = None
            results.append(entry)
            continue

        gt_centers = [(x, y) for x, y, _ in target.hits]

        gt_label_lower = target.label.lower()

        def _match_label(obj: dict, _lbl: str = gt_label_lower) -> bool:
            cat = str(obj.get("category", "")).strip().lower()
            if _lbl in ("desk", "chair", "block"):
                return _lbl in cat
            return cat == _lbl

        pred_matched = [o for o in pred_objects if _match_label(o)]
        pred_centers = [(_xy(o)[0], _xy(o)[1]) for o in pred_matched if _xy(o)]

        params = extract_grid_params(gt_centers)
        if params is None:
            entry["skip_reason"] = "n_gt_insufficient(n<2 after extraction)"
            entry["gcr"] = None
            entry["gsr"] = None
            results.append(entry)
            continue

        entry["grid_params"] = {
            "R": params.R, "C": params.C,
            "row_spacing": params.row_spacing,
            "col_spacing": params.col_spacing,
        }

        G, hit_pairs = _select_hit_pred_objects(
            gt_centers, pred_centers, pred_matched, params
        )
        entry["gcr"] = grid_coverage_score(G, len(hit_pairs))
        entry["gsr"] = metric_gsr(pred_centers)

        results.append(entry)

    return {
        "applicable": True,
        "room_type":  room_type,
        "targets":    results,
    }