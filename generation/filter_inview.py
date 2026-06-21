#!/usr/bin/env python3
"""
Usage (run from ``IDEAL/generation``; ``cwd`` matters for ``bpy`` subprocess module resolution)::

    python filter_inview.py --help
    python filter_inview.py
    python filter_inview.py --scenes-root /path/to/batch_root [--workers N] [--skip] [--gpu ID]

Stage-2 in-view preprocessing: apply three visibility filters and write ``objects_in_view.json``.

Operates on scenes that have already passed ``filter_scenes.py`` (stage 1).
Does NOT move or reject any scene directory — only writes/updates ``objects_in_view.json``
and appends a per-scene diagnostic record to ``inview_batch.jsonl``.

**CLI:** ``--scenes-root`` defaults to ``IDEAL/data/scenes_raw``. Scene discovery matches ``filter_scenes.py``.
Verify the chosen root actually contains exported, discoverable scene folders before invoking this tool; when the batch directory is empty, run ``generate.py`` (same directory as this script) first to synthesize smoke-test scenes.

Filters (applied in order, cheapest first):
    1. Visibility       — instance-map pixel count >= MIN_VISIBLE_PIXELS  (pure Python)
    2. Aspect Ratio     — max(Δx/Δy, Δy/Δx) <= ASPECT_RATIO_MAX          (pure Python)
    3. Truncation       — centroid within 20% of image border AND actual/projected < TRUNCATION_RATIO_MIN  (bpy subprocess)

``NO_FILTER_ROOM_TYPES`` in this module lists room-type prefixes that **skip all three filters**:
for those ids, every GT-aligned asset with **any** instance-map pixels is written to
``objects_in_view.json`` (via ``build_inview_no_filter``). **Currently only** ``classroom`` is registered;
office, library, meetingroom still run F1–F3.

objects_in_view.json format per object: {id, category, actual_pixels, truncation_ratio, near_edge}
3D attributes (position, dimensions, rotation_yaw) are omitted — look them up from GT.json by id.

Only scenes with at least one filter triggered (or errors) are written to inview_batch.jsonl.
Clean scenes are counted in batch_summary but not individually logged.
Batches are separated by a blank line. A batch_summary record closes each run.
Timestamps are in America/New_York time.

Default behaviour: always (re-)write objects_in_view.json, overwriting if it already exists.
Pass ``--skip`` to skip scenes where objects_in_view.json already exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_GENERATION_DIR = Path(__file__).resolve().parent
if str(_GENERATION_DIR) not in sys.path:
    sys.path.insert(0, str(_GENERATION_DIR))

_REPO_ROOT = _GENERATION_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.gpu_select import resolve_cuda_visible_devices
from preprocess_lib.build_inview import (
    MissingInViewInfoError,
    build_inview_no_filter,
    build_inview_with_filters,
)
from preprocess_lib.defaults import (
    BPY_PYTHON,
    INVIEW_BATCH_LOG_JSONL,
    OBJECTS_IN_VIEW_JSON,
    describe_no_valid_scenes_error,
    resolve_default_scenes_root,
)
from preprocess_lib.scene_discovery import discover_scene_blend_paths
from generation_common.asset_rules import scene_id_from_output_dir

_TZ_NY = ZoneInfo("America/New_York")

# Canonical room prefixes that skip F1–F3 (see module docstring). Currently: classroom only.
NO_FILTER_ROOM_TYPES = frozenset({"classroom"})


def _now_ny() -> str:
    return datetime.now(_TZ_NY).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# bpy python finder
# ---------------------------------------------------------------------------

def _find_bpy_python() -> str:
    if BPY_PYTHON and os.path.isfile(BPY_PYTHON):
        return BPY_PYTHON
    candidate = os.path.normpath(
        os.path.join(
            str(_GENERATION_DIR), "..", "..", "miniconda", "envs", "infinigen", "bin", "python"
        )
    )
    if os.path.isfile(candidate):
        return candidate
    return sys.executable


# ---------------------------------------------------------------------------
# Filter 3 subprocess
# ---------------------------------------------------------------------------

def _run_truncation_subprocess(
    bpy_python: str,
    blend_path: str,
    scene_id: str,
    candidates: List[Dict[str, Any]],
    render_size: List[int],
) -> Dict[str, Any]:
    """Run truncation_filter in a bpy subprocess. Returns the parsed result dict."""
    candidates_payload = {
        "scene_id": scene_id,
        "render_size": render_size,
        "objects": candidates,
    }

    fd_in, tmp_in = tempfile.mkstemp(suffix=".json", prefix="trunc_cand_")
    fd_out, tmp_out = tempfile.mkstemp(suffix=".json", prefix="trunc_res_")
    os.close(fd_in)
    os.close(fd_out)

    try:
        with open(tmp_in, "w", encoding="utf-8") as f:
            json.dump(candidates_payload, f)

        cmd = [
            bpy_python,
            "-m",
            "preprocess_lib.truncation_filter",
            "--blend", blend_path,
            "--candidates-json", tmp_in,
            "--out-json", tmp_out,
        ]
        r = subprocess.run(cmd, check=False, cwd=str(_GENERATION_DIR))
        if r.returncode != 0:
            return {"computed": False, "error": f"subprocess_exit_{r.returncode}",
                    "results": [], "behind_camera_tris": []}

        with open(tmp_out, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"computed": False, "error": str(e), "results": [], "behind_camera_tris": []}
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# objects_in_view.json writer
# ---------------------------------------------------------------------------

def _write_objects_in_view(
    blend_dir: str,
    scene_id: str,
    kept_ids_ordered: List[str],
    gt_id_to_stub: Dict[str, Dict[str, Any]],
    f3_results: Dict[str, Dict[str, Any]],
) -> None:
    """Write objects_in_view.json with minimal object stubs + F3 diagnostics.

    Each object entry: {id, category, actual_pixels, truncation_ratio, near_edge}
    3D fields are intentionally omitted — look them up from GT.json by id.
    """
    objects_out = []
    for gid in kept_ids_ordered:
        stub = gt_id_to_stub.get(gid)
        if stub is None:
            continue
        f3 = f3_results.get(gid, {})
        obj = {
            "id": stub["id"],
            "category": stub["category"],
            "actual_pixels": f3.get("actual_pixels"),
            "truncation_ratio": f3.get("truncation_ratio"),
            "near_edge": f3.get("near_edge"),
        }
        objects_out.append(obj)

    categories = sorted({o["category"] for o in objects_out if o.get("category")})
    payload = {
        "scene_id": scene_id,
        "categories_in_view_count": len(categories),
        "categories_in_view": categories,
        "objects_in_view_count": len(objects_out),
        "objects": objects_out,
    }
    out_path = os.path.join(blend_dir, OBJECTS_IN_VIEW_JSON)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[inview] wrote {out_path} (objects={len(objects_out)})")


# ---------------------------------------------------------------------------
# Per-scene processing
# ---------------------------------------------------------------------------

def _process_one_scene(
    job_idx: int,
    total: int,
    blend_path: str,
    scenes_root: str,
    bpy_python: str,
    skip: bool,
    print_lock: threading.Lock,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Process one scene. Returns (job_idx, log_record_or_None).

    log_record is None for clean scenes (no filters triggered, no errors).
    Clean scenes are counted in batch_summary but not written to jsonl.
    """

    def _log(msg: str) -> None:
        with print_lock:
            print(msg)

    blend_dir = os.path.dirname(os.path.abspath(blend_path))
    scene_id = scene_id_from_output_dir(blend_dir)
    _log(f"\n{'─'*60}\n[{job_idx}/{total}] {scene_id}\n{'─'*60}")

    out_path = os.path.join(blend_dir, OBJECTS_IN_VIEW_JSON)
    if skip and os.path.isfile(out_path):
        _log(f"[inview] skip (exists): {out_path}")
        return job_idx, None

    # --- Check if this room type bypasses all filters ---------------------
    room_type = scene_id.split("_")[0]
    if room_type in NO_FILTER_ROOM_TYPES:
        _log(f"[inview] room_type={room_type!r} → skip all filters, use instance map directly")
        try:
            no_filter = build_inview_no_filter(scene_id, blend_dir)
        except MissingInViewInfoError as e:
            _log(f"[ERROR] missing inputs: {e}")
            return job_idx, {
                "scene_id": scene_id,
                "status": "error",
                "error_message": f"MissingInViewInfoError: {e}",
            }
        except Exception as e:
            _log(f"[ERROR] unexpected: {type(e).__name__}: {e}")
            return job_idx, {
                "scene_id": scene_id,
                "status": "error",
                "error_message": f"{type(e).__name__}: {e}",
            }
        _write_objects_in_view(
            blend_dir, scene_id,
            no_filter["kept_ids_ordered"],
            no_filter["gt_id_to_stub"],
            no_filter["f3_results"],
        )
        return job_idx, None  # no filters → always clean, not logged

    # --- Filter 1 + 2 (pure Python) ---
    try:
        f12 = build_inview_with_filters(scene_id, blend_dir)
    except MissingInViewInfoError as e:
        _log(f"[ERROR] missing inputs: {e}")
        return job_idx, {
            "scene_id": scene_id,
            "status": "error",
            "error_message": f"MissingInViewInfoError: {e}",
        }
    except Exception as e:
        _log(f"[ERROR] unexpected: {type(e).__name__}: {e}")
        return job_idx, {
            "scene_id": scene_id,
            "status": "error",
            "error_message": f"{type(e).__name__}: {e}",
        }

    objects_inview_original: int = f12["objects_inview_original"]
    filter_1: List[Dict] = f12["filter_1"]
    filter_2: List[Dict] = f12["filter_2"]
    kept_after_f2: List[str] = f12["kept_ids_ordered"]
    pixel_counts: Dict[str, int] = f12["pixel_counts"]
    centroids: Dict[str, List[float]] = f12["centroids"]
    render_size: List[int] = f12["render_size"]
    gt_id_to_stub: Dict[str, Any] = f12["gt_id_to_stub"]
    gt_id_to_mattress_gt_id: Dict[str, str] = f12.get("gt_id_to_mattress_gt_id", {})

    # --- Filter 3 (bpy subprocess) ---
    filter_3_filtered: List[Dict[str, Any]] = []
    behind_camera_tris: List[Dict[str, Any]] = []
    kept_final: List[str] = kept_after_f2
    f3_results: Dict[str, Dict[str, Any]] = {}

    if kept_after_f2:
        candidates = [
            {
                "gt_id": gid,
                "actual_pixels": pixel_counts[gid],
                "centroid": centroids.get(gid),
                "mattress_gt_id": gt_id_to_mattress_gt_id.get(gid),  # None for non-bed objects
            }
            for gid in kept_after_f2
        ]
        trunc_result = _run_truncation_subprocess(
            bpy_python, blend_path, scene_id, candidates, render_size
        )
        behind_camera_tris = trunc_result.get("behind_camera_tris") or []

        if not trunc_result.get("computed"):
            err = trunc_result.get("error", "unknown")
            _log(f"[WARN] truncation subprocess failed: {err} — F3 skipped, keeping all F2 survivors")
            for gid in kept_after_f2:
                f3_results[gid] = {
                    "actual_pixels": pixel_counts[gid],
                    "truncation_ratio": None,
                    "near_edge": None,
                }
        else:
            kept_final = []
            for res in trunc_result.get("results") or []:
                gid = res["gt_id"]
                f3_results[gid] = {
                    "actual_pixels": res.get("actual_pixels"),
                    "truncation_ratio": res.get("truncation_ratio"),
                    "near_edge": res.get("near_edge"),
                }
                if res.get("passed", True):
                    kept_final.append(gid)
                else:
                    filter_3_filtered.append({
                        "gt_id": gid,
                        "actual_pixels": res.get("actual_pixels"),
                        "truncation_ratio": res.get("truncation_ratio"),
                        "near_edge": res.get("near_edge"),
                    })
    else:
        for gid in kept_after_f2:
            f3_results[gid] = {
                "actual_pixels": pixel_counts.get(gid),
                "truncation_ratio": None,
                "near_edge": None,
            }

    # --- Write objects_in_view.json ---
    _write_objects_in_view(blend_dir, scene_id, kept_final, gt_id_to_stub, f3_results)

    status = "empty_after_filter" if not kept_final else "ok"

    # --- Only log scenes where something interesting happened ---
    any_filtered = bool(filter_1 or filter_2 or filter_3_filtered or behind_camera_tris)
    if not any_filtered and status == "ok":
        return job_idx, None

    record: Dict[str, Any] = {
        "scene_id": scene_id,
        "status": status,
        "objects_inview_original": objects_inview_original,
        "objects_inview_final": len(kept_final),
    }
    if filter_1:
        record["filter_1_visibility"] = {"count": len(filter_1), "filtered": filter_1}
    if filter_2:
        record["filter_2_aspect_ratio"] = {"count": len(filter_2), "filtered": filter_2}
    if filter_3_filtered:
        record["filter_3_truncation"] = {"count": len(filter_3_filtered), "filtered": filter_3_filtered}
    if behind_camera_tris:
        record["behind_camera_tris"] = behind_camera_tris

    return job_idx, record


