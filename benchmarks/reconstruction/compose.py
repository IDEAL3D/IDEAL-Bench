#!/usr/bin/env python3
"""
compose.py — Assemble the render payload for a single scene.

Data flow (standard scenes — living space / meeting room)
---------------------------------------------------------
  objects_in_view.json        GT.json
  (id, category,          (id, position,
   pixel stats)           dimensions, yaw, …)
        |                       |
        +------- join on id ----+
                  |
          gt_objects_with_pos
                  |
     match_objects()  [category-wise Hungarian on XY distance]
                  |
    MatchedPair list  +  unmatched sets
                  |
   _apply_overlay: write pred x,y,dims,yaw (keep GT z)
                  |
   _merge_with_gt: overlay onto full GT document
                  |
        {model_id}_render.json

Data flow (grid scenes — classroom / library / office)
------------------------------------------------------
Same join step, but matching is replaced by positional write-back:

  For each grid target category (e.g. "desk" in classroom):
    - Take in-view GT objects of that category (GT order).
    - Take pred objects of that category (pred JSON order).
    - zip() them one-to-one: write pred x,y,dims,yaw → GT slot (keep GT z).
    - Pred surplus → placeholder spheres.
    - GT surplus  → nullified (hidden in render).

  Non-target categories in grid scenes → standard Hungarian matching.

Z-coordinate convention
-----------------------
All objects keep the GT z value. Predicted z is never written back.

Scene routing
-------------
Determined by ``scene_config.is_grid_scene(room_type)``.
Room type is read from GT.json ``scene.room.type``.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path
from typing import Any

# Ensure the repository root (parent of 'benchmarks/') is on sys.path so that
# 'import benchmarks.*' works regardless of how this script is invoked
# (direct subprocess call, python -m, etc.).
_REPO_ROOT = Path(__file__).resolve().parents[2]  # …/IDEAL/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.match_objects import extract_objects, match_objects
from benchmarks.scene_config import GRID_TARGET_CATEGORIES, is_grid_scene


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_objects_in_view(info_path: Path) -> list[dict[str, Any]]:
    with open(info_path, encoding="utf-8") as f:
        info: dict[str, Any] = json.load(f)
    objs = info.get("objects")
    if not isinstance(objs, list):
        raise ValueError(f"{info_path}: missing or invalid 'objects' list")
    return objs


def _build_gt_index(gt_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gt_objs = extract_objects(gt_data)
    return {
        str(o["id"]): o
        for o in gt_objs
        if isinstance(o, dict) and o.get("id") is not None
    }


def _get_room_type(gt_data: dict[str, Any]) -> str | None:
    try:
        return gt_data["scene"]["room"]["type"]
    except (KeyError, TypeError):
        return None


def _join_inview_with_gt(
    inview_objs: list[dict[str, Any]],
    gt_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich in-view objects with GT geometry (position, dimensions, yaw).

    Returns entries in the same order as *inview_objs*. Objects whose id is
    not found in GT are skipped with a warning.
    """
    result: list[dict[str, Any]] = []
    for iv in inview_objs:
        oid = str(iv.get("id", ""))
        gt_entry = gt_index.get(oid)
        if gt_entry is None:
            print(
                f"[compose] WARNING: in-view object {oid!r} not found in GT — skipping",
                file=sys.stderr,
            )
            continue
        merged = copy.deepcopy(gt_entry)
        merged["actual_pixels"] = iv.get("actual_pixels")
        merged["truncation_ratio"] = iv.get("truncation_ratio")
        merged["near_edge"] = iv.get("near_edge")
        result.append(merged)
    return result




# Camera coordinate transform is handled by the shared module.
# benchmarks/camera_transform.py is the single source of truth for both
# reconstruction (compose.py) and evaluation (metrics.py).
from benchmarks.camera_transform import transform_pred_scene as _ct_transform_pred_scene

# ---------------------------------------------------------------------------
# Geometry write-back primitives
# ---------------------------------------------------------------------------


def _safe_category(obj: dict[str, Any]) -> str:
    return str(obj.get("category", "")).strip().lower()


def _nullify_geometry(obj: dict[str, Any]) -> None:
    obj["position"] = None
    obj["dimensions"] = None
    obj["rotation_yaw"] = None


def _placeholder_id(seq: int) -> str:
    return f"placeholder_{seq:02d}" if seq < 100 else f"placeholder_{seq}"


