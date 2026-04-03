#!/usr/bin/env python3
"""
将旧版 ``runs/{run_id}/inference|reconstruction/`` 迁入仓库根 ``result/``（场景目录下扁平）。

- ``.../inference/<protocol>/<model_id>/<scene_id>/prediction.json``
  → ``result/<protocol>/<scene_id>/<model_id>.json``
- ``.../reconstruction/<protocol>/<model_id>_<scene_id>.png``
  → ``result/<protocol>/<scene_id>/<model_id>.png``
- ``*_reconstructed.blend`` → ``result/<protocol>/<scene_id>/<model_id>_reconstructed.blend``

同一路径已存在时保留 **mtime 更新** 的文件。完成后可删除 ``runs/``（由调用方决定）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def collect_scene_ids(scenes_root: Path) -> list[str]:
    if not scenes_root.is_dir():
        return []
    return sorted(
        p.name for p in scenes_root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def split_model_scene(
    basename: str, scene_ids: list[str]
) -> tuple[str, str] | None:
    """
    文件名形如 ``{model_id}_{scene_id}.png`` 或 ``{model_id}_{scene_id}_reconstructed.blend``。
    scene_id 与 scenes2test 下目录名对齐；按最长匹配 scene_id。
    """
    if basename.endswith("_reconstructed.blend"):
        core = basename[: -len("_reconstructed.blend")]
    elif basename.endswith(".png"):
        core = basename[: -len(".png")]
    else:
        return None
    for sid in sorted(scene_ids, key=len, reverse=True):
        suf = f"_{sid}"
        if core.endswith(suf):
            return core[: -len(suf)], sid
    return None


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
    ap = argparse.ArgumentParser(description="runs/ → result/（仓库根扁平）迁移")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的操作，不写文件",
    )
    ap.add_argument(
        "--remove-runs",
        action="store_true",
        help="迁移成功后删除整个 runs/ 目录",
    )
    args = ap.parse_args()

    root = project_dir()
    runs = root / "runs"
    result = root / "result"
    scenes_root = root / "data" / "scenes2test"
    scene_ids = collect_scene_ids(scenes_root)

    if not runs.is_dir():
        print(f"未找到 {runs}，无需迁移。", file=sys.stderr)
        return 0

    n_pred = n_img = n_blend = 0

    # prediction.json
    for pred in sorted(runs.rglob("prediction.json")):
        parts = pred.parts
        if "inference" not in parts:
            continue
        i = parts.index("inference")
        if i < 1 or i + 3 >= len(parts):
            continue
        protocol = parts[i + 1]
        model_id = parts[i + 2]
        scene_id = parts[i + 3]
        dest = result / protocol / scene_id / f"{model_id}.json"
        if args.dry_run:
            print(f"[dry-run] {pred} -> {dest}")
        else:
            act = copy_if_newer(pred, dest)
            print(f"{act}: {dest}")
        n_pred += 1

    # png / blend under reconstruction/
    for f in sorted(runs.rglob("*")):
        if not f.is_file():
            continue
        if "reconstruction" not in f.parts:
            continue
        if f.name.endswith(".png"):
            sp = split_model_scene(f.name, scene_ids)
            if not sp:
                print(f"⚠️ 无法解析场景名，跳过: {f}", file=sys.stderr)
                continue
            mid, sid = sp
            proto_dir = f.parent
            protocol = proto_dir.name
            dest = result / protocol / sid / f"{mid}.png"
            if args.dry_run:
                print(f"[dry-run] {f} -> {dest}")
            else:
                act = copy_if_newer(f, dest)
                print(f"{act}: {dest}")
            n_img += 1
        elif f.name.endswith("_reconstructed.blend"):
            sp = split_model_scene(f.name, scene_ids)
            if not sp:
                print(f"⚠️ 无法解析场景名，跳过: {f}", file=sys.stderr)
                continue
            mid, sid = sp
            protocol = f.parent.name
            dest = result / protocol / sid / f"{mid}_reconstructed.blend"
            if args.dry_run:
                print(f"[dry-run] {f} -> {dest}")
            else:
                act = copy_if_newer(f, dest)
                print(f"{act}: {dest}")
            n_blend += 1

    print(
        f"完成: prediction.json={n_pred}, png={n_img}, blend={n_blend} -> {result}",
        file=sys.stderr,
    )

    if args.remove_runs and not args.dry_run:
        shutil.rmtree(runs)
        print(f"已删除 {runs}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
