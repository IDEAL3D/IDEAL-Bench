#!/usr/bin/env python3
"""
从磁盘上的 GT.json / info.json（及 CSV 中的 overlap_count 回退）补写 preprocess_batches.jsonl 中的
num_scenes_with_overlap、avg_gt_asset_count、avg_info_asset_count_in_view。

场景目录默认在 SCENES_PROCESSED/<scene_id>/；若已 promote 到 IDL3D，可传 ``--extra-scene-root`` 指向
例如 ``IDL3D/data/scenes_v1``（脚本会按 ``<类别>/<scene_id>`` 查找，类别为 scene_id 里 ``_s`` 前一段）。

用法::

    cd IDL3D/generation
    python backfill_preprocess_batches_metrics.py --dry-run
    python backfill_preprocess_batches_metrics.py
    python backfill_preprocess_batches_metrics.py --extra-scene-root ../data/scenes_v1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from preprocess_lib import orchestrator as p  # noqa: E402


def _load_batch_tables(
    manifests_dir: Path,
) -> tuple[dict[str, list[str]], dict[str, dict[str, int]]]:
    """batch_id -> scene_id 列表；batch_id -> scene_id -> CSV 中的 overlap_count（整数）。"""
    scenes: dict[str, list[str]] = {}
    overlaps: dict[str, dict[str, int]] = {}
    for csv_path in sorted(manifests_dir.glob("*_preprocess_scenes.csv")):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                bid = (row.get("preprocess_batch_id") or "").strip()
                sid = (row.get("scene_id") or "").strip()
                if not bid or not sid:
                    continue
                scenes.setdefault(bid, []).append(sid)
                try:
                    oc = int((row.get("overlap_count") or "0").strip() or 0)
                except ValueError:
                    oc = 0
                overlaps.setdefault(bid, {})[sid] = oc
    return scenes, overlaps


def _scene_category_prefix(scene_id: str) -> str:
    if "_s" not in scene_id:
        return scene_id
    return scene_id.rsplit("_s", 1)[0]


def _resolve_scene_dir(
    scene_id: str, processed_root: Path, extra_roots: list[Path]
) -> Path | None:
    """优先 PROCESSED/<scene_id>，再在 extra_roots/<prefix>/<scene_id> 查找。"""
    direct = processed_root / scene_id
    if direct.is_dir() and (direct / p.GT_JSON_NAME).is_file():
        return direct
    prefix = _scene_category_prefix(scene_id)
    for root in extra_roots:
        cand = root / prefix / scene_id
        if cand.is_dir() and (cand / p.GT_JSON_NAME).is_file():
            return cand
    return None


def _metrics_for_batch(
    scene_ids: list[str],
    csv_overlap: dict[str, int],
    processed_root: Path,
    extra_roots: list[Path],
) -> tuple[int, float | None, float | None, str]:
    """
    返回 (num_scenes_with_overlap, avg_gt, avg_iv, note)。
    重叠：优先 info.json；若无则 CSV overlap_count>0。
    均值：在能解析到的目录上取平均（与全量批处理的「全非 None 才写」不同，便于 promote 后补录）。
    """
    semantics = True
    overlap_n = 0
    gts: list[int] = []
    ivs: list[int] = []
    for sid in scene_ids:
        sd = _resolve_scene_dir(sid, processed_root, extra_roots)
        if sd is not None:
            opc = p._overlap_pair_count_from_info_dir(str(sd), semantics)
            if opc is not None:
                if opc > 0:
                    overlap_n += 1
            else:
                if csv_overlap.get(sid, 0) > 0:
                    overlap_n += 1
            g = p._read_gt_asset_count(str(sd))
            v = p._read_info_asset_count_in_view(str(sd))
            if g is not None and v is not None:
                gts.append(g)
                ivs.append(v)
        else:
            if csv_overlap.get(sid, 0) > 0:
                overlap_n += 1

    n = len(scene_ids)
    avg_gt: float | None
    avg_iv: float | None
    if not gts:
        avg_gt, avg_iv = None, None
    else:
        avg_gt = round(sum(gts) / len(gts), 6)
        avg_iv = round(sum(ivs) / len(ivs), 6)

    note = ""
    if len(gts) < n and n > 0:
        note = f"avg over {len(gts)}/{n} scenes with GT+info on disk"
    return overlap_n, avg_gt, avg_iv, note


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill preprocess_batches.jsonl overlap & asset averages from disk / CSV."
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print computed values only; do not write jsonl.",
    )
    ap.add_argument(
        "--extra-scene-root",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help=(
            "Extra root(s) with layout <room_prefix>/<scene_id>/ (e.g. IDL3D/data/scenes_v1). "
            "Repeat for multiple roots."
        ),
    )
    args = ap.parse_args()

    repo = Path(p._repo_root())
    manifests_dir = repo / "SCENES_PROCESSED" / "manifests"
    processed_root = repo / "SCENES_PROCESSED"
    jpath = manifests_dir / "preprocess_batches.jsonl"

    default_promote = repo / "data" / "scenes_v1"
    extra_roots = [Path(x).resolve() for x in args.extra_scene_root]
    if default_promote.is_dir() and default_promote not in extra_roots:
        extra_roots.insert(0, default_promote)

    if not jpath.is_file():
        print(f"[ERROR] missing {jpath}", file=sys.stderr)
        sys.exit(2)

    batch_to_scenes, batch_to_overlap = _load_batch_tables(manifests_dir)
    if not batch_to_scenes:
        print(f"[WARN] no rows in *_preprocess_scenes.csv under {manifests_dir}")
        sys.exit(0)

    if extra_roots:
        print(f"[info] extra scene roots: {', '.join(str(x) for x in extra_roots)}")

    raw_lines = jpath.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    for line in raw_lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        bid = rec.get("preprocess_batch_id")
        if not bid or bid not in batch_to_scenes:
            out_lines.append(line)
            continue
        scene_ids = batch_to_scenes[bid]
        csv_ov = batch_to_overlap.get(bid, {})
        ov, avg_gt, avg_iv, note = _metrics_for_batch(
            scene_ids, csv_ov, processed_root, extra_roots
        )
        rec["num_scenes_with_overlap"] = ov
        rec["avg_gt_asset_count"] = avg_gt
        rec["avg_info_asset_count_in_view"] = avg_iv
        ordered = {
            "preprocess_batch_id": rec["preprocess_batch_id"],
            "num_to_process": rec.get("num_to_process"),
            "num_processed": rec.get("num_processed"),
            "num_scenes_with_overlap": rec["num_scenes_with_overlap"],
            "start_seed": rec.get("start_seed"),
            "end_seed_inclusive": rec.get("end_seed_inclusive"),
            "timestamp": rec.get("timestamp"),
            "avg_gt_asset_count": rec["avg_gt_asset_count"],
            "avg_info_asset_count_in_view": rec["avg_info_asset_count_in_view"],
        }
        new_line = json.dumps(ordered, ensure_ascii=False)
        msg = (
            f"{bid}: num_scenes_with_overlap={ov} "
            f"avg_gt_asset_count={avg_gt} avg_info_asset_count_in_view={avg_iv}"
        )
        if note:
            msg += f"  ({note})"
        print(msg)
        out_lines.append(new_line)

    if args.dry_run:
        print("[dry-run] no file written.")
        return

    tmp = str(jpath) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")
    os.replace(tmp, jpath)
    print(f"[OK] updated {jpath}")


if __name__ == "__main__":
    main()
