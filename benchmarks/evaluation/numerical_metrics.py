"""
IDEAL hard metrics D1–D4 (grid / D5 lives in ``metrics_grid``).

Grid rooms (classroom / office / library) call ``evaluate_scene(..., object_metrics=False)``
from ``evaluate.py``: D1 + D2 only here; D5 comes from ``metrics_grid``. Non-grid rooms use
``object_metrics=True`` (full D1–D4) and do not run ``evaluate_grid``.

Notation (consistent with benchmark paper):
  N   = full-room GT object count (GT.json)
  K   = in-view GT object count (objects_in_view.json)
  P   = total predicted object count (pred.json)
  K_m = matched pairs (D3/D4 denominator)
  H   = hallucinated pred objects (P - K_m)
  Q   = pred objects involved in pairwise collision (OOR numerator)
  B   = pred objects outside room boundary (OOB numerator)

Matching uses the canonical Hungarian algorithm from match_objects.py (shared with compose.py).
Default position error is XY-only; IoU uses world-axis AABBs from local W,D and yaw.
Rotation error uses yaw-symmetry tiers from scene_config.py.
Matched floor-standing (non-wall-mounted) predictions use GT ``position[2]`` for all
geometry metrics, consistent with reconstruction (paper §5.1 vertical convention).

CLI::

    python benchmarks/evaluation/numerical_metrics.py --gt <gt.json> --pred <pred.json>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


# benchmarks/ root (one level up from benchmarks/evaluation/)
_BENCHMARKS_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_ROOT))

from common.match_objects import MatchedPair, match_objects as _match_objects
from common.scene_config import (
    YAW_IGNORE_CATEGORIES,
    YAW_180_SYMMETRIC_CATEGORIES,
    YAW_90_SYMMETRIC_CATEGORIES,
    COLLISION_IOU_THRESHOLD,
    PE_FAIL_THRESHOLD_M,
    IOU_PASS_THRESHOLD,
    RE_FAIL_THRESHOLD_DEG,
    is_wall_mounted,
)


# ------------------------------
# Data structures and helpers
# ------------------------------


@dataclass(frozen=True)
class AABB:
    """Axis-aligned 3D bounding box."""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float


# MatchedPair is imported from match_objects (canonical shared module).


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_objects(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept ``{"scene": {"objects": [...]}}`` or a flat ``{"objects": [...]}`` (e.g. objects_in_view)."""
    scene = payload.get("scene")
    if isinstance(scene, dict) and isinstance(scene.get("objects"), list):
        return scene["objects"]
    objs = payload.get("objects")
    return objs if isinstance(objs, list) else []