# ---------------------------------------------------------------------------
# Batch log writer
# ---------------------------------------------------------------------------

def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_batch_separator(path: str) -> None:
    """Append a blank line to visually separate batches in the jsonl file."""
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    _default_scenes_root = resolve_default_scenes_root(_REPO_ROOT)
    parser = argparse.ArgumentParser(
        description="Stage-2 in-view filter: write objects_in_view.json with 3-filter pipeline."
    )
    parser.add_argument(
        "--scenes-root",
        default=_default_scenes_root,
        help=(
            "Parent directory for scene folders "
            "(<root>/<scene_id>/scene.blend). "
            "Default: IDEAL/data/scenes_raw (resolved at runtime)."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Parallel scene jobs (default: 1). Each spawns a bpy subprocess for Filter 3.",
    )
    parser.add_argument(
        "--skip", action="store_true",
        help="Skip scenes where objects_in_view.json already exists. Default: overwrite.",
    )
    parser.add_argument(
        "--gpu", type=str, default=None,
        help="CUDA_VISIBLE_DEVICES for bpy subprocess (e.g. 0 or auto).",
    )
    args = parser.parse_args()

    if args.workers < 1:
        print("[ERROR] --workers must be >= 1", file=sys.stderr)
        return 2

    scenes_root = os.path.abspath(os.path.expanduser(args.scenes_root))
    _cuda = resolve_cuda_visible_devices(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = _cuda
    bpy_python = _find_bpy_python()

    blends = discover_scene_blend_paths(scenes_root)
    if not blends:
        print(f"[ERROR] {describe_no_valid_scenes_error(scenes_root)}", file=sys.stderr)
        return 2

    log_path = os.path.join(scenes_root, INVIEW_BATCH_LOG_JSONL)
    print(f"[inview] scenes_root={scenes_root}")
    print(f"[inview] CUDA_VISIBLE_DEVICES={_cuda}")
    print(f"[inview] bpy python: {bpy_python}")
    print(f"[inview] {len(blends)} scene(s), workers={args.workers}, skip={args.skip}")
    print(f"[inview] no_filter_room_types={sorted(NO_FILTER_ROOM_TYPES)}")
    print(f"[inview] batch log -> {log_path}")

    print_lock = threading.Lock()
    indexed_records: List[Tuple[int, Optional[Dict[str, Any]]]] = []

    if args.workers == 1:
        for i, bp in enumerate(blends, 1):
            result = _process_one_scene(
                i, len(blends), bp, scenes_root, bpy_python,
                args.skip, print_lock,
            )
            indexed_records.append(result)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    _process_one_scene,
                    i, len(blends), bp, scenes_root, bpy_python,
                    args.skip, print_lock,
                ): i
                for i, bp in enumerate(blends, 1)
            }
            for fut in as_completed(futs):
                indexed_records.append(fut.result())

    indexed_records.sort(key=lambda t: t[0])

    # --- Tally stats and write jsonl ---
    stats = {
        "scenes_total": len(blends),
        "scenes_ok": 0,
        "scenes_empty_after_filter": 0,
        "scenes_with_errors": 0,
        "scenes_with_behind_camera_tris": 0,
        "filter_1_visibility": {"scenes_affected": 0, "objects_filtered": 0},
        "filter_2_aspect_ratio": {"scenes_affected": 0, "objects_filtered": 0},
        "filter_3_truncation": {"scenes_affected": 0, "objects_filtered": 0},
    }

    for _, record in indexed_records:
        if record is None:
            stats["scenes_ok"] += 1
            continue

        status = record.get("status", "ok")
        if status == "error":
            stats["scenes_with_errors"] += 1
        elif status == "empty_after_filter":
            stats["scenes_empty_after_filter"] += 1
        else:
            stats["scenes_ok"] += 1

        if record.get("behind_camera_tris"):
            stats["scenes_with_behind_camera_tris"] += 1

        for key, stat_key in (
            ("filter_1_visibility", "filter_1_visibility"),
            ("filter_2_aspect_ratio", "filter_2_aspect_ratio"),
        ):
            if key in record:
                stats[stat_key]["scenes_affected"] += 1
                stats[stat_key]["objects_filtered"] += record[key].get("count", 0)

        f3 = record.get("filter_3_truncation", {})
        filtered_count = len(f3.get("filtered") or [])
        if filtered_count > 0:
            stats["filter_3_truncation"]["scenes_affected"] += 1
            stats["filter_3_truncation"]["objects_filtered"] += filtered_count

        _append_jsonl(log_path, record)

    # --- Batch summary + separator ---
    summary = {
        "type": "batch_summary",
        "timestamp": _now_ny(),
        **stats,
    }
    _append_jsonl(log_path, summary)
    _append_batch_separator(log_path)

    print(f"\n[inview] DONE. batch log -> {log_path}")
    print(
        f"  batch_scene_num={stats['scenes_total']} "
        f"processed_scene_num={stats['scenes_ok']} "
        f"empty_scene_num={stats['scenes_empty_after_filter']} "
        f"error_scene_num={stats['scenes_with_errors']}"
    )
    print(
        f"  f1_affected_scene_num={stats['filter_1_visibility']['scenes_affected']} "
        f"f1_filtered_object_num={stats['filter_1_visibility']['objects_filtered']}"
    )
    print(
        f"  f2_affected_scene_num={stats['filter_2_aspect_ratio']['scenes_affected']} "
        f"f2_filtered_object_num={stats['filter_2_aspect_ratio']['objects_filtered']}"
    )
    print(
        f"  f3_affected_scene_num={stats['filter_3_truncation']['scenes_affected']} "
        f"f3_filtered_object_num={stats['filter_3_truncation']['objects_filtered']}"
    )
    print(
        f"  behind_camera_tris_scene_num={stats['scenes_with_behind_camera_tris']}"
    )
    return 1 if stats["scenes_with_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())