#!/usr/bin/env python3
"""
Stage-1 filter: ``build_inview.build_instance_seg_view_payload`` (same as ``objects_in_view`` in-view list);
then ``check_overlap`` in a Blender subprocess. Invalid dirs → ``<scenes-root>/invalid/<scene_id>/``.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_GENERATION_DIR = Path(__file__).resolve().parent
if str(_GENERATION_DIR) not in sys.path:
    sys.path.insert(0, str(_GENERATION_DIR))

_REPO_ROOT = _GENERATION_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.data_paths import SCENES_RAW_ROOT
from configs.gpu_select import resolve_cuda_visible_devices
from preprocess_lib.core import _find_bpy_python
from preprocess_lib.defaults import (
    FILTER_INVALID_SUBDIR,
    FILTER_SCENES_BATCHES_JSONL,
    FILTER_SCENES_REPORT_CSV,
)
from preprocess_lib.build_inview import (
    MissingInViewInfoError,
    build_instance_seg_view_payload,
)


def _raw_skip_names() -> frozenset[str]:
    return frozenset({"manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"})


def discover_scene_blend_paths(scenes_root: str) -> List[str]:
    """``<root>/<scene_id>/scene.blend``; skips ``invalid``, manifests, dot dirs."""
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
""".strip()
    p = argparse.ArgumentParser(
        description="Filter scenes: empty in-view semantics (vs objects_in_view) or furniture overlap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    p.add_argument(
        "--scenes-root",
        type=str,
        default=str(SCENES_RAW_ROOT.resolve()),
        help=f"Parent of scene folders (default: {SCENES_RAW_ROOT})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without moving directories.",
    )
    p.add_argument(
        "--gpu",
        type=str,
        default=None,
        help="CUDA_VISIBLE_DEVICES for Blender subprocess (same as preprocess).",
    )
    args = p.parse_args()

    scenes_root = os.path.abspath(os.path.expanduser(args.scenes_root))
    _cuda = resolve_cuda_visible_devices(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = _cuda

    blends = discover_scene_blend_paths(scenes_root)
    if not blends:
        print(f"[WARN] No scene.blend under {scenes_root}")
        return 0

    bpy_python = _find_bpy_python()
    print(f"[filter_scenes] scenes_root={scenes_root}")
    print(f"[filter_scenes] CUDA_VISIBLE_DEVICES={_cuda}")
    print(f"[filter_scenes] bpy python: {bpy_python}")
    print(f"[filter_scenes] {len(blends)} scene(s)")

    def _trunc_field(s: str, max_len: int) -> str:
        if len(s) <= max_len:
            return s
        return s[: max_len - 3] + "..."

    rows: List[Dict[str, Any]] = []
    stats = {
        "kept": 0,
        "invalid_empty_view": 0,
        "invalid_missing_inputs": 0,
        "invalid_overlap": 0,
        "errors": 0,
    }

    for i, bp in enumerate(blends, 1):
        scene_dir = os.path.dirname(os.path.abspath(bp))
        scene_id = os.path.basename(scene_dir.rstrip(os.sep))
        print(f"\n{'─' * 60}\n[{i}/{len(blends)}] {scene_id}\n{'─' * 60}")

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
            print(f"[reject] in_view: {e}")
            row["outcome"] = "invalid_missing_inputs"
            row["empty"] = "0"
            row["note"] = _trunc_field(str(e), 2000)
            stats["invalid_missing_inputs"] += 1
            try:
                _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=bool(args.dry_run))
            except OSError as ex:
                print(f"[ERROR] move failed: {ex}")
                stats["errors"] += 1
                row["outcome"] = "error"
                row["note"] = _trunc_field(f"move failed: {ex}", 2000)
            rows.append(row)
            continue

        if int(payload.get("objects_in_view_count") or 0) == 0:
            print("[reject] in_view: no qualified assets (empty objects_in_view semantics)")
            row["outcome"] = "invalid_empty_view"
            row["empty"] = "1"
            row["note"] = "objects_in_view empty"
            stats["invalid_empty_view"] += 1
            try:
                _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=bool(args.dry_run))
            except OSError as ex:
                print(f"[ERROR] move failed: {ex}")
                stats["errors"] += 1
                row["outcome"] = "error"
                row["note"] = _trunc_field(f"move failed: {ex}", 2000)
            rows.append(row)
            continue

        print(
            f"[ok] in_view: {payload.get('objects_in_view_count')} object(s), "
            f"categories={payload.get('categories_in_view')}"
        )

        pair_count, ov_err, pair_strings, vol_total = _run_overlap_subprocess(
            bpy_python, bp
        )
        row["empty"] = "0"
        if ov_err is not None:
            print(f"[ERROR] overlap check failed: {ov_err}")
            row["outcome"] = "error"
            row["note"] = _trunc_field(ov_err, 2000)
            stats["errors"] += 1
            rows.append(row)
            continue

        assert pair_count is not None
        row["ovlp"] = str(pair_count)
        if vol_total is not None:
            row["overlap_volume_m3_total"] = f"{vol_total:.6f}"
        print(
            f"[overlap] pair_count={pair_count} "
            f"overlap_volume_m3_total={row['overlap_volume_m3_total'] or '-'}"
        )
        if pair_count > 0:
            print("[reject] furniture overlap")
            row["outcome"] = "invalid_overlap"
            row["overlap_pairs"] = _trunc_field(";".join(pair_strings), 15000)
            stats["invalid_overlap"] += 1
            try:
                _move_to_invalid(scene_dir, scenes_root, scene_id, dry_run=bool(args.dry_run))
            except OSError as ex:
                print(f"[ERROR] move failed: {ex}")
                stats["errors"] += 1
                row["outcome"] = "error"
                row["note"] = _trunc_field(f"move failed: {ex}", 2000)
        else:
            print("[ok] no overlap")
            row["outcome"] = "kept"
            stats["kept"] += 1
        rows.append(row)

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
        "invalid_empty_view": stats["invalid_empty_view"],
        "invalid_missing_inputs": stats["invalid_missing_inputs"],
        "invalid_overlap": stats["invalid_overlap"],
        "errors": stats["errors"],
        "dry_run": bool(args.dry_run),
        "report_csv": FILTER_SCENES_REPORT_CSV,
        "batches_jsonl": FILTER_SCENES_BATCHES_JSONL,
    }
    _append_filter_scenes_batch_jsonl(scenes_root, batch_record)
    print(
        f"\n[DONE] report -> {os.path.join(scenes_root, FILTER_SCENES_REPORT_CSV)} "
        f"(append); batch -> {os.path.join(scenes_root, FILTER_SCENES_BATCHES_JSONL)} (append)"
    )
    print(
        f"counts: kept={stats['kept']} invalid_empty={stats['invalid_empty_view']} "
        f"invalid_missing={stats['invalid_missing_inputs']} invalid_overlap={stats['invalid_overlap']} "
        f"errors={stats['errors']} dry_run={args.dry_run}"
    )
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