def _extract_room_size(payload: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    """Read ``scene.room.size`` as ``[W, D, H]`` metres, or ``None`` if missing."""
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
    # Clamp to avoid inverted AABBs from bad inputs
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
    """Euclidean distance; default ignores Z when ``use_xy_only`` is True."""
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
    """World-axis XY half-extents of local (width, depth) after yaw; no yaw ⇒ local axes."""
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
    Object dict → 3D AABB. ``z_anchor``: ``"center"`` = ``position.z`` at volume centre;
    ``"bottom"`` = bottom-centre (legacy).
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
    """Smaller interior angle between two yaw values in [0, 180]°."""
    d = abs((a_deg - b_deg) % 360.0)
    return min(d, 360.0 - d)


# ------------------------------
# Matching (N / K / M)
# ------------------------------


# Object matching is handled by the canonical match_objects module (Hungarian algorithm).
# _match_objects(gt_objects, pred_objects) -> (matched_pairs, unmatched_gt, unmatched_pred)


# ------------------------------
# D3: Position Accuracy (PA)
# ------------------------------


def metric_position_accuracy(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    use_xy_only: bool = True,
    fail_threshold_m: float = PE_FAIL_THRESHOLD_M,
) -> Dict[str, Any]:
    """XY position accuracy on matched pairs.

    Continuous: mean_pe_distance (metres), denominator = K_m.
    Discrete:   pa_rate = PA@0.3m = fraction of pairs with distance <= fail_threshold_m.
    """
    errors: List[float] = []
    for pair in matched_pairs:
        gp = _safe_position(gt_objects[pair.gt_idx])
        pp = _safe_position(pred_objects[pair.pred_idx])
        errors.append(euclidean_distance(gp, pp, use_xy_only=use_xy_only))

    if not errors:
        return {
            "mean_pe_distance": None,
            "pa_rate": None,
            "pa_fail_count": 0,
            "fail_threshold_m": fail_threshold_m,
            "evaluated_pairs": 0,
            "use_xy_only": use_xy_only,
        }

    n = len(errors)
    mean_err = sum(errors) / n
    fail_count = sum(1 for e in errors if e > fail_threshold_m)

    return {
        "mean_pe_distance": mean_err,
        "pa_rate": 1.0 - fail_count / n,
        "pa_fail_count": fail_count,
        "fail_threshold_m": fail_threshold_m,
        "evaluated_pairs": n,
        "use_xy_only": use_xy_only,
    }


# ------------------------------
# D3: Geometric Accuracy (GA)
# ------------------------------


def metric_geometric_accuracy(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    iou_threshold: float = IOU_PASS_THRESHOLD,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """3D BBox IoU on matched pairs.

    Continuous: mean_iou, denominator = K_m.
    Discrete:   ba_rate = BA@0.15 = fraction of pairs with IoU >= iou_threshold.
    """
    pairs = list(matched_pairs)
    ious: List[float] = []

    for pair in pairs:
        gt_box = object_to_aabb(gt_objects[pair.gt_idx], z_anchor=z_anchor)
        pr_box = object_to_aabb(pred_objects[pair.pred_idx], z_anchor=z_anchor)
        ious.append(bbox_iou_3d(gt_box, pr_box))

    k = len(pairs)
    fail_count = sum(1 for v in ious if v < iou_threshold)
    mean_iou = (sum(ious) / k) if k > 0 else None

    return {
        "mean_iou": mean_iou,
        "ba_rate": (1.0 - fail_count / k) if k > 0 else None,
        "ba_fail_count": fail_count,
        "iou_threshold": iou_threshold,
        "evaluated_pairs": k,
    }


# ------------------------------
# D3: Rotation Accuracy (RA)
# ------------------------------


def _symmetry_corrected_yaw_error(gt_yaw: float, pr_yaw: float, category: str) -> float:
    """Compute symmetry-aware absolute yaw error in degrees.

    Symmetry tiers (from scene_config):
      - YAW_90_SYMMETRIC:  objects look identical every 90°; error folded to [0°, 45°].
      - YAW_180_SYMMETRIC: objects look identical every 180°; error folded to [0°, 90°].
      - All others:        standard shortest-arc error in [0°, 180°].
    """
    if category in YAW_90_SYMMETRIC_CATEGORIES:
        d = abs(gt_yaw % 90.0 - pr_yaw % 90.0)
        return min(d, 90.0 - d)
    if category in YAW_180_SYMMETRIC_CATEGORIES:
        d = angular_difference_deg(gt_yaw, pr_yaw)  # in [0°, 180°]
        return min(d, 180.0 - d)                    # fold to [0°, 90°]
    return angular_difference_deg(gt_yaw, pr_yaw)


def metric_rotation_accuracy(
    gt_objects: List[Dict[str, Any]],
    pred_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
    *,
    fail_threshold_deg: float = RE_FAIL_THRESHOLD_DEG,
) -> Dict[str, Any]:
    """Symmetry-aware yaw accuracy on matched pairs, excluding YAW_IGNORE categories.

    Uses the three-tier symmetry config from scene_config:
      - YAW_IGNORE_CATEGORIES: skipped (orientation irrelevant).
      - YAW_90_SYMMETRIC_CATEGORIES: error folded to [0°, 45°].
      - YAW_180_SYMMETRIC_CATEGORIES: error folded to [0°, 90°].
      - All others: standard shortest-arc error in [0°, 180°].

    Continuous output: mean_abs_yaw_error_deg (degrees).
    Discrete output:   ra_rate = RA@30° = fraction of pairs with error <= fail_threshold_deg.
    """
    errors: List[float] = []
    skipped_ignore = 0
    skipped_no_yaw = 0

    for pair in matched_pairs:
        gt_obj = gt_objects[pair.gt_idx]
        pr_obj = pred_objects[pair.pred_idx]
        cat = _safe_category(gt_obj)

        if cat in YAW_IGNORE_CATEGORIES:
            skipped_ignore += 1
            continue

        gt_yaw = _safe_yaw(gt_obj)
        pr_yaw = _safe_yaw(pr_obj)
        if gt_yaw is None or pr_yaw is None:
            skipped_no_yaw += 1
            continue

        errors.append(_symmetry_corrected_yaw_error(gt_yaw, pr_yaw, cat))

    n = len(errors)
    if n == 0:
        return {
            "mean_abs_yaw_error_deg": None,
            "ra_rate": None,
            "ra_fail_count": 0,
            "fail_threshold_deg": fail_threshold_deg,
            "evaluated_pairs": 0,
            "skipped_ignore": skipped_ignore,
            "skipped_no_yaw": skipped_no_yaw,
        }

    mean_abs = sum(errors) / n
    fail_count = sum(1 for e in errors if e > fail_threshold_deg)
    return {
        "mean_abs_yaw_error_deg": mean_abs,
        "ra_rate": 1.0 - fail_count / n,
        "ra_fail_count": fail_count,
        "fail_threshold_deg": fail_threshold_deg,
        "evaluated_pairs": n,
        "skipped_ignore": skipped_ignore,
        "skipped_no_yaw": skipped_no_yaw,
    }


# ------------------------------
# D2: Non-Overlap Rate (NOR)
# ------------------------------


def metric_non_overlap_rate(
    pred_objects: List[Dict[str, Any]],
    *,
    denominator_n: int,
    overlap_iou_threshold: float = 0.03,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """NOR = 1 - Q / P: fraction of pred objects not involved in any pairwise collision."""
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
        "non_overlap_rate": (1.0 - q / denominator_n) if denominator_n > 0 else None,
        "overlap_iou_threshold": overlap_iou_threshold,
    }


# ------------------------------
# D2: In-Bounds Rate (IBR)
# ------------------------------


def _is_box_out_of_room(box: AABB, room_size: Tuple[float, float, float]) -> bool:
    """True if any part of the box lies outside centred XY footprint ``[-W/2,W/2]×[-D/2,D/2]`` and ``z∈[0,H]``."""
    w, d, h = room_size
    x_min, x_max = -w / 2.0, w / 2.0
    y_min, y_max = -d / 2.0, d / 2.0
    z_min, z_max = 0.0, h

    # Intersecting the wall volume or sitting outside counts as violation
    return (
        box.min_x < x_min
        or box.max_x > x_max
        or box.min_y < y_min
        or box.max_y > y_max
        or box.min_z < z_min
        or box.max_z > z_max
    )


def metric_in_bounds_rate(
    pred_objects: List[Dict[str, Any]],
    *,
    room_size: Tuple[float, float, float],
    denominator_n: int,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """IBR = 1 - B / P: fraction of pred objects whose AABB lies within the room boundary."""
    violating_indices: List[int] = []
    for idx, obj in enumerate(pred_objects):
        box = object_to_aabb(obj, z_anchor=z_anchor)
        if _is_box_out_of_room(box, room_size):
            violating_indices.append(idx)

    b = len(violating_indices)
    return {
        "B_out_of_bounds_objects": b,
        "in_bounds_rate": (1.0 - b / denominator_n) if denominator_n > 0 else None,
        "violating_indices": violating_indices,
    }


# ------------------------------
# D4: Object Recognition
# ------------------------------


def metric_recognition_rate(*, k_matched: int, k_inview: int) -> Optional[float]:
    """D4 RR: Recognition Rate RR = K_m / K (matched over in-view GT count)."""
    if k_inview <= 0:
        return None
    return k_matched / k_inview


def metric_non_hallucination_rate(*, h_hallucinated: int, p_pred: int) -> Optional[float]:
    """D4 NHR: Non-Hallucination Rate NHR = 1 - H / P (fraction of predictions that are not hallucinations)."""
    if p_pred <= 0:
        return None
    return 1.0 - h_hallucinated / p_pred


# ------------------------------
# D1: Scene Validity — Room Type Accuracy
# ------------------------------


def metric_room_type_accuracy(
    gt_payload: Dict[str, Any], pred_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """D1 RTA: single-sample room ``type`` match (0/1)."""

    def _room_type(payload: Dict[str, Any]) -> Optional[str]:
        scene = payload.get("scene")
        if not isinstance(scene, dict):
            return None
        room = scene.get("room")
        if not isinstance(room, dict):
            return None
        t = room.get("type")
        if t is None:
            return None
        return str(t).strip().lower()

    gt_type = _room_type(gt_payload)
    pred_type = _room_type(pred_payload)
    if gt_type is None or pred_type is None:
        return {
            "correct_count": None,
            "total_count": 1,
            "room_type_accuracy": None,
            "gt_room_type": gt_type,
            "pred_room_type": pred_type,
            "warning": "missing room.type in gt or prediction",
        }

    correct = 1 if gt_type == pred_type else 0
    return {
        "correct_count": correct,
        "total_count": 1,
        "room_type_accuracy": float(correct),
        "gt_room_type": gt_type,
        "pred_room_type": pred_type,
    }


# ------------------------------
# D1: Scene Validity — Output Failure Rate
# ------------------------------


def _validate_prediction_schema_minimal(pred_payload: Dict[str, Any]) -> List[str]:
    """Lightweight shape checks (not a full JSON Schema). Only runs on already-parsed dicts."""
    errors: List[str] = []

    scene = pred_payload.get("scene")
    if not isinstance(scene, dict):
        return ["missing or invalid top-level scene"]

    room = scene.get("room")
    if not isinstance(room, dict):
        errors.append("missing or invalid scene.room")
    else:
        if not isinstance(room.get("type"), str):
            errors.append("scene.room.type must be string")
        size = room.get("size")
        if not (
            isinstance(size, list)
            and len(size) == 3
            and all(isinstance(v, (int, float)) for v in size)
        ):
            errors.append("scene.room.size must be [float,float,float]")
        if not isinstance(room.get("asset_count"), int):
            errors.append("scene.room.asset_count must be int")

    objs = scene.get("objects")
    if not isinstance(objs, list):
        errors.append("scene.objects must be a list")
        return errors

    for idx, o in enumerate(objs):
        if not isinstance(o, dict):
            errors.append(f"scene.objects[{idx}] must be object")
            continue
        if not isinstance(o.get("id"), str):
            errors.append(f"scene.objects[{idx}].id must be string")
        if not isinstance(o.get("category"), str):
            errors.append(f"scene.objects[{idx}].category must be string")

        pos = o.get("position")
        if not (
            isinstance(pos, list)
            and len(pos) == 3
            and all(isinstance(v, (int, float)) for v in pos)
        ):
            errors.append(f"scene.objects[{idx}].position must be [float,float,float]")

        dims = o.get("dimensions")
        if not (
            isinstance(dims, list)
            and len(dims) == 3
            and all(isinstance(v, (int, float)) for v in dims)
        ):
            errors.append(
                f"scene.objects[{idx}].dimensions must be [float,float,float]"
            )

        yaw = o.get("rotation_yaw")
        if yaw is not None and not isinstance(yaw, (int, float)):
            errors.append(f"scene.objects[{idx}].rotation_yaw must be number or null")

    return errors


def is_prediction_unparsed(pred_payload: Dict[str, Any]) -> bool:
    """True when prediction must not run D2–D5 (same cases as PSR ≠ 1)."""
    if pred_payload.get("_parse_status") == "failed":
        return True
    if "_error" in pred_payload:
        return True
    return bool(_validate_prediction_schema_minimal(pred_payload))


def metric_parse_success_rate(pred_payload: Dict[str, Any]) -> Dict[str, Any]:
    """D1 PSR: 1 if output parsed and passed schema validation, 0 otherwise.

    Detection priority:
      1. ``_parse_status == "failed"``  — infer.py parse failure flag.
      2. ``"_error" in pred_payload``   — deepseek_janus_pro_api.py parse failure flag.
      3. Fallback: minimal schema validation (no pipeline-level flag present).
    """
    parse_status = pred_payload.get("_parse_status")
    if parse_status == "failed":
        return {
            "failed_count": 1,
            "total_count": 1,
            "parse_success_rate": 0.0,
            "failure_source": "parse_failed",
            "schema_errors": [],
        }

    if "_error" in pred_payload:
        return {
            "failed_count": 1,
            "total_count": 1,
            "parse_success_rate": 0.0,
            "failure_source": "parse_error",
            "schema_errors": [],
        }

    errors = _validate_prediction_schema_minimal(pred_payload)
    failed = 1 if errors else 0
    return {
        "failed_count": failed,
        "total_count": 1,
        "parse_success_rate": float(1 - failed),
        "failure_source": "schema_validation" if failed else None,
        "schema_errors": errors,
    }


# ------------------------------
# Full-scene driver
# ------------------------------


def _pred_objects_copy_for_metrics(pred_objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shallow-copy each object with a private ``position`` list (avoid mutating ``pred_payload``)."""
    out: List[Dict[str, Any]] = []
    for o in pred_objects:
        oc = dict(o)
        p = o.get("position")
        if isinstance(p, list) and len(p) == 3:
            oc["position"] = [float(p[0]), float(p[1]), float(p[2])]
        else:
            oc["position"] = [0.0, 0.0, 0.0]
        out.append(oc)
    return out


def _snap_floor_standing_z_to_gt(
    pred_objects: List[Dict[str, Any]],
    gt_objects: List[Dict[str, Any]],
    matched_pairs: Iterable[MatchedPair],
) -> None:
    """In-place: for matched pairs whose GT category is floor-standing, set pred ``z`` from GT."""
    for pair in matched_pairs:
        gt_obj = gt_objects[pair.gt_idx]
        if is_wall_mounted(_safe_category(gt_obj)):
            continue
        gz = _safe_position(gt_obj)[2]
        pred_objects[pair.pred_idx]["position"][2] = gz


def evaluate_scene(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
    *,
    inview_objects: Optional[List[Dict[str, Any]]] = None,
    object_metrics: bool = True,
    use_xy_only_for_position: bool = True,
    iou_threshold: float = IOU_PASS_THRESHOLD,
    collision_iou_threshold: float = COLLISION_IOU_THRESHOLD,
    z_anchor: str = "center",
) -> Dict[str, Any]:
    """Run all per-scene metrics and return the nested result dict.

    Parameters
    ----------
    inview_objects:
        GT objects filtered to the camera's field of view (from objects_in_view.json).
        D3/D4 matching uses this subset as ground truth.  When None, falls back to
        all GT objects (gt_payload), which may unfairly penalise models for objects
        they were never shown.

    object_metrics:
        If False (grid rooms: classroom / office / library), skip Hungarian matching,
        D3, and D4; still run D1 and D2 (NOR / IBR) on raw predictions.

    Vertical convention (§5.1): matched floor-standing objects use GT ``z`` for metric
    computation; wall-mounted categories keep predicted ``z``. Unmatched predictions
    keep raw ``z``.

    ``z_anchor`` defaults to ``\"center\"`` (``position.z`` at box centre; paper §3),
    matching ``object_to_aabb`` and per-metric helpers. Use ``\"bottom\"`` only for
    legacy comparisons.
    """
    gt_objects_all = _extract_objects(gt_payload)
    pred_objects   = _extract_objects(pred_payload)

    # D3/D4 reference: in-view GT objects only; fall back to full GT if unavailable.
    gt_objects_inview = inview_objects if inview_objects is not None else gt_objects_all

    if object_metrics:
        matched_pairs, unmatched_gt, unmatched_pred = _match_objects(
            gt_objects_inview,
            pred_objects,
        )
        pred_objects_metrics = _pred_objects_copy_for_metrics(pred_objects)
        _snap_floor_standing_z_to_gt(pred_objects_metrics, gt_objects_inview, matched_pairs)
    else:
        matched_pairs = []
        unmatched_gt = set(range(len(gt_objects_inview)))
        unmatched_pred = set(range(len(pred_objects)))
        pred_objects_metrics = pred_objects

    n      = len(gt_objects_all)     # N: full-room GT count
    k      = len(gt_objects_inview)  # K: in-view GT count (D3/D4 reference)
    k_m    = len(matched_pairs)      # K_m: matched pairs
    h      = len(unmatched_pred)     # H: hallucinated pred objects
    fn     = len(unmatched_gt)       # FN: missed in-view GT objects
    n_pred = len(pred_objects)       # P: total predicted objects (OOR/OOB denominator)

    # Prefer GT room size for a consistent room frame; else prediction
    room_size = _extract_room_size(gt_payload) or _extract_room_size(pred_payload)

    if object_metrics:
        position = metric_position_accuracy(
            gt_objects_inview,
            pred_objects_metrics,
            matched_pairs,
            use_xy_only=use_xy_only_for_position,
        )
        iou = metric_geometric_accuracy(
            gt_objects_inview,
            pred_objects_metrics,
            matched_pairs,
            iou_threshold=iou_threshold,
            z_anchor=z_anchor,
        )
        rotation = metric_rotation_accuracy(
            gt_objects_inview, pred_objects_metrics, matched_pairs
        )
        rr = metric_recognition_rate(k_matched=k_m, k_inview=k)
        nhr = metric_non_hallucination_rate(h_hallucinated=h, p_pred=n_pred)
    else:
        empty_pairs: List[MatchedPair] = []
        position = metric_position_accuracy(
            gt_objects_inview,
            pred_objects_metrics,
            empty_pairs,
            use_xy_only=use_xy_only_for_position,
        )
        iou = metric_geometric_accuracy(
            gt_objects_inview,
            pred_objects_metrics,
            empty_pairs,
            iou_threshold=iou_threshold,
            z_anchor=z_anchor,
        )
        rotation = metric_rotation_accuracy(
            gt_objects_inview, pred_objects_metrics, empty_pairs
        )
        rr = None
        nhr = None

    # NOR / IBR: denominator = P (total pred count) so rate is always in [0, 1]
    non_overlap = metric_non_overlap_rate(
        pred_objects_metrics,
        denominator_n=n_pred,
        overlap_iou_threshold=collision_iou_threshold,
        z_anchor=z_anchor,
    )

    in_bounds = (
        metric_in_bounds_rate(
            pred_objects_metrics,
            room_size=room_size,
            denominator_n=n_pred,
            z_anchor=z_anchor,
        )
        if room_size is not None
        else {
            "B_out_of_bounds_objects": None,
            "in_bounds_rate": None,
            "violating_indices": [],
            "warning": "room_size is missing in both GT and prediction payload",
        }
    )
    room_type = metric_room_type_accuracy(gt_payload, pred_payload)
    json_succ = metric_parse_success_rate(pred_payload)

    return {
        "global_counts": {
            "N_gt_objects":           n,
            "K_inview_objects":       k,
            "P_pred_objects":         n_pred,
            "Km_matched_objects":     k_m,
            "H_hallucinated_objects": h,
            "FN_missed_inview_objects": fn,
        },
        "geometry_spatial_accuracy": {
            "position_accuracy":  position,
            "geometric_accuracy": iou,
            "rotation_accuracy":  rotation,
        },
        "gt_independent": {
            "non_overlap": non_overlap,
            "in_bounds":   in_bounds,
        },
        "object_recognition": {
            "recognition_rate_Km_over_K": rr,
            "non_hallucination_rate":     nhr,
        },
        "scene_recognition": {
            "room_type_accuracy": room_type,
        },
        "structured_output": {
            "parse_success": json_succ,
        },
        "matching": {
            "matched_pairs": [
                {"gt_idx": pair.gt_idx, "pred_idx": pair.pred_idx}
                for pair in matched_pairs
            ],
            "unmatched_gt_indices":   sorted(unmatched_gt),
            "unmatched_pred_indices": sorted(unmatched_pred),
        },
        "settings": {
            "object_metrics":              object_metrics,
            "use_xy_only_for_position":    use_xy_only_for_position,
            "iou_threshold":               iou_threshold,
            "collision_iou_threshold":     collision_iou_threshold,
            "z_anchor":                    z_anchor,
            "inview_objects_provided":     inview_objects is not None,
            "floor_standing_z_from_gt":    object_metrics,
        },
    }


# ------------------------------
# CLI
# ------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="IDEAL hard metrics D1–D4 (single pair of GT / prediction JSON files)."
    )
    parser.add_argument("--gt", required=True, help="Path to ground-truth JSON")
    parser.add_argument("--pred", required=True, help="Path to model prediction JSON")
    parser.add_argument(
        "--use-xyz",
        action="store_true",
        help="Use XYZ for position error (default: XY only)",
    )
    parser.add_argument(
        "--iou-threshold",
        type=float,
        default=IOU_PASS_THRESHOLD,
        help=f"BBox-IoU pass threshold (default: {IOU_PASS_THRESHOLD})",
    )
    parser.add_argument(
        "--collision-iou-threshold",
        type=float,
        default=COLLISION_IOU_THRESHOLD,
        help=f"Pairwise pred–pred IoU threshold for collision count (default: {COLLISION_IOU_THRESHOLD})",
    )
    parser.add_argument(
        "--z-anchor",
        choices=["center", "bottom"],
        default="center",
        help='How to interpret object position.z when building AABBs (default: "center", paper §3)',
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
