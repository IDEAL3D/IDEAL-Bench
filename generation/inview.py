#!/usr/bin/env python3
"""
Stage-2 in-view preprocessing: apply three visibility filters and write ``objects_in_view.json``.

Operates on scenes that have already passed ``filter_scenes.py`` (stage 1).
Does NOT move or reject any scene directory — only writes/updates ``objects_in_view.json``
and appends a per-scene diagnostic record to ``inview_batch.jsonl``.

Filters (applied in order, cheapest first):
    1. Visibility       — instance-map pixel count >= MIN_VISIBLE_PIXELS  (pure Python)
    2. Aspect Ratio     — max(Δx/Δy, Δy/Δx) <= ASPECT_RATIO_MAX          (pure Python)
    3. Truncation       — actual_pixels / projected_pixels >= TRUNCATION_RATIO_MIN  (bpy subprocess)

Only scenes with at least one filtered object are written to the batch log.
Scenes with errors are always logged.
A final batch-summary record is appended at the end.

Usage:
    python inview.py --scenes-root <path> [--workers N] [--skip-if-exists] [--gpu ID]
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_GENERATION_DIR = Path(__file__).resolve().parent
if str(_GENERATION_DIR) not in sys.path:
    sys.path.insert(0, str(_GENERATION_DIR))

_REPO_ROOT = _GENERATION_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.gpu_select import resolve_cuda_visible_devices
from preprocess_lib.build_inview import MissingInViewInfoError, build_inview_with_filters
from preprocess_lib.defaults import (
    BPY_PYTHON,
    INVIEW_BATCH_LOG_JSONL,
    OBJECTS_IN_VIEW_JSON,
)
from generation_common.asset_rules import scene_id_from_output_dir


# ---------------------------------------------------------------------------
# Scene discovery (same logic as filter_scenes.py)
# ---------------------------------------------------------------------------

def _discover_blend_paths(scenes_root: str) -> List[str]:
    skip = frozenset({"manifests", "invalid", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"})
    root = os.path.abspath(os.path.expanduser(scenes_root))
    out: List[str] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name in skip or name.startswith("."):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        bp = os.path.join(d, "scene.blend")
        if os.path.isfile(bp):
            out.append(bp)
    return out


# ---------------------------------------------------------------------------
# bpy python finder (same logic as filter_scenes.py)
# ---------------------------------------------------------------------------

def _find_bpy_python() -> str:
    """Python executable with ``bpy`` (for ``-m preprocess_lib.truncation_filter``)."""
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
) -> Dict[str, Any]:
    """Run truncation_filter in a bpy subprocess. Returns the parsed result dict."""
    candidates_payload = {"scene_id": scene_id, "objects": candidates}

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
    kept_gt_ids: List[str],
    gt_id_to_obj: Dict[str, Any],
) -> None:
    from configs.constant import canonicalize_asset_category

    objects_out = [gt_id_to_obj[gid] for gid in kept_gt_ids if gid in gt_id_to_obj]
    categories = sorted(
        {str(o["category"]) for o in objects_out if o.get("category") is not None}
    )
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
    skip_if_exists: bool,
    print_lock: threading.Lock,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Process one scene. Returns (job_idx, log_record_or_None).

    log_record is None when the scene is fully clean (no filters triggered, no errors)
    — clean scenes are counted in the batch summary but not written to the jsonl.
    """

    def _log(msg: str) -> None:
        with print_lock:
            print(msg)

    blend_dir = os.path.dirname(os.path.abspath(blend_path))
    scene_id = scene_id_from_output_dir(blend_dir)
    _log(f"\n{'─'*60}\n[{job_idx}/{total}] {scene_id}\n{'─'*60}")

    # Skip if objects_in_view.json already present
    out_path = os.path.join(blend_dir, OBJECTS_IN_VIEW_JSON)
    if skip_if_exists and os.path.isfile(out_path):
        _log(f"[inview] skip (exists): {out_path}")
        return job_idx, None

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
    gt_id_to_obj: Dict[str, Any] = f12["gt_id_to_obj"]

    # --- Filter 3 (bpy subprocess) ---
    filter_3: List[Dict[str, Any]] = []
    behind_camera_tris: List[Dict[str, Any]] = []
    kept_final: List[str] = kept_after_f2  # default: all pass if F3 skipped

    if kept_after_f2:
        candidates = [
            {"gt_id": gid, "actual_pixels": pixel_counts[gid]}
            for gid in kept_after_f2
        ]
        trunc_result = _run_truncation_subprocess(bpy_python, blend_path, scene_id, candidates)
        behind_camera_tris = trunc_result.get("behind_camera_tris") or []

        if not trunc_result.get("computed"):
            err = trunc_result.get("error", "unknown")
            _log(f"[WARN] truncation subprocess failed: {err} — F3 skipped, keeping all F2 survivors")
        else:
            kept_final = []
            for res in trunc_result.get("results") or []:
                if res.get("passed", True):
                    kept_final.append(res["gt_id"])
                else:
                    filter_3.append(
                        {
                            "gt_id": res["gt_id"],
                            "actual_pixels": res.get("actual_pixels"),
                            "projected_pixels": res.get("projected_pixels"),
                            "truncation_ratio": res.get("truncation_ratio"),
                        }
                    )

    # --- Write objects_in_view.json ---
    _write_objects_in_view(blend_dir, scene_id, kept_final, gt_id_to_obj)

    # --- Build log record (only if something interesting happened) ---
    any_filtered = bool(filter_1 or filter_2 or filter_3 or behind_camera_tris)
    status = "empty_after_filter" if not kept_final else "ok"

    if not any_filtered and status == "ok":
        # Clean scene — count in summary but don't write to jsonl
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
    if filter_3:
        record["filter_3_truncation"] = {"count": len(filter_3), "filtered": filter_3}
    if behind_camera_tris:
        record["behind_camera_tris"] = behind_camera_tris

    return job_idx, record


