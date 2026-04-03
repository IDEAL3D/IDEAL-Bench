#!/usr/bin/env python3
"""
补登记 SCENES_RAW/manifests：用于 VPN 中断等导致 generate.py 未写完 manifest 时，
根据磁盘上已存在的场景目录追加 generation_batches.jsonl 与对应房型 .csv。

用法（在 IDL3D 仓库根目录或 generation/ 下均可，脚本会定位仓库根）:

  python generation/register_scenes_raw_manifest.py \\
    --room-type meeting-room --start-seed 1124442331 --count 5

会先检查 INFINIGEN/SCENES_RAW/<房型前缀>_s<seed>/ 是否存在；缺目录则报错退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate as gen  # noqa: E402
from infinigen_paths import SCENES_RAW_ROOT  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(
        description="根据已有场景目录补写 SCENES_RAW/manifests（generation_batches.jsonl + 房型 csv）"
    )
    p.add_argument(
        "--room-type",
        required=True,
        choices=list(gen.ROOM_TYPES_UNIFIED),
        help="房型（与 generate.py 一致）",
    )
    p.add_argument("--start-seed", type=int, required=True, help="该批次起始 seed")
    p.add_argument(
        "--count",
        type=int,
        required=True,
        help="连续 seed 个数（scene_id = <房型前缀>_s{start+ i}）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将登记的内容，不写文件",
    )
    args = p.parse_args()

    if args.count < 1:
        print("error: --count 须为正整数", file=sys.stderr)
        sys.exit(1)

    folder = gen._room_folder_name(args.room_type)
    scenes_root = SCENES_RAW_ROOT
    scene_ids: list[str] = []
    missing: list[str] = []
    for i in range(args.count):
        seed = args.start_seed + i
        sid = f"{folder}_s{seed}"
        scene_ids.append(sid)
        pdir = scenes_root / sid
        if not pdir.is_dir():
            missing.append(str(pdir))

    if missing:
        print("error: 以下目录不存在，请确认路径或 seed：", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("将登记 batch（单条兼容格式）+ CSV 行：")
        for sid in scene_ids:
            print(f"  {sid}")
        return

    gen._append_scenes_raw_manifest(
        args.room_type,
        args.count,
        args.start_seed,
        scene_ids,
        argv_for_record=sys.argv.copy(),
    )
    print("[register_scenes_raw_manifest.py] 完成。")


if __name__ == "__main__":
    main()
