#!/usr/bin/env python3
"""
Stage-1 scene filter: keep scenes with valid in-view semantics (``objects_in_view``-compatible) and no
furniture mesh overlap.

**CLI:** ``--scenes-root`` is required (no default). Discovery is **one level deep** only:
``<scenes-root>/<scene_id>/scene.blend``. Point at a room-type batch folder for large corpora.

**Pipeline:** (1) ``build_inview`` — reject on missing inputs / broken instance–metadata–GT chain;
(2) reject empty in-view (``objects_in_view_count == 0``); (3) Blender ``check_overlap`` — reject
on overlap. Failures move to ``<scenes-root>/invalid/<scene_id>/``; CSV/JSONL append under
``scenes-root``. ``--dry-run`` skips overlap unless ``--check-overlap``; ``--workers`` parallelizes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
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
from preprocess_lib.defaults import (
    BPY_PYTHON,
    FILTER_INVALID_SUBDIR,
    FILTER_SCENES_BATCHES_JSONL,
    FILTER_SCENES_REPORT_CSV,
)
from preprocess_lib.build_inview import (
    MissingInViewInfoError,
    build_instance_seg_view_payload,
)


def _find_bpy_python() -> str:
    """Python executable that has ``bpy`` (for ``-m preprocess_lib.check_overlap``); never imports bpy here."""
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


def _raw_skip_names() -> frozenset[str]:
    return frozenset({"manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"})


def discover_scene_blend_paths(scenes_root: str) -> List[str]:
    """``<root>/<scene_id>/scene.blend`` only (one level); or ``<root>/scene.blend`` if no subdir hits.

    Skips ``invalid``, manifests, dot dirs. For nested layouts (e.g. ``root/cat/id/``), point
    ``--scenes-root`` at the category folder so each run stays a manageable batch.
    """
    out: List[str] = []
    root = os.path.abspath(os.path.expanduser(scenes_root))
    if not os.path.isdir(root):
        return out
    skip = _raw_skip_names() | {FILTER_INVALID_SUBDIR}
    for name in sorted(os.listdir(root)):
        if name in skip or name.startswith("."):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        bp = os.path.join(d, "scene.blend")
        if os.path.isfile(bp):
            out.append(bp)
    if out:
        return out
    direct = os.path.join(root, "scene.blend")
    if os.path.isfile(direct):
        return [direct]
    return []


def _run_overlap_subprocess(
    bpy_python: str, blend_path: str
) -> Tuple[Optional[int], Optional[str], List[str], Optional[float]]:
    """Returns ``(pair_count, error, overlap_pair_strings, overlap_volume_m3_total)`` from subprocess JSON."""
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="filter_overlap_")
    os.close(fd)
    try:
        cmd = [
            bpy_python,
            "-m",
            "preprocess_lib.check_overlap",
            "--filter-write-json",
            blend_path,
            tmp_path,
        ]
        r = subprocess.run(cmd, check=False, cwd=str(_GENERATION_DIR))
        if r.returncode != 0:
            return None, f"subprocess_exit_{r.returncode}", [], None
        with open(tmp_path, encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
        err = data.get("error")
        if err:
            return None, str(err), [], None
        pc = data.get("pair_count")
        if pc is None:
            return None, "missing_pair_count", [], None
        raw_pairs = data.get("overlap_pair_strings")
        pair_strings: List[str] = []
        if isinstance(raw_pairs, list):
            pair_strings = [str(x) for x in raw_pairs]
        vol_total: Optional[float] = None
        raw_vol = data.get("overlap_volume_m3_total")
        if raw_vol is not None:
            try:
                vol_total = float(raw_vol)
            except (TypeError, ValueError):
                vol_total = None
        return int(pc), None, pair_strings, vol_total
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return None, str(e), [], None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _append_filter_scenes_csv(scenes_root: str, rows: List[Dict[str, Any]]) -> None:
    path = os.path.join(scenes_root, FILTER_SCENES_REPORT_CSV)
    write_header = (not os.path.isfile(path)) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(
                [
                    "scene_id",
                    "outcome",
                    "empty",
                    "ovlp",
                    "overlap_pairs",
                    "overlap_volume_m3_total",
                    "note",
                ]
            )
        for r in rows:
            w.writerow(
                [
                    r["scene_id"],
                    r["outcome"],
                    r["empty"],
                    r["ovlp"],
                    r["overlap_pairs"],
                    r["overlap_volume_m3_total"],
                    r["note"],
                ]
            )


def _append_filter_scenes_batch_jsonl(scenes_root: str, record: Dict[str, Any]) -> None:
    path = os.path.join(scenes_root, FILTER_SCENES_BATCHES_JSONL)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _trunc_field(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _aggregate_stats_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    s = {
        "kept": 0,
        "dry_run_inview_ok": 0,
        "invalid_empty_view": 0,
        "invalid_missing_inputs": 0,
        "invalid_overlap": 0,
        "errors": 0,
    }
    for r in rows:
        o = r.get("outcome") or ""
        if o == "kept":
            s["kept"] += 1
        elif o == "dry_run_inview_ok":
            s["dry_run_inview_ok"] += 1
        elif o == "invalid_empty_view":
            s["invalid_empty_view"] += 1
        elif o == "invalid_missing_inputs":
            s["invalid_missing_inputs"] += 1
        elif o == "invalid_overlap":
            s["invalid_overlap"] += 1
        elif o == "error":
            s["errors"] += 1
    return s


def _filter_one_blend(
    job_index: int,
    total: int,
    bp: str,
    scenes_root: str,
    run_overlap: bool,
    bpy_python: str,
    dry_run: bool,
    print_lock: threading.Lock,
) -> Tuple[int, Dict[str, Any]]:
    """Returns ``(1-based index for sort order, csv row)``."""

    def _plog(msg: str) -> None:
        with print_lock:
            print(msg)

    scene_dir = os.path.dirname(os.path.abspath(bp))
    scene_id = os.path.basename(scene_dir.rstrip(os.sep))
    _plog(f"\n{'─' * 60}\n[{job_index}/{total}] {scene_id}\n{'─' * 60}")

    row: Dict[str, Any] = {
        "scene_id": scene_id,
        "outcome": "",
        "empty": "",
        "ovlp": "",
        "overlap_pairs": "",
        "overlap_volume_m3_total": "",
        "note": "",
    }

    try:
        payload = build_instance_seg_view_payload(scene_id, scene_dir)
    except MissingInViewInfoError as e:
        _plog(f"[reject] in_view: {e}")
        row["outcome"] = "invalid_missing_inputs"
        row["empty"] = "0"
        row["note"] = _trunc_field(str(e), 2000)
        try:
            _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=dry_run)
        except OSError as ex:
            _plog(f"[ERROR] move failed: {ex}")
            row["outcome"] = "error"
            row["note"] = _trunc_field(f"move failed: {ex}", 2000)
        return job_index, row
    except Exception as e:
        _plog(f"[ERROR] in_view unexpected: {type(e).__name__}: {e}")
        row["outcome"] = "error"
        row["empty"] = "0"
        row["note"] = _trunc_field(f"{type(e).__name__}: {e}", 2000)
        return job_index, row

    if int(payload.get("objects_in_view_count") or 0) == 0:
        _plog("[reject] in_view: no qualified assets (empty objects_in_view semantics)")
        row["outcome"] = "invalid_empty_view"
        row["empty"] = "1"
        row["note"] = "objects_in_view empty"
        try:
            _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=dry_run)
        except OSError as ex:
            _plog(f"[ERROR] move failed: {ex}")
            row["outcome"] = "error"
            row["note"] = _trunc_field(f"move failed: {ex}", 2000)
        return job_index, row

    _plog(
        f"[ok] in_view: {payload.get('objects_in_view_count')} object(s), "
        f"categories={payload.get('categories_in_view')}"
    )

    row["empty"] = "0"
    if not run_overlap:
        _plog("[dry-run] skip Blender overlap (pass --check-overlap for full dry-run)")
        row["outcome"] = "dry_run_inview_ok"
        row["note"] = "overlap_skipped_dry_run"
        return job_index, row

    pair_count, ov_err, pair_strings, vol_total = _run_overlap_subprocess(
        bpy_python, bp
    )
    if ov_err is not None:
        _plog(f"[ERROR] overlap check failed: {ov_err}")
        row["outcome"] = "error"
        row["note"] = _trunc_field(ov_err, 2000)
        return job_index, row

    assert pair_count is not None
    row["ovlp"] = str(pair_count)
    if vol_total is not None:
        row["overlap_volume_m3_total"] = f"{vol_total:.6f}"
    _plog(
        f"[overlap] pair_count={pair_count} "
        f"overlap_volume_m3_total={row['overlap_volume_m3_total'] or '-'}"
    )
    if pair_count > 0:
        _plog("[reject] furniture overlap")
        row["outcome"] = "invalid_overlap"
        row["overlap_pairs"] = _trunc_field(";".join(pair_strings), 15000)
        try:
            _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=dry_run)
        except OSError as ex:
            _plog(f"[ERROR] move failed: {ex}")
            row["outcome"] = "error"
            row["note"] = _trunc_field(f"move failed: {ex}", 2000)
    else:
        _plog("[ok] no overlap")
        row["outcome"] = "kept"
    return job_index, row


def _move_to_invalid(
    scene_dir: str,
    scenes_root: str,
    scene_id: str,
    *,
    dry_run: bool,
) -> None:
    dest_parent = os.path.join(scenes_root, FILTER_INVALID_SUBDIR)
    dest = os.path.join(dest_parent, scene_id)
    if dry_run:
        print(f"[dry-run] would move -> {dest}")
        return
    os.makedirs(dest_parent, exist_ok=True)
    if os.path.exists(dest):
        raise FileExistsError(f"refuse to overwrite existing {dest}")
    shutil.move(scene_dir, dest)
    print(f"[moved] -> {dest}")


def main() -> int:
    epilog = """
