#!/usr/bin/env python3
"""
将 ``inference/result/<protocol>/<scene>/{json,image,blend}/`` 迁入仓库根 ``result/<protocol>/<scene>/``，
文件平铺在场景目录下（``{model_id}.json``、``.png``、``_reconstructed.blend``），并删除空的 ``inference/result/``。

若已存在扁平 ``result/``，只合并迁入，同名文件保留 **mtime 更新** 者。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def copy_if_newer(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
        return "copy"
    if src.stat().st_mtime > dest.stat().st_mtime:
        shutil.copy2(src, dest)
        return "overwrite"
    return "skip"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="inference/result/ 嵌套布局 → 根目录 result/ 扁平布局"
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--remove-source",
        action="store_true",
        help="迁移成功后删除 inference/result/",
    )
    args = ap.parse_args()

    root = project_dir()
    nested = root / "inference" / "result"
    flat = root / "result"

    if not nested.is_dir():
        print(f"未找到 {nested}，无需迁移。", file=sys.stderr)
        return 0

    n = 0
    for proto_dir in sorted(nested.iterdir()):
        if not proto_dir.is_dir():
            continue
        protocol = proto_dir.name
        for scene_dir in sorted(proto_dir.iterdir()):
            if not scene_dir.is_dir():
                continue
            scene_id = scene_dir.name
            dest_scene = flat / protocol / scene_id

            for sub, pattern in (
                ("json", "*.json"),
                ("image", "*.png"),
                ("blend", "*_reconstructed.blend"),
            ):
                subdir = scene_dir / sub
                if not subdir.is_dir():
                    continue
                for f in sorted(subdir.glob(pattern)):
                    dest = dest_scene / f.name
                    if args.dry_run:
                        print(f"[dry-run] {f} -> {dest}")
                    else:
                        act = copy_if_newer(f, dest)
                        print(f"{act}: {dest}")
                    n += 1

    print(f"完成: {n} 个文件 -> {flat}", file=sys.stderr)

    if args.remove_source and not args.dry_run:
        shutil.rmtree(nested)
        print(f"已删除 {nested}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
