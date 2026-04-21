#!/usr/bin/env python3
"""
compose.py — Build a render instruction payload for one (scene, model) pair.

Pipeline (replaces the legacy "overlay pred onto GT" design)
------------------------------------------------------------

  GT.json  ─┐
            │   filter by
  objects_in_view.json ──► in-view GT list
            │
  <model>.json (pred, camera frame) ─► camera_transform ─► pred (world)
            │
            ▼
  Matching (category-wise Hungarian)  ──or──  grid-positional zip
            │
            ▼
  For each matched pair:
     read (location, dimensions) from scene.blend     [bpy]
     pose_inverse.invert_pose(pred, gt, original, cat)
     apply wall-mounted / floor-standing Z policy
            │
            ▼
  <model>_render.json   (instruction payload for render.py)

Output schema (``<model>_render.json``)
---------------------------------------
::

  {
    "scene_id":  "...",
    "model_id":  "...",
    "room_type": "bedroom" | "classroom" | ...,
    "is_grid_scene": bool,
    "camera_transform": {"delta_deg": int | null},

    "matched": [
      {"gt_id": "...", "category": "bed",
       "final_location":   [x, y, z],
       "final_rotation_z": float_deg,
       "final_dimensions": [x, y, z]}, ...
    ],

    "hide": [
      {"gt_id": "...", "category": "..."}, ...
    ],

    "hallucination": [
      {"pred_category": "sofa",
       "pred_position": [x, y, z],       # world frame
       "pred_dim_mean": float_metres},   # render.py caps the sphere radius
      ...
    ]
  }

``matched`` entries are the only ones that cause geometry overrides in
render.py. ``hide`` entries are the in-view GT objects that the model failed
to predict (rendered with ``hide_render=True``). ``hallucination`` entries
are pred objects with no GT counterpart (rendered as translucent red spheres
at the predicted XYZ position).

Z-coordinate policy
-------------------
For floor-standing categories, ``final_location[2]`` is forced to the
original ``obj.location.z`` read from scene.blend. This respects the
per-asset origin-z quirks of Infinigen (see engineering notes §Z 轴处理).
Wall-mounted categories (``mirror``, ``wall_art``) keep the value produced
by ``pose_inverse``.

bpy requirement
---------------
This module opens scene.blend to read original pose data. The process must
be able to ``import bpy``. ``reconstruct.py`` already enforces this via
``runtime.ensure_bpy_importable`` before spawning compose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# Repository root on sys.path so "benchmarks.xxx" imports work.
_REPO_ROOT = Path(__file__).resolve().parents[2]  # .../IDEAL/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.camera_transform import transform_pred_scene
from benchmarks.match_objects import MatchedPair, extract_objects, match_objects
from benchmarks.scene_config import (
    GRID_TARGET_CATEGORIES,
    is_grid_scene,
    is_wall_mounted,
)

from benchmarks.reconstruction.pose_inverse import invert_pose


# ---------------------------------------------------------------------------
# JSON loaders
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    with open(path.resolve(), encoding="utf-8") as f:
        return json.load(f)


def _get_room_type(gt_data: dict[str, Any]) -> str | None:
    try:
        return gt_data["scene"]["room"]["type"]
    except (KeyError, TypeError):
        return None


def _safe_category(obj: dict[str, Any]) -> str:
    return str(obj.get("category", "")).strip().lower()


def _filter_in_view(
    gt_objects: list[dict[str, Any]],
    inview_ids: set[str],
) -> list[dict[str, Any]]:
    """Return the subset of ``gt_objects`` whose id is in ``inview_ids``.

    Order is preserved from GT.json so downstream grid zip-alignment matches
    the convention documented in scene_config.
    """
    return [o for o in gt_objects if str(o.get("id", "")) in inview_ids]


# ---------------------------------------------------------------------------
# scene.blend reader (bpy)
# ---------------------------------------------------------------------------


def _read_original_poses(
    blend_path: Path,
    ids_of_interest: Iterable[str],
) -> dict[str, dict[str, list[float]]]:
    """Open ``blend_path`` with bpy and read (location, dimensions) for each id.

    Only the ids we actually need are pulled. Returns::

        {gt_id: {"location":   [x, y, z],
                 "dimensions": [x, y, z]}, ...}

    Objects not found in the blend are omitted (caller logs a warning).
    """
    import bpy  # type: ignore[import-not-found]

    bpy.ops.wm.open_mainfile(filepath=str(blend_path.resolve()))

    wanted = set(ids_of_interest)
    out: dict[str, dict[str, list[float]]] = {}
    for oid in wanted:
        obj = bpy.data.objects.get(oid)
        if obj is None:
            continue
        loc = obj.location
        dim = obj.dimensions
        out[oid] = {
            "location":   [float(loc.x),  float(loc.y),  float(loc.z)],
            "dimensions": [float(dim.x),  float(dim.y),  float(dim.z)],
        }
    return out


# ---------------------------------------------------------------------------
# Matching dispatchers
# ---------------------------------------------------------------------------


def _match_non_grid(
    gt_in_view: list[dict[str, Any]],
    pred_objs: list[dict[str, Any]],
) -> tuple[list[MatchedPair], set[int], set[int]]:
    """Hungarian matching (living space / meeting room / …)."""
    return match_objects(gt_in_view, pred_objs)


def _match_grid(
    gt_in_view: list[dict[str, Any]],
    pred_objs: list[dict[str, Any]],
    room_type: str,
) -> tuple[list[MatchedPair], set[int], set[int]]:
    """Positional zip matching on grid target categories only.

    Non-target in-view GT objects (e.g. blackboard, teacher's desk) are
    simply left unmatched — they will not be hidden, because render.py
    treats them as untouched.  We indicate this by NOT adding them to any
    of (matched, unmatched_gt). Callers should treat unmatched_gt as
    "in-view GT that the model failed to place" for target categories only.

    Pred objects in target categories with no GT slot → unmatched_pred.
    Pred objects in non-target categories → silently ignored (irrelevant
    to the grid soft metric).
    """
    targets = GRID_TARGET_CATEGORIES.get(room_type, frozenset())

    gt_targets_idx: list[int] = [
        i for i, o in enumerate(gt_in_view) if _safe_category(o) in targets
    ]
    pred_targets_idx: list[int] = [
        j for j, o in enumerate(pred_objs) if _safe_category(o) in targets
    ]

    pairs: list[MatchedPair] = []
    n = min(len(gt_targets_idx), len(pred_targets_idx))
    for k in range(n):
        pairs.append(MatchedPair(
            gt_idx=gt_targets_idx[k], pred_idx=pred_targets_idx[k]))

    unmatched_gt = set(gt_targets_idx[n:])
    unmatched_pred = set(pred_targets_idx[n:])
    return pairs, unmatched_gt, unmatched_pred


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _build_matched_entries(
    pairs: list[MatchedPair],
    gt_in_view: list[dict[str, Any]],
    pred_objs: list[dict[str, Any]],
    originals: dict[str, dict[str, list[float]]],
    *,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pair in pairs:
        gt_obj = gt_in_view[pair.gt_idx]
        pred_obj = pred_objs[pair.pred_idx]
        gt_id = str(gt_obj.get("id", ""))
        cat = _safe_category(gt_obj)

        orig = originals.get(gt_id)
        if orig is None:
            print(
                f"[compose] WARNING: {gt_id!r} not found in scene.blend — "
                f"skipping this match",
                file=sys.stderr,
            )
            continue

        pose = invert_pose(pred_obj, gt_obj, orig, cat)

        # Wall-mounted vs floor-standing Z policy.
        # Floor-standing: preserve scene.blend's obj.location.z verbatim to
        # respect Infinigen's per-asset origin-z quirks.
        if not is_wall_mounted(cat):
            pose["final_location"][2] = orig["location"][2]

        if verbose:
            print(
                f"[compose]   matched {gt_id} ({cat}): "
                f"loc={pose['final_location']} rot_z={pose['final_rotation_z']} "
                f"dim={pose['final_dimensions']}",
                file=sys.stderr,
            )

        out.append({
            "gt_id":              gt_id,
            "category":           cat,
            "final_location":     pose["final_location"],
            "final_rotation_z":   pose["final_rotation_z"],
            "final_dimensions":   pose["final_dimensions"],
        })
    return out


def _build_hide_entries(
    unmatched_gt: set[int],
    gt_in_view: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "gt_id":    str(gt_in_view[i].get("id", "")),
            "category": _safe_category(gt_in_view[i]),
        }
        for i in sorted(unmatched_gt)
    ]


def _build_hallucination_entries(
    unmatched_pred: set[int],
    pred_objs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for j in sorted(unmatched_pred):
        obj = pred_objs[j]
        pos = obj.get("position")
        dim = obj.get("dimensions")
        if not isinstance(pos, list) or len(pos) < 3:
            print(
                f"[compose] WARNING: hallucinated pred object missing "
                f"position; skipping: {obj!r}",
                file=sys.stderr,
            )
            continue
        if isinstance(dim, list) and len(dim) >= 3:
            dim_mean = (float(dim[0]) + float(dim[1]) + float(dim[2])) / 3.0
        else:
            dim_mean = 0.3  # fall-back radius cue
        out.append({
            "pred_category": _safe_category(obj),
            "pred_position": [float(pos[0]), float(pos[1]), float(pos[2])],
            "pred_dim_mean": round(float(dim_mean), 4),
        })
    return out


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def run_compose(
    info_path: Path,
    gt_path: Path,
    prediction_path: Path,
    blend_path: Path,
    render_out: Path,
    scene_id: str,
    model_id: str,
    scene_dir: Path | None = None,
    *,
    verbose: bool = False,
) -> None:
    # ── Inputs ──────────────────────────────────────────────────────────
    gt_data = _load_json(gt_path)
    pred = _load_json(prediction_path)
    info = _load_json(info_path)

    inview_list = info.get("objects")
    if not isinstance(inview_list, list):
        raise ValueError(f"{info_path}: missing or invalid 'objects' list")
    inview_ids = {str(o.get("id", "")) for o in inview_list if isinstance(o, dict)}

    room_type = _get_room_type(gt_data)

    # ── Camera-frame → world-frame on predictions ──────────────────────
    _scene_dir = scene_dir or info_path.resolve().parent
    pred, delta_deg = transform_pred_scene(pred, _scene_dir, verbose=verbose)
    if verbose:
        print(
            f"[compose] camera transform delta = {delta_deg}° "
            f"(None means metadata missing; pred used as-is)",
            file=sys.stderr,
        )

    # ── GT filter by in-view ids ───────────────────────────────────────
    gt_all = extract_objects(gt_data)
    gt_in_view = _filter_in_view(gt_all, inview_ids)
    if verbose:
        print(
            f"[compose] {len(gt_in_view)}/{len(gt_all)} GT objects in view",
            file=sys.stderr,
        )

    pred_objs = extract_objects(pred)

    # ── Matching ───────────────────────────────────────────────────────
    grid = is_grid_scene(room_type)
    if grid:
        print(
            f"[compose] grid scene ({room_type}) — positional zip on target categories",
            file=sys.stderr,
        )
        pairs, unmatched_gt, unmatched_pred = _match_grid(
            gt_in_view, pred_objs, room_type or ""
        )
    else:
        pairs, unmatched_gt, unmatched_pred = _match_non_grid(gt_in_view, pred_objs)

    if verbose:
        print(
            f"[compose] matched={len(pairs)} "
            f"unmatched_gt={len(unmatched_gt)} "
            f"unmatched_pred={len(unmatched_pred)}",
            file=sys.stderr,
        )

    # ── Read original pose from scene.blend (only for matched ids) ─────
    matched_ids = [
        str(gt_in_view[p.gt_idx].get("id", "")) for p in pairs
    ]
    try:
        originals = _read_original_poses(blend_path, matched_ids)
    except ImportError as exc:
        raise RuntimeError(
            "compose.py requires a Python that can import bpy. "
            "Run via reconstruct.py in the Infinigen conda env."
        ) from exc

    # ── Build render payload ───────────────────────────────────────────
    matched_entries = _build_matched_entries(
        pairs, gt_in_view, pred_objs, originals, verbose=verbose,
    )
    hide_entries = _build_hide_entries(unmatched_gt, gt_in_view)
    halluc_entries = _build_hallucination_entries(unmatched_pred, pred_objs)

    payload = {
        "scene_id":         scene_id,
        "model_id":         model_id,
        "room_type":        room_type,
        "is_grid_scene":    grid,
        "camera_transform": {"delta_deg": delta_deg},
        "matched":          matched_entries,
        "hide":             hide_entries,
        "hallucination":    halluc_entries,
    }

    render_out = render_out.resolve()
    render_out.parent.mkdir(parents=True, exist_ok=True)
    with open(render_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"[compose] written: {render_out}  "
        f"(matched={len(matched_entries)}, hide={len(hide_entries)}, "
        f"hallucination={len(halluc_entries)})",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IDEAL compose: build <model>_render.json from pred + GT + scene.blend"
    )
    p.add_argument("--prediction", type=Path, required=True,
                   help="VLM prediction JSON (camera-frame)")
    p.add_argument("--info-json", type=Path, required=True,
                   help="objects_in_view.json (in-view id list + pixel stats)")
    p.add_argument("--gt-json", type=Path, required=True,
                   help="GT.json for the scene")
    p.add_argument("--blend", type=Path, required=True,
                   help="scene.blend for the scene (read-only)")
    p.add_argument("--protocol", default="floor", choices=("floor", "camera"),
                   help="prediction coordinate frame (for logging only)")
    p.add_argument("--scene-id", required=True)
    p.add_argument("--model-id", required=True,
                   help="stem of the prediction file; output is {model_id}_render.json")
    p.add_argument("--scene-dir", type=Path, default=None,
                   help="scene directory with metadata.json "
                        "(defaults to parent of --info-json)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    pred = args.prediction.resolve()
    render_out = pred.parent / f"{args.model_id}_render.json"
    scene_dir = args.scene_dir or args.info_json.resolve().parent

    try:
        run_compose(
            info_path=args.info_json,
            gt_path=args.gt_json,
            prediction_path=pred,
            blend_path=args.blend,
            render_out=render_out,
            scene_id=args.scene_id,
            model_id=args.model_id,
            scene_dir=scene_dir,
            verbose=args.verbose,
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"[compose] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())