# ---------------------------------------------------------------------------
# Batch log writer
# ---------------------------------------------------------------------------

def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage-2 in-view filter: write objects_in_view.json with 3-filter pipeline."
    )
    parser.add_argument(
        "--scenes-root", required=True,
        help="Parent of scene folders (<root>/<scene_id>/scene.blend).",
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Parallel scene jobs (default: 1). Each spawns a bpy subprocess for Filter 3.",
    )
    parser.add_argument(
        "--skip-if-exists", action="store_true",
        help="Skip scenes where objects_in_view.json already exists.",
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

    blends = _discover_blend_paths(scenes_root)
    if not blends:
        print(f"[WARN] No scene.blend found under {scenes_root}")
        return 0

    log_path = os.path.join(scenes_root, INVIEW_BATCH_LOG_JSONL)
    print(f"[inview] scenes_root={scenes_root}")
    print(f"[inview] CUDA_VISIBLE_DEVICES={_cuda}")
    print(f"[inview] bpy python: {bpy_python}")
    print(f"[inview] {len(blends)} scene(s), workers={args.workers}")
    print(f"[inview] batch log -> {log_path}")

    print_lock = threading.Lock()
    indexed_records: List[Tuple[int, Optional[Dict[str, Any]]]] = []

    if args.workers == 1:
        for i, bp in enumerate(blends, 1):
            result = _process_one_scene(
                i, len(blends), bp, scenes_root, bpy_python,
                args.skip_if_exists, print_lock,
            )
            indexed_records.append(result)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    _process_one_scene,
                    i, len(blends), bp, scenes_root, bpy_python,
                    args.skip_if_exists, print_lock,
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
            # Clean scene
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
            ("filter_3_truncation", "filter_3_truncation"),
        ):
            if key in record:
                stats[stat_key]["scenes_affected"] += 1
                stats[stat_key]["objects_filtered"] += record[key].get("count", 0)

        _append_jsonl(log_path, record)

    # --- Batch summary ---
    summary = {
        "type": "batch_summary",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenes_root": scenes_root,
        **stats,
    }
    _append_jsonl(log_path, summary)

    print(f"\n[inview] DONE. batch log -> {log_path}")
    print(
        f"  total={stats['scenes_total']} ok={stats['scenes_ok']} "
        f"empty={stats['scenes_empty_after_filter']} errors={stats['scenes_with_errors']}"
    )
    print(
        f"  F1 affected={stats['filter_1_visibility']['scenes_affected']} "
        f"obj={stats['filter_1_visibility']['objects_filtered']}"
    )
    print(
        f"  F2 affected={stats['filter_2_aspect_ratio']['scenes_affected']} "
        f"obj={stats['filter_2_aspect_ratio']['objects_filtered']}"
    )
    print(
        f"  F3 affected={stats['filter_3_truncation']['scenes_affected']} "
        f"obj={stats['filter_3_truncation']['objects_filtered']}"
    )
    print(
        f"  behind_camera_tris scenes={stats['scenes_with_behind_camera_tris']}"
    )
    return 1 if stats["scenes_with_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())