#!/usr/bin/env python3
"""
compose.py — Assemble the render payload for a single scene.

After ``match_objects`` pairs predicted objects with their in-view GT slots,
this script writes the predicted geometry back into the working layer, merges
the result with the full ``GT.json``, and emits ``{model_id}_render.json``
which ``render.py`` consumes directly.

Z-coordinate convention
-----------------------
All objects (including wall-mounted ones such as mirrors and wall art) keep
the GT z value. Predicted z is never written back. This avoids floating /
floor-penetration artefacts caused by imprecise model z estimates, and is
consistent with the evaluation protocol where z is excluded from position
error.

Name rationale
--------------
"compose" mirrors the graphics / build-pipeline usage: assembling a single
renderable artefact from multiple sources (matched prediction layer + full GT).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from benchmarks.match_objects import MatchedPair, extract_objects, match_objects


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _cache_from_info_path(info_path: Path) -> dict[str, Any]:
    """Load the in-view sidecar JSON and return a working-layer dict.

    Accepts both ``objects_in_view.json`` (top-level ``objects`` key) and the
    legacy ``info.json`` format.
    """
    with open(info_path, encoding="utf-8") as f:
        info: dict[str, Any] = json.load(f)
    objs = info.get("objects")
    if not isinstance(objs, list):
        raise ValueError(f"{info_path}: expected a non-empty list under 'objects'")
    return {
        "scene_id": info.get("scene_id"),
        "scene": {"objects": copy.deepcopy(objs)},
    }


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


# ---------------------------------------------------------------------------
# Geometry write-back
# ---------------------------------------------------------------------------


def _apply_overlay(cache_obj: dict[str, Any], pred_obj: dict[str, Any]) -> None:
    """Write predicted x, y, dimensions, and yaw into *cache_obj* in-place.

    The z component is always taken from *cache_obj* (GT value). Predicted z
    is ignored for all object types.
    """
    pp = pred_obj.get("position")
    cp = cache_obj.get("position")
    if not isinstance(pp, list) or len(pp) < 2:
        raise ValueError(
            f"Predicted object missing valid position (need at least x, y): "
            f"id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )

    gt_z = cp[2] if isinstance(cp, list) and len(cp) >= 3 else 0.0
    cache_obj["position"] = [float(pp[0]), float(pp[1]), float(gt_z)]

    dim = pred_obj.get("dimensions")
    if not isinstance(dim, list) or len(dim) < 3:
        raise ValueError(
            f"Predicted object missing valid dimensions (need 3 values): "
            f"id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    cache_obj["dimensions"] = [float(dim[0]), float(dim[1]), float(dim[2])]

    if "rotation_yaw" in pred_obj:
        cache_obj["rotation_yaw"] = pred_obj["rotation_yaw"]


# ---------------------------------------------------------------------------
# Matching + write-back orchestration
# ---------------------------------------------------------------------------


def _apply_matches(
    info_layer: dict[str, Any],
    pred: dict[str, Any],
) -> None:
    """Run Hungarian matching and write predicted geometry back into *info_layer*.

    Hallucinated predictions (no GT slot) are appended as placeholder entries.
    Unmatched GT slots have their geometry nullified (hidden during render).
    Modifies *info_layer* in-place.
    """
    cache_objs: list[dict[str, Any]] = info_layer["scene"]["objects"]
    pred_objs: list[dict[str, Any]] = extract_objects(pred)

    matched, _unmatched_gt, unmatched_pred = match_objects(cache_objs, pred_objs)

    matched_cache_ids: set[int] = set()
    for pair in matched:
        cache_obj = cache_objs[pair.gt_idx]
        pred_obj = pred_objs[pair.pred_idx]
        _apply_overlay(cache_obj, pred_obj)
        matched_cache_ids.add(pair.gt_idx)

    # Nullify unmatched GT slots so render.py hides them
    for i, obj in enumerate(cache_objs):
        if i not in matched_cache_ids:
            _nullify_geometry(obj)

    # Append placeholder entries for hallucinated predictions
    placeholder_seq = 0
    for pj in sorted(unmatched_pred):
        placeholder_seq += 1
        ph = _make_placeholder(pred_objs[pj], placeholder_seq)
        cache_objs.append(ph)
        print(
            f"[compose] No GT slot for predicted object — appending placeholder: "
            f"{ph['id']} (category={pred_objs[pj].get('category')!r})",
            file=sys.stderr,
        )


def _merge_with_gt(
    gt: dict[str, Any], info_layer: dict[str, Any]
) -> dict[str, Any]:
    """Merge the matched info_layer back into the full GT document.

    GT is the base; any object id present in info_layer overrides the GT
    entry of the same id. New ids (placeholders) are appended.
    """
    out = copy.deepcopy(gt)
    scene = out.setdefault("scene", {})
    room = scene.setdefault("room", {})
    gt_objs: list[dict[str, Any]] = scene.setdefault("objects", [])

    id_to_idx: dict[str, int] = {
        str(o["id"]): i
        for i, o in enumerate(gt_objs)
        if isinstance(o, dict) and o.get("id") is not None
    }

    for co in (info_layer.get("scene") or {}).get("objects", []):
        if not isinstance(co, dict):
            continue
        oid = co.get("id")
        if oid is None:
            continue
        oid = str(oid)
        if oid in id_to_idx:
            gt_objs[id_to_idx[oid]] = copy.deepcopy(co)
        else:
            gt_objs.append(copy.deepcopy(co))
            id_to_idx[oid] = len(gt_objs) - 1

    room["asset_count"] = len(gt_objs)
    return out


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------


def run_compose(
    info_path: Path,
    gt_path: Path,
    prediction_path: Path,
    render_out: Path,
) -> None:
    info_layer = _cache_from_info_path(info_path.resolve())

    with open(prediction_path.resolve(), encoding="utf-8") as f:
        pred: dict[str, Any] = json.load(f)
    with open(gt_path.resolve(), encoding="utf-8") as f:
        gt_data: dict[str, Any] = json.load(f)

    _apply_matches(info_layer, pred)
    pseudo = _merge_with_gt(gt_data, info_layer)

    render_out = render_out.resolve()
    render_out.parent.mkdir(parents=True, exist_ok=True)
    with open(render_out, "w", encoding="utf-8") as f:
        json.dump(pseudo, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IDEAL: match predictions -> write geometry -> merge GT -> {model_id}_render.json"
    )
    p.add_argument("--blend", type=Path, default=None, help="scene.blend path (reserved for debugging, unused)")
    p.add_argument("--prediction", type=Path, required=True, help="VLM prediction JSON")
    p.add_argument("--info-json", type=Path, required=True, help="objects_in_view.json (or info.json)")
    p.add_argument("--gt-json", type=Path, required=True, help="GT.json for the scene")
    p.add_argument("--protocol", required=True, choices=("floor", "camera"))
    p.add_argument("--scene-id", required=True)
    p.add_argument("--model-id", required=True, help="stem of the prediction file; output is {model_id}_render.json")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    pred = args.prediction.resolve()
    render_out = pred.parent / f"{args.model_id}_render.json"
    try:
        run_compose(args.info_json, args.gt_json, pred, render_out)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[compose] {exc}", file=sys.stderr)
        return 1
    print(f"[compose] written: {render_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())