Order: (1) same in-view payload as objects_in_view.json (build_inview); (2) check_overlap in a Blender subprocess.

--dry-run skips Blender overlap by default (fast). Use --dry-run --check-overlap for a full check without moving.

--workers runs multiple scenes in parallel (threads + one Blender subprocess each). Watch GPU/RAM if overlap is enabled.
""".strip()
    p = argparse.ArgumentParser(
        description="Filter scenes: empty in-view semantics (vs objects_in_view) or furniture overlap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument(
        "--scenes-root",
        type=str,
        required=True,
        help="Parent of scene folders (<root>/<scene_id>/scene.blend). Required.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without moving directories. Skips Blender overlap unless --check-overlap.",
    )
    p.add_argument(
        "--check-overlap",
        action="store_true",
        help="With --dry-run, still run check_overlap in Blender (slow). Ignored when not --dry-run.",
    )
    p.add_argument(
        "--gpu",
        type=str,
        default=None,
        help="CUDA_VISIBLE_DEVICES for Blender subprocess (same as preprocess).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Parallel scene jobs (default: 1). Each overlap check spawns Blender; avoid OOM on GPU/RAM.",
    )
    args = p.parse_args()
    if args.workers < 1:
        print("[ERROR] --workers must be >= 1", file=sys.stderr)
        return 2

    scenes_root = os.path.abspath(os.path.expanduser(args.scenes_root))
    _cuda = resolve_cuda_visible_devices(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = _cuda

    blends = discover_scene_blend_paths(scenes_root)
    if not blends:
        print(f"[WARN] No scene.blend under {scenes_root}")
        return 0

    run_overlap = (not args.dry_run) or bool(args.check_overlap)
    bpy_python = _find_bpy_python() if run_overlap else ""
    print(f"[filter_scenes] scenes_root={scenes_root}")
    print(f"[filter_scenes] CUDA_VISIBLE_DEVICES={_cuda}")
    if run_overlap:
        print(f"[filter_scenes] bpy python: {bpy_python}")
    else:
        print("[filter_scenes] Blender overlap skipped (dry-run; pass --check-overlap to run it)")
    print(f"[filter_scenes] {len(blends)} scene(s), workers={args.workers}")

    print_lock = threading.Lock()
    indexed_rows: List[Tuple[int, Dict[str, Any]]] = []
    total_n = len(blends)

    if args.workers == 1:
        for i, bp in enumerate(blends, 1):
            _, row = _filter_one_blend(
                i,
                total_n,
                bp,
                scenes_root,
                run_overlap,
                bpy_python,
                bool(args.dry_run),
                print_lock,
            )
            indexed_rows.append((i, row))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    _filter_one_blend,
                    i,
                    total_n,
                    bp,
                    scenes_root,
                    run_overlap,
                    bpy_python,
                    bool(args.dry_run),
                    print_lock,
                ): i
                for i, bp in enumerate(blends, 1)
            }
            for fut in as_completed(futs):
                indexed_rows.append(fut.result())

    indexed_rows.sort(key=lambda t: t[0])
    rows = [r for _, r in indexed_rows]
    stats = _aggregate_stats_from_rows(rows)

    _append_filter_scenes_csv(scenes_root, rows)
    first_sid = os.path.basename(os.path.dirname(os.path.abspath(blends[0])))
    last_sid = os.path.basename(os.path.dirname(os.path.abspath(blends[-1])))
    ts = datetime.now(timezone.utc)
    batch_record = {
        "run_id": f"{ts.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "timestamp_utc": ts.isoformat(),
        "scenes_root": scenes_root,
        "first_scene_id": first_sid,
        "last_scene_id": last_sid,
        "all_ok": stats["errors"] == 0,
        "total_scenes": len(blends),
        "kept": stats["kept"],
        "dry_run_inview_ok": stats["dry_run_inview_ok"],
        "invalid_empty_view": stats["invalid_empty_view"],
        "invalid_missing_inputs": stats["invalid_missing_inputs"],
        "invalid_overlap": stats["invalid_overlap"],
        "errors": stats["errors"],
        "dry_run": bool(args.dry_run),
        "overlap_skipped": not run_overlap,
        "workers": args.workers,
        "report_csv": FILTER_SCENES_REPORT_CSV,
        "batches_jsonl": FILTER_SCENES_BATCHES_JSONL,
    }
    _append_filter_scenes_batch_jsonl(scenes_root, batch_record)
    print(
        f"\n[DONE] report -> {os.path.join(scenes_root, FILTER_SCENES_REPORT_CSV)} "
        f"(append); batch -> {os.path.join(scenes_root, FILTER_SCENES_BATCHES_JSONL)} (append)"
    )
    print(
        f"counts: kept={stats['kept']} dry_run_inview_ok={stats['dry_run_inview_ok']} "
        f"invalid_empty={stats['invalid_empty_view']} "
        f"invalid_missing={stats['invalid_missing_inputs']} invalid_overlap={stats['invalid_overlap']} "
        f"errors={stats['errors']} dry_run={args.dry_run} overlap_skipped={not run_overlap} "
        f"workers={args.workers}"
    )
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
