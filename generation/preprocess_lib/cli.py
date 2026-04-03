"""argparse 与分支调度：调用 ``core`` / ``pipeline``。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .defaults import DEFAULT_LOG_FILENAME, GT_JSON_NAME
from .core import (
    _append_preprocess_batches_jsonl,
    _append_preprocess_log_block,
    _assert_raw_scenes_ready_for_final,
    _copy_raw_scenes_to_processed,
    _default_log_path,
    _discover_scene_ids_under_raw,
    _format_log_single,
    _load_raw_scene_rows,
    _load_scene_id_to_batch_csv,
    _next_preprocess_batch_id,
    _preprocess_batches_jsonl_path,
    _preprocess_scenes_csv_path,
    _processed_manifests_dir,
    _processed_root,
    _room_folder_name,
    _run_batch,
    _seed_from_scene_id,
)
from .pipeline import _scene_output_dir, run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Box scene preprocessor (GT + info.json)")
    parser.add_argument("--blend", type=str, default=None, help="Path to scene.blend")
    parser.add_argument(
        "--scenes-root",
        type=str,
        default=None,
        help=(
            "Batch: parent dir with <scene_name>/scene.blend each, OR a single scene folder "
            "containing scene.blend directly (not a path to the .blend file — use --blend for that). "
            "Default: ../SCENES2TEST"
        ),
    )
    parser.add_argument(
        "--no-semantics",
        action="store_true",
        help="Do not write info.json (camera / categories / overlap)",
    )
    parser.add_argument(
        "--no-overlap",
        action="store_true",
        help="Write info.json but skip furniture_overlap voxel computation",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        metavar="PATH",
        help=f"Append brief summary log (default: <REPO>/SCENES2TEST/{DEFAULT_LOG_FILENAME})",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Do not write summary log file",
    )
    parser.add_argument(
        "--force",
        "--non-skip",
        action="store_true",
        dest="force",
        help=(
            f"Re-run preprocessing even if {GT_JSON_NAME} exists (batch mode skips processed scenes by default). "
            "`--non-skip` is an alias for `--force`."
        ),
    )
    parser.add_argument(
        "--final",
        "--formal",
        action="store_true",
        dest="final_mode",
        help=(
            "Formal pipeline: SCENES_RAW -> SCENES_PROCESSED + manifests/ "
            "(preprocess_batches.jsonl, *_preprocess_scenes.csv). "
            "Requires --room-type. Scene list: flat SCENES_RAW/<room_type>_s*/ only; "
            "SCENES_RAW/<room>/ (nested) is treated as archived and skipped. "
            "CSV / --raw-batch-id never expands the set (intersection with disk only). "
            "Each scene must have scene.blend and at least one .png (any basename). "
            "Optional --raw-batch-id filters to rows in *_scenes.csv for those batch_ids. "
            "Without --final, only --scenes-root (default SCENES2TEST) is used in-place; no SCENES_PROCESSED/manifests."
        ),
    )
    parser.add_argument(
        "--room-type",
        type=str,
        default=None,
        metavar="TYPE",
        help="Only with --final/--formal: e.g. bedroom (SCENES_RAW/manifests/{TYPE}_scenes.csv).",
    )
    parser.add_argument(
        "--raw-batch-id",
        action="append",
        dest="raw_batch_ids",
        default=None,
        metavar="ID",
        help=(
            "Only with --final: intersect flat-root SCENES_RAW/<scene_id>/ (discovered) with these "
            "batch_id rows in RAW *_scenes.csv. CSV-only rows (e.g. scene only under SCENES_RAW/<room>/) "
            "are never selected. Repeat to merge multiple batches. Omit for all discovered flat scenes."
        ),
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Only with --final: skip copy from SCENES_RAW (expect dirs already under SCENES_PROCESSED/).",
    )
    parser.add_argument(
        "--overwrite-processed",
        action="store_true",
        help="Only with --final: replace existing SCENES_PROCESSED/<scene_id> when copying.",
    )
    args = parser.parse_args()

    extra: List[str] = []
    if args.no_overlap:
        extra.append("--no-overlap")
    if args.no_semantics:
        extra.append("--no-semantics")

    log_path: Optional[str] = None
    if not args.no_log:
        log_path = os.path.abspath(args.log_file) if args.log_file else _default_log_path()

    semantics_enabled = not args.no_semantics

    if args.room_type and not args.final_mode:
        print("[ERROR] --room-type is only valid with --final (or --formal)", file=sys.stderr)
        sys.exit(2)
    if args.raw_batch_ids and not args.final_mode:
        print("[ERROR] --raw-batch-id is only valid with --final (or --formal)", file=sys.stderr)
        sys.exit(2)
    if args.no_copy and not args.final_mode:
        print("[ERROR] --no-copy is only valid with --final (or --formal)", file=sys.stderr)
        sys.exit(2)
    if args.overwrite_processed and not args.final_mode:
        print(
            "[ERROR] --overwrite-processed is only valid with --final (or --formal)",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.final_mode:
        if args.blend:
            print("[ERROR] --final cannot be used with --blend", file=sys.stderr)
            sys.exit(2)
        if args.scenes_root:
            print(
                "[WARN] --scenes-root is ignored in --final mode "
                "(scene list comes from SCENES_RAW disk layout).",
                file=sys.stderr,
            )
        if not args.room_type:
            print("[ERROR] --final requires --room-type", file=sys.stderr)
            sys.exit(2)
        room_type = args.room_type.strip()
        raw_ids: Optional[List[str]] = (
            list(args.raw_batch_ids) if args.raw_batch_ids else None
        )
        discovered = _discover_scene_ids_under_raw(room_type)
        if not discovered:
            print(
                f"[ERROR] SCENES_RAW 根目录下未发现待预处理场景："
                f"请确认存在 SCENES_RAW/<scene_id>/（平铺，scene_id 形如 {_room_folder_name(room_type)}_s<seed>）。"
                f"已收入 SCENES_RAW/{_room_folder_name(room_type)}/ 的场景不会参与 --final。",
                file=sys.stderr,
            )
            sys.exit(2)

        batch_by_sid = _load_scene_id_to_batch_csv(room_type)

        if raw_ids:
            try:
                csv_rows = _load_raw_scene_rows(room_type, raw_ids)
            except (FileNotFoundError, ValueError) as e:
                print(f"[ERROR] {e}", file=sys.stderr)
                sys.exit(2)
            allowed = {r["scene_id"] for r in csv_rows}
            scene_ids = sorted([s for s in discovered if s in allowed])
            if not scene_ids:
                print(
                    "[ERROR] 根目录平铺场景与 --raw-batch-id 在 CSV 中的登记无交集。"
                    "（仅 SCENES_RAW/<scene_id>/ 可预处理；CSV 中已登记但不在根目录的场景会被忽略。）",
                    file=sys.stderr,
                )
                sys.exit(2)
            print(
                f"[final] {len(scene_ids)} scene(s) under SCENES_RAW "
                f"(discovered ∩ CSV batch filter: {', '.join(raw_ids)})"
            )
        else:
            scene_ids = list(discovered)
            print(
                f"[final] {len(scene_ids)} scene(s) under SCENES_RAW "
                f"(flat root only; SCENES_RAW/{_room_folder_name(room_type)}/ excluded)"
            )

        pairs = [(sid, batch_by_sid.get(sid, "")) for sid in scene_ids]

        try:
            _assert_raw_scenes_ready_for_final(room_type, scene_ids)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        manifests_dir = _processed_manifests_dir()
        preprocess_batch_id = _next_preprocess_batch_id(manifests_dir, room_type)
        seeds = [_seed_from_scene_id(sid) for sid in scene_ids]
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        record = {
            "preprocess_batch_id": preprocess_batch_id,
            "num_to_process": len(scene_ids),
            "num_processed": None,
            "num_scenes_with_overlap": None,
            "start_seed": min(seeds),
            "end_seed_inclusive": None,
            "timestamp": ts,
            "avg_gt_asset_count": None,
            "avg_info_asset_count_in_view": None,
        }
        jpath = _preprocess_batches_jsonl_path(manifests_dir)
        _append_preprocess_batches_jsonl(jpath, record)
        print(f"[final] pending batch -> {jpath} ({preprocess_batch_id})")

        if not args.no_copy:
            try:
                _copy_raw_scenes_to_processed(
                    scene_ids,
                    overwrite=bool(args.overwrite_processed),
                )
            except (FileNotFoundError, FileExistsError, OSError) as e:
                print(f"[ERROR] copy from RAW failed: {e}", file=sys.stderr)
                sys.exit(2)
        else:
            print("[final] --no-copy: expect scenes already under SCENES_PROCESSED/")

        proc_root = _processed_root()
        blend_paths: List[str] = []
        for sid in scene_ids:
            bp = os.path.join(proc_root, sid, "scene.blend")
            if not os.path.isfile(bp):
                print(f"[ERROR] missing scene.blend: {bp}", file=sys.stderr)
                sys.exit(2)
            blend_paths.append(bp)

        csv_path = _preprocess_scenes_csv_path(room_type, manifests_dir)
        manifest_ctx: Dict[str, Any] = {
            "preprocess_batch_id": preprocess_batch_id,
            "room_type": room_type,
            "manifests_dir": manifests_dir,
            "preprocess_scenes_csv": csv_path,
            "scene_raw_batch_pairs": pairs,
        }
        code = _run_batch(
            proc_root,
            extra,
            semantics_enabled=semantics_enabled,
            log_path=log_path,
            skip_if_gt=not args.force,
            blend_paths=blend_paths,
            manifest_ctx=manifest_ctx,
        )
        sys.exit(code)

    if args.blend:
        scene_dir_single = _scene_output_dir(args.blend)
        gt_single = os.path.join(scene_dir_single, GT_JSON_NAME)
        if not args.force and os.path.isfile(gt_single):
            print(f"[SKIP] {GT_JSON_NAME} already exists, skipping: {scene_dir_single}")
            print("        Re-run with --force or --non-skip to overwrite.")
            sys.exit(0)
        t0 = time.perf_counter()
        res: Dict[str, Any] | None = None
        try:
            res = run_pipeline(
                args.blend,
                write_semantics=semantics_enabled,
                compute_overlap=not args.no_overlap,
            )
        except BaseException as ex:
            elapsed = time.perf_counter() - t0
            label = os.path.basename(os.path.dirname(os.path.abspath(args.blend)))
            res = {
                "ok": False,
                "scene_label": label,
                "output_dir": "",
                "error": f"unhandled_exception:{type(ex).__name__}",
                "overlap_pair_count": None,
            }
            if log_path:
                _append_preprocess_log_block(log_path, _format_log_single(res, elapsed))
            raise
        elapsed = time.perf_counter() - t0
        if log_path:
            _append_preprocess_log_block(log_path, _format_log_single(res, elapsed))
        sys.exit(0 if res.get("ok") else 1)

    if args.scenes_root:
        sr = os.path.abspath(os.path.expanduser(args.scenes_root))
        if os.path.isfile(sr) and sr.lower().endswith(".blend"):
            print(
                "[ERROR] --scenes-root must be a scenes parent directory or a single scene folder "
                "containing scene.blend, not a path to a .blend file.",
                file=sys.stderr,
            )
            print(f"        Use instead: python preprocess.py --blend {sr}", file=sys.stderr)
            sys.exit(2)
        if not os.path.isdir(sr):
            print(f"[ERROR] Not a valid directory: {sr}", file=sys.stderr)
            sys.exit(2)

    print(
        "[preprocess] 默认/试跑：就地处理 --scenes-root 下场景（默认 ../SCENES2TEST），"
        "不写 SCENES_PROCESSED/manifests；正式管线使用 --final。"
    )
    code = _run_batch(
        args.scenes_root,
        extra,
        semantics_enabled=semantics_enabled,
        log_path=log_path,
        skip_if_gt=not args.force,
    )
    sys.exit(code)
