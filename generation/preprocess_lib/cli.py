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
    FINAL_SCOPE_ROOT,
    ROOM_TYPES,
    _append_preprocess_batches_jsonl,
    _append_preprocess_log_block,
    _assert_raw_scenes_ready_for_final,
    _copy_raw_scenes_to_processed,
    _default_log_path,
    _discover_scene_ids_under_raw,
    _final_scope_normalized,
    _format_log_single,
    _load_raw_scene_rows,
    _load_scene_id_to_batch_csv,
    _next_preprocess_batch_id,
    _preprocess_batches_jsonl_path,
    _preprocess_scenes_csv_path,
    _processed_manifests_dir,
    _processed_root,
    _run_batch,
    _seed_from_scene_id,
    _sync_sketch_to_scenes2test,
)
from .pipeline import _scene_output_dir, run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Box scene preprocessor（GT + objects_in_view.json，内含家具重叠摘要）"
    )
    parser.add_argument("--blend", type=str, default=None, help="Path to scene.blend")
    parser.add_argument(
        "--scenes-root",
        type=str,
        default=None,
        help=(
            "非 --final：批处理根目录（各 <scene_id>/scene.blend）。"
            "省略时默认 INFINIGEN/SCENES2TEST；"
            "若指定路径则只处理该目录，且默认不从 SCENES_SKETCH 同步。"
        ),
    )
    parser.add_argument(
        "--no-sync-sketch",
        action="store_true",
        help="非 --final：跳过 SCENES_SKETCH → SCENES2TEST 的目录复制，直接处理 --scenes-root 下已有数据。",
    )
    parser.add_argument(
        "--sync-sketch-overwrite",
        action="store_true",
        help="非 --final：同步时若 SCENES2TEST 下已有同名场景目录则先删除再复制。",
    )
    parser.add_argument(
        "--no-semantics",
        action="store_true",
        help="只写 GT.json，不写 objects_in_view.json（亦不跑视锥与重叠筛查）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        metavar="PATH",
        help=f"Append brief summary log (default: INFINIGEN/SCENES2TEST/{DEFAULT_LOG_FILENAME})",
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
            "正式管线：SCENES_RAW → INFINIGEN/SCENES_PROCESSED + manifests/；"
            "批末将家具重叠场景移入 rejected_furniture_overlap/ 并写 manifests 重叠日志与 overlap_scenes.csv。 "
            f"须配合 --room-type：{FINAL_SCOPE_ROOT!r} 表示 RAW 根目录平铺的十种房型 scene_id；"
            f"否则为 {list(ROOM_TYPES)} 之一，处理 SCENES_RAW/<房型>/ 下子目录。 "
            "可选 --raw-batch-id（仅非 root 时）按 RAW manifests 中 batch 过滤。 "
            "非 --final：默认 SCENES_SKETCH → SCENES2TEST 同步后在 INFINIGEN/SCENES2TEST 批处理。"
        ),
    )
    parser.add_argument(
        "--room-type",
        type=str,
        default=None,
        metavar="TYPE",
        help=(
            f"仅 --final：{FINAL_SCOPE_ROOT!r}=RAW 根平铺全系场景；"
            f"否则为房型名，处理 SCENES_RAW/<房型>/。合法房型：{list(ROOM_TYPES)}。"
        ),
    )
    parser.add_argument(
        "--raw-batch-id",
        action="append",
        dest="raw_batch_ids",
        default=None,
        metavar="ID",
        help=(
            "仅 --final 且 --room-type 为具体房型（非 root）：与 manifests 中 <房型>_scenes.csv 里 "
            "所列 batch_id 求交。可重复。与 --room-type root 互斥。"
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
    if args.no_semantics:
        extra.append("--no-semantics")

    log_path: Optional[str] = None
    if not args.no_log:
        log_path = os.path.abspath(args.log_file) if args.log_file else _default_log_path()

    semantics_enabled = not args.no_semantics

    if args.room_type and not args.final_mode:
        print("[ERROR] --room-type is only valid with --final (or --formal)", file=sys.stderr)
        sys.exit(2)
    if (args.no_sync_sketch or args.sync_sketch_overwrite) and args.final_mode:
        print(
            "[ERROR] --no-sync-sketch / --sync-sketch-overwrite 仅用于非 --final",
            file=sys.stderr,
        )
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
        try:
            scope = _final_scope_normalized(args.room_type)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        raw_ids: Optional[List[str]] = (
            list(args.raw_batch_ids) if args.raw_batch_ids else None
        )
        if raw_ids and scope == FINAL_SCOPE_ROOT:
            print(
                "[ERROR] --raw-batch-id 与 --room-type root 不能同时使用（无单一 RAW CSV）。",
                file=sys.stderr,
            )
            sys.exit(2)

        discovered = _discover_scene_ids_under_raw(scope)
        if not discovered:
            if scope == FINAL_SCOPE_ROOT:
                hint = (
                    "SCENES_RAW 根下无平铺 scene_id（十种房型前缀 + _s<seed>；"
                    "不含 SCENES_RAW/bedroom 等子目录内场景）。"
                )
            else:
                hint = (
                    f"SCENES_RAW/{scope} 下无子目录 scene_id（须为 {scope}_s<seed>）。"
                )
            print(f"[ERROR] {hint}", file=sys.stderr)
            sys.exit(2)

        batch_by_sid = _load_scene_id_to_batch_csv(scope)

        if raw_ids:
            try:
                csv_rows = _load_raw_scene_rows(scope, raw_ids)
            except (FileNotFoundError, ValueError) as e:
                print(f"[ERROR] {e}", file=sys.stderr)
                sys.exit(2)
            allowed = {r["scene_id"] for r in csv_rows}
            scene_ids = sorted([s for s in discovered if s in allowed])
            if not scene_ids:
                print(
                    "[ERROR] 磁盘发现与 --raw-batch-id 对应 CSV 行无交集。",
                    file=sys.stderr,
                )
                sys.exit(2)
            print(
                f"[final] {len(scene_ids)} scene(s) (discovered ∩ CSV batch: {', '.join(raw_ids)})"
            )
        else:
            scene_ids = list(discovered)
            loc = "RAW 根平铺" if scope == FINAL_SCOPE_ROOT else f"SCENES_RAW/{scope}/"
            print(f"[final] {len(scene_ids)} scene(s) ({loc})")

        pairs = [(sid, batch_by_sid.get(sid, "")) for sid in scene_ids]

        try:
            _assert_raw_scenes_ready_for_final(scope, scene_ids)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        manifests_dir = _processed_manifests_dir()
        preprocess_batch_id = _next_preprocess_batch_id(manifests_dir, scope)
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
                    scope=scope,
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

        csv_path = _preprocess_scenes_csv_path(scope, manifests_dir)
        manifest_ctx: Dict[str, Any] = {
            "preprocess_batch_id": preprocess_batch_id,
            "room_type": scope,
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

    batch_root: Optional[str] = None
    if args.scenes_root:
        batch_root = os.path.abspath(os.path.expanduser(args.scenes_root))
    else:
        batch_root = None
        if not args.no_sync_sketch:
            _sync_sketch_to_scenes2test(overwrite=bool(args.sync_sketch_overwrite))

    print(
        "[preprocess] 试跑：INFINIGEN/SCENES2TEST（或 --scenes-root）；"
        "默认先 SCENES_SKETCH→SCENES2TEST（可用 --no-sync-sketch 关闭）；不写 SCENES_PROCESSED/manifests。"
    )
    code = _run_batch(
        batch_root,
        extra,
        semantics_enabled=semantics_enabled,
        log_path=log_path,
        skip_if_gt=not args.force,
    )
    sys.exit(code)
