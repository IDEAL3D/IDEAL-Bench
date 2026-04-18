"""
match_objects.py — Canonical object matching module for IDEAL
=============================================================

Single source of truth for object matching, shared by:

    benchmarks/evaluation/metrics.py      — hard metric computation (PE, IoU, RE, …)
    benchmarks/reconstruction/compose.py  — reconstruction pipeline (geometry write-back)

Both pipelines use the same matching result, so the pairing used during
evaluation is identical to the one used when writing predicted poses back
into the scene.

Matching strategy
-----------------
Category-wise Hungarian matching:

1. Partition GT and predicted objects into buckets by ``category``
   (case-insensitive, stripped).
2. Within each category bucket, build a cost matrix whose entry (i, j) is
   the XY-plane Euclidean distance between GT object i and predicted object j.
3. Run the Hungarian algorithm (``scipy.optimize.linear_sum_assignment``) on
   each bucket to find the globally optimal one-to-one assignment.
4. Predicted objects with no GT counterpart in their category are
   hallucinations (unmatched_pred); GT objects with no predicted counterpart
   are missed detections (unmatched_gt).

Why XY only
-----------
The Z coordinate is tied to each asset's pivot-point convention and is not
comparable across different assets. The project spec explicitly restricts
position error to the XY plane.

Public API
----------
- ``MatchedPair``    — frozen dataclass holding (gt_idx, pred_idx)
- ``match_objects``  — main entry point; returns (pairs, unmatched_gt, unmatched_pred)
- ``extract_objects``— extracts the objects list from a payload dict (two formats)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchedPair:
    """A matched (GT, prediction) pair.

    Attributes:
        gt_idx:   Index into the GT objects list.
        pred_idx: Index into the predicted objects list.
    """

    gt_idx: int
    pred_idx: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_category(obj: Dict[str, Any]) -> str:
    return str(obj.get("category", "")).strip().lower()


def _safe_position_xy(obj: Dict[str, Any]) -> Tuple[float, float]:
    p = obj.get("position", [])
    if isinstance(p, list) and len(p) >= 2:
        try:
            return float(p[0]), float(p[1])
        except (TypeError, ValueError):
            pass
    return 0.0, 0.0


def _xy_distance(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    ax, ay = _safe_position_xy(a)
    bx, by = _safe_position_xy(b)
    return math.hypot(ax - bx, ay - by)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_objects(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the objects list from a payload dict.

    Supports two formats used in the project:

    - Standard prediction / GT JSON: ``{"scene": {"objects": [...]}}``
    - objects_in_view JSON:          ``{"objects": [...]}``
    """
    scene = payload.get("scene")
    if isinstance(scene, dict):
        objs = scene.get("objects")
        if isinstance(objs, list):
            return objs
    objs = payload.get("objects")
    return objs if isinstance(objs, list) else []


def match_objects(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
) -> Tuple[List[MatchedPair], Set[int], Set[int]]:
    """Match predicted objects to GT objects using category-wise Hungarian matching.

    Parameters
    ----------
    gt_objects:
        GT objects list (from ``objects_in_view.json`` or ``GT.json``
        ``scene.objects``).
    pred_objects:
        Predicted objects list (from the model prediction JSON
        ``scene.objects``).

    Returns
    -------
    matched_pairs:
        List of ``MatchedPair`` instances. Each GT index and each predicted
        index appears at most once.
    unmatched_gt:
        Set of GT indices not matched by any prediction (missed detections / FN).
    unmatched_pred:
        Set of predicted indices not matched to any GT (hallucinations).

    Algorithm
    ---------
    For each category shared by GT and predictions:

    1. Build an (n_gt_cat x n_pred_cat) cost matrix of XY distances.
    2. Solve with ``scipy.optimize.linear_sum_assignment`` (Hungarian algorithm)
       to minimise total assignment cost.
    3. Collect the resulting (gt_idx, pred_idx) pairs.

    Categories present in only one side produce entirely unmatched entries:
    GT-only categories contribute to unmatched_gt (FN); pred-only categories
    contribute to unmatched_pred (hallucinations).
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

    for cat in set(gt_by_cat) & set(pred_by_cat):
        g_indices = gt_by_cat[cat]
        p_indices = pred_by_cat[cat]

        cost = np.array(
            [
                [_xy_distance(gt_objects[gi], pred_objects[pj]) for pj in p_indices]
                for gi in g_indices
            ],
            dtype=np.float64,
        )

        row_ind, col_ind = linear_sum_assignment(cost)

        for r, c in zip(row_ind, col_ind):
            gi = g_indices[r]
            pj = p_indices[c]
            used_gt.add(gi)
            used_pred.add(pj)
            matched.append(MatchedPair(gt_idx=gi, pred_idx=pj))

    unmatched_gt = set(range(len(gt_objects))) - used_gt
    unmatched_pred = set(range(len(pred_objects))) - used_pred
    return matched, unmatched_gt, unmatched_pred