def _make_placeholder(pred_obj: dict[str, Any], seq: int) -> dict[str, Any]:
    out = copy.deepcopy(pred_obj)
    out["id"] = _placeholder_id(seq)
    out["category"] = "placeholder"
    return out


def _apply_overlay(gt_obj: dict[str, Any], pred_obj: dict[str, Any]) -> None:
    """Write pred x, y, dimensions, yaw into *gt_obj*. GT z is always kept."""
    pp = pred_obj.get("position")
    cp = gt_obj.get("position")
    if not isinstance(pp, list) or len(pp) < 2:
        raise ValueError(
            f"Predicted object missing valid position (need at least x, y): "
            f"id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    gt_z = cp[2] if isinstance(cp, list) and len(cp) >= 3 else 0.0
    gt_obj["position"] = [float(pp[0]), float(pp[1]), float(gt_z)]

    dim = pred_obj.get("dimensions")
    if not isinstance(dim, list) or len(dim) < 3:
        raise ValueError(
            f"Predicted object missing valid dimensions (need 3 values): "
            f"id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    gt_obj["dimensions"] = [float(dim[0]), float(dim[1]), float(dim[2])]

    if "rotation_yaw" in pred_obj:
        gt_obj["rotation_yaw"] = pred_obj["rotation_yaw"]


def _apply_overlay_grid(gt_obj: dict[str, Any], pred_obj: dict[str, Any]) -> None:
    """Write pred x, y, and yaw into *gt_obj*. GT z and dimensions are kept.

    Used for grid target objects (desk, chair, library_block, ...) where asset
    dimensions are uniform and should not be overridden by model predictions.
    Only XY position and orientation matter for the grid soft metric.
    """
    pp = pred_obj.get("position")
    cp = gt_obj.get("position")
    if not isinstance(pp, list) or len(pp) < 2:
        raise ValueError(
            f"Predicted object missing valid position (need at least x, y): "
            f"id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    gt_z = cp[2] if isinstance(cp, list) and len(cp) >= 3 else 0.0
    gt_obj["position"] = [float(pp[0]), float(pp[1]), float(gt_z)]
    # dimensions intentionally not written -- GT asset size is preserved

    if "rotation_yaw" in pred_obj:
        gt_obj["rotation_yaw"] = pred_obj["rotation_yaw"]


# ---------------------------------------------------------------------------
# Standard matching (living space / meeting room)
# ---------------------------------------------------------------------------


def _apply_matches(
    inview_with_gt: list[dict[str, Any]],
    pred: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hungarian matching for standard scenes.

    Returns (updated inview list, placeholder list).
    """
    pred_objs = extract_objects(pred)
    matched, _unmatched_gt, unmatched_pred = match_objects(inview_with_gt, pred_objs)

    matched_indices: set[int] = set()
    for pair in matched:
        _apply_overlay(inview_with_gt[pair.gt_idx], pred_objs[pair.pred_idx])
        matched_indices.add(pair.gt_idx)

    for i, obj in enumerate(inview_with_gt):
        if i not in matched_indices:
            _nullify_geometry(obj)

    placeholders: list[dict[str, Any]] = []
    for seq, pj in enumerate(sorted(unmatched_pred), start=1):
        ph = _make_placeholder(pred_objs[pj], seq)
        placeholders.append(ph)
        print(
            f"[compose] Hallucinated prediction — appending placeholder: "
            f"{ph['id']} (category={pred_objs[pj].get('category')!r})",
            file=sys.stderr,
        )

    return inview_with_gt, placeholders


# ---------------------------------------------------------------------------
# Grid matching (classroom / library / office)
# ---------------------------------------------------------------------------


def _apply_grid_matches(
    inview_with_gt: list[dict[str, Any]],
    pred: dict[str, Any],
    room_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Positional write-back for grid scenes.

    For each grid target category (e.g. desk/chair in classroom):
      - GT in-view objects (GT order) are zipped with pred objects (pred JSON
        order) one-to-one: pred x,y,dims,yaw → GT slot (GT z kept).
      - Pred surplus → placeholder spheres.
      - GT surplus   → nullified (hidden in render).

    Non-target categories (e.g. blackboard, teacher's desk) are left
    completely untouched — their GT geometry is preserved as-is. This keeps
    the rendered scene stable for judge VLM scoring without introducing
    distortion from potentially inaccurate model predictions for these objects.
    Non-target pred objects are silently ignored (they are irrelevant to the
    grid soft metric).

    Returns (updated inview list, placeholder list).
    """
    pred_objs = extract_objects(pred)
    target_cats = GRID_TARGET_CATEGORIES.get(room_type, frozenset())

    # Separate grid targets from non-targets in the in-view GT list
    target_inview: list[tuple[int, dict[str, Any]]] = []  # (original index, obj)

    for i, obj in enumerate(inview_with_gt):
        if _safe_category(obj) in target_cats:
            target_inview.append((i, obj))
    # Non-target in-view objects are intentionally not collected:
    # they stay untouched in inview_with_gt (GT geometry preserved).

    # Collect target pred objects in JSON order
    target_pred: list[dict[str, Any]] = [
        obj for obj in pred_objs if _safe_category(obj) in target_cats
    ]

    placeholders: list[dict[str, Any]] = []
    placeholder_seq = 0
    handled_inview_indices: set[int] = set()

    # Positional zip: pred JSON order → GT order, one-to-one
    for (gt_i, gt_obj), pred_obj in zip(target_inview, target_pred):
        _apply_overlay_grid(gt_obj, pred_obj)
        handled_inview_indices.add(gt_i)

    # Pred surplus → placeholder spheres
    for pred_obj in target_pred[len(target_inview):]:
        placeholder_seq += 1
        ph = _make_placeholder(pred_obj, placeholder_seq)
        placeholders.append(ph)
        print(
            f"[compose] Grid surplus prediction — appending placeholder: "
            f"{ph['id']} (category={pred_obj.get('category')!r})",
            file=sys.stderr,
        )

    # GT surplus → nullify (hide in render)
    for gt_i, gt_obj in target_inview[len(target_pred):]:
        _nullify_geometry(gt_obj)
        handled_inview_indices.add(gt_i)

    return inview_with_gt, placeholders


# ---------------------------------------------------------------------------
# GT merge
# ---------------------------------------------------------------------------


def _merge_with_gt(
    gt_data: dict[str, Any],
    updated_inview: list[dict[str, Any]],
    placeholders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Overlay updated in-view objects and placeholders onto the full GT document."""
    out = copy.deepcopy(gt_data)
    scene = out.setdefault("scene", {})
    room = scene.setdefault("room", {})
    gt_objs: list[dict[str, Any]] = scene.setdefault("objects", [])

    id_to_idx: dict[str, int] = {
        str(o["id"]): i
        for i, o in enumerate(gt_objs)
        if isinstance(o, dict) and o.get("id") is not None
    }

    for obj in updated_inview:
        oid = str(obj.get("id", ""))
        if oid in id_to_idx:
            gt_objs[id_to_idx[oid]] = copy.deepcopy(obj)

    for ph in placeholders:
        gt_objs.append(copy.deepcopy(ph))

    room["asset_count"] = len(gt_objs)
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_compose(
    info_path: Path,
    gt_path: Path,
    prediction_path: Path,
    render_out: Path,
    scene_dir: Path | None = None,
    *,
    verbose: bool = False,
) -> None:
    inview_objs = _load_objects_in_view(info_path.resolve())

    with open(gt_path.resolve(), encoding="utf-8") as f:
        gt_data: dict[str, Any] = json.load(f)
    with open(prediction_path.resolve(), encoding="utf-8") as f:
        pred: dict[str, Any] = json.load(f)

    # Transform predicted coordinates from camera-aligned frame to world frame.
    # metadata.json is expected alongside objects_in_view.json in the scene dir.
    _scene_dir = scene_dir or info_path.resolve().parent
    pred, delta_deg = _ct_transform_pred_scene(pred, _scene_dir, verbose=verbose)
    if verbose and delta_deg is not None:
        print(f"[compose] applied camera transform: delta={delta_deg}°", file=sys.stderr)

    room_type = _get_room_type(gt_data)
    gt_index = _build_gt_index(gt_data)
    inview_with_gt = _join_inview_with_gt(inview_objs, gt_index)

    if is_grid_scene(room_type):
        print(f"[compose] Grid scene detected ({room_type}) — using positional write-back", file=sys.stderr)
        updated_inview, placeholders = _apply_grid_matches(inview_with_gt, pred, room_type)
    else:
        updated_inview, placeholders = _apply_matches(inview_with_gt, pred)

    pseudo = _merge_with_gt(gt_data, updated_inview, placeholders)

    render_out = render_out.resolve()
    render_out.parent.mkdir(parents=True, exist_ok=True)
    with open(render_out, "w", encoding="utf-8") as f:
        json.dump(pseudo, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Verification helper (--verify)
# ---------------------------------------------------------------------------


def _verify_camera_transform(scene_dir: Path, gt_path: Path) -> None:
    """Print camera yaw, snap delta, and GT object positions for manual inspection.

    Usage: python compose.py --verify --scene-dir <scene_dir> --gt-json <GT.json>
    """
    from benchmarks.camera_transform import (
        load_camera_meta, camera_yaw_deg, snap_delta_deg, needs_dim_swap
    )

    cam_meta = load_camera_meta(scene_dir)
    if cam_meta is None:
        print("[verify] ERROR: metadata.json not found or missing camera.rotation_euler", file=sys.stderr)
        return

    yaw_deg = camera_yaw_deg(cam_meta)
    delta = snap_delta_deg(yaw_deg)
    swap = needs_dim_swap(delta)

    print(f"[verify] scene_dir          : {scene_dir}")
    print(f"[verify] rotation_euler     : {cam_meta['rotation_euler']}")
    print(f"[verify] camera yaw         : {yaw_deg:.2f}°  (continuous)")
    print(f"[verify] snap delta         : {delta}°  (applied to pred)")
    print(f"[verify] swap W↔D          : {swap}")

    if gt_path.is_file():
        with open(gt_path, encoding="utf-8") as f:
            gt_data = json.load(f)
        gt_objs = extract_objects(gt_data)
        print(f"\n[verify] GT objects ({len(gt_objs)} total):")
        print(f"  {'id':30s}  {'category':20s}  {'position (world)':30s}  yaw")
        for o in gt_objs:
            oid = str(o.get("id", "?"))
            cat = str(o.get("category", "?"))
            pos = o.get("position", [])
            yaw = o.get("rotation_yaw", "?")
            pos_str = f"[{pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}]" if isinstance(pos, list) and len(pos) >= 3 else str(pos)
            print(f"  {oid:30s}  {cat:20s}  {pos_str:30s}  {yaw}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IDEAL: match predictions -> write geometry -> merge GT -> {model_id}_render.json"
    )
    p.add_argument("--blend", type=Path, default=None,
                   help="scene.blend path (reserved for debugging, unused)")
    p.add_argument("--prediction", type=Path, default=None,
                   help="VLM prediction JSON")
    p.add_argument("--info-json", type=Path, default=None,
                   help="objects_in_view.json (or info.json)")
    p.add_argument("--gt-json", type=Path, default=None,
                   help="GT.json for the scene")
    p.add_argument("--protocol", default="floor", choices=("floor", "camera"))
    p.add_argument("--scene-id", default=None)
    p.add_argument("--model-id", default=None,
                   help="stem of the prediction file; output is {model_id}_render.json")
    p.add_argument("--scene-dir", type=Path, default=None,
                   help="scene directory containing metadata.json (auto-inferred from --info-json if omitted)")
    p.add_argument("--verbose", action="store_true",
                   help="print per-object coordinate transform details")
    p.add_argument("--verify", action="store_true",
                   help="print camera forward angle and GT layout for manual inspection; "
                        "requires --scene-dir (or --info-json) and --gt-json")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    if args.verify:
        scene_dir = args.scene_dir or (args.info_json.resolve().parent if args.info_json else None)
        gt_path = args.gt_json or Path("/dev/null")
        if scene_dir is None:
            print("[compose] --verify requires --scene-dir or --info-json", file=sys.stderr)
            return 1
        _verify_camera_transform(scene_dir, gt_path)
        return 0

    # Normal pipeline — all three required args must be present
    missing = [n for n, v in [("--prediction", args.prediction),
                               ("--info-json", args.info_json),
                               ("--gt-json", args.gt_json),
                               ("--model-id", args.model_id),
                               ("--scene-id", args.scene_id)] if v is None]
    if missing:
        print(f"[compose] Missing required arguments: {', '.join(missing)}", file=sys.stderr)
        return 1

    pred = args.prediction.resolve()
    render_out = pred.parent / f"{args.model_id}_render.json"
    scene_dir = args.scene_dir or args.info_json.resolve().parent
    try:
        run_compose(args.info_json, args.gt_json, pred, render_out,
                    scene_dir=scene_dir, verbose=args.verbose)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[compose] {exc}", file=sys.stderr)
        return 1
    print(f"[compose] written: {render_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())