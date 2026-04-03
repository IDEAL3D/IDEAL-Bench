#!/usr/bin/env python
"""
为 **旧版 / 未跑完收尾** 的场景目录补渲染 ``instance_segmentation.png`` 并写入 ``metadata.json``，
可选清理为与 ``SCENES_SKETCH`` 一致的四文件布局。

本脚本放在 ``generation/`` 根下：属**一次性数据修补**，不是 ``generate_lib`` 生成管线的一部分；
全部场景修完后可直接删除本文件。实现上仅调用 ``generate_lib.core._finalize_scene_outputs``，
无其它模块 import 本文件。

``core.py`` 正常全链路里，首趟渲染产出 ``Image_*.png``，收尾时改名为 ``color.png``。
老数据往往**已经只有** ``color.png``（没有 ``Image_*.png``），此时仍应以该彩图为准；
``core._ensure_color_png`` 已支持「有 color.png 则跳过改名」。

用法::

    cd /path/to/IDL3D/generation

    # 批量：``SCENES_RAW/<room_type>/`` 下所有 ``<room_type>_s<seed>/``
    python fix_existing_scene.py --room-type bedroom

    # 单个场景目录
    python fix_existing_scene.py /path/to/INFINIGEN/SCENES_RAW/bedroom/bedroom_s123

    # 只补文件、不删目录内其它内容（不执行四文件清理）
    python fix_existing_scene.py --room-type bedroom --no-clean

四文件：``scene.blend``, ``color.png``, ``instance_segmentation.png``, ``metadata.json``
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_GEN_LIB = _THIS_DIR / "generate_lib"
for _p in (_REPO_ROOT, _GEN_LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from configs.constant import ROOM_TYPES, infinigen_room_type  # noqa: E402
from configs.infinigen_paths import SCENES_RAW_ROOT  # noqa: E402
from core import (  # noqa: E402
    ROOM_CONFIG_MAP,
    _finalize_scene_outputs,
    get_render_gpu,
)


def _parse_scene_dir(name: str) -> tuple[str, int]:
    m = re.match(r"^(.+)_s(\d+)$", name)
    if not m:
        raise ValueError(f"目录名应为 {{canonical}}_s{{seed}}，当前: {name!r}")
    slug = m.group(1)
    seed = int(m.group(2))
    if slug not in ROOM_TYPES:
        raise ValueError(
            f"目录前缀 {slug!r} 须为 configs.constant.ROOM_TYPES 之一。"
        )
    return slug, seed


def _build_args_namespace(
    *,
    room_type: str,
    render_width: int,
) -> SimpleNamespace:
    inf = infinigen_room_type(room_type)
    return SimpleNamespace(
        configs=[ROOM_CONFIG_MAP[inf]],
        render_width=render_width,
        task_uniqname=None,
        debug=None,
    )


def _iter_scene_dirs(room_type: str, raw_root: Path) -> list[Path]:
    parent = (raw_root / room_type).resolve()
    if not parent.is_dir():
        raise SystemExit(f"不存在或未作为目录: {parent}")
    out: list[Path] = []
    prefix = f"{room_type}_s"
    for p in sorted(parent.iterdir()):
        if not p.is_dir():
            continue
        if not p.name.startswith(prefix):
            continue
        rest = p.name[len(prefix) :]
        if rest.isdigit():
            out.append(p)
    return out


def _process_one(
    scene_dir: Path,
    *,
    room_type: str,
    seed: int,
    render_width: int,
    gpu: str | None,
    clean_to_four: bool,
    force: bool,
    dry_run: bool,
) -> str:
    scene_dir = scene_dir.resolve()
    blend = scene_dir / "scene.blend"
    color = scene_dir / "color.png"
    inst = scene_dir / "instance_segmentation.png"
    meta = scene_dir / "metadata.json"

    if not blend.is_file():
        return f"[skip] 无 scene.blend: {scene_dir}"
    if not color.is_file():
        return f"[skip] 无 color.png: {scene_dir}"

    if not force and inst.is_file() and meta.is_file():
        return f"[skip] 已有 instance + metadata: {scene_dir}"

    if dry_run:
        return f"[dry-run] 将处理: {scene_dir}"

    ns = _build_args_namespace(room_type=room_type, render_width=render_width)
    _finalize_scene_outputs(
        scene_dir,
        seed,
        room_type,
        ns,
        gpu=gpu,
        clean_to_four=clean_to_four,
    )
    return f"[ok] {scene_dir}"


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "为 SCENES_RAW 下某房型批量（或单个路径）补 Object Index + metadata；"
            "默认与 generate 一致清理为四文件。"
        )
    )
    p.add_argument(
        "scene_dir",
        type=Path,
        nargs="?",
        default=None,
        help="可选：单个场景目录（例如 …/SCENES_RAW/bedroom/bedroom_s123）",
    )
    p.add_argument(
        "--room-type",
        type=str,
        choices=list(ROOM_TYPES),
        default=None,
        help="批量模式必填：房型 canonical（扫描 SCENES_RAW/<room_type>/）",
    )
    p.add_argument(
        "--scenes-raw-root",
        type=Path,
        default=None,
        help=f"RAW 根目录，默认 {SCENES_RAW_ROOT}",
    )
    p.add_argument(
        "--render-width",
        type=int,
        choices=(1280, 640),
        default=1280,
        help="须与生成时 RGB 分辨率一致（1280→1280×720，640→640×360）",
    )
    p.add_argument(
        "--gpu",
        type=str,
        default=None,
        help="CUDA_VISIBLE_DEVICES（默认 get_render_gpu()）",
    )
    p.add_argument(
        "--no-clean",
        action="store_true",
        help="不删除四文件以外的内容（不执行「仅保留四文件」清理）",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="即使已有 instance_segmentation.png 与 metadata.json 也重跑",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出将处理的目录，不调用 Blender/Infinigen",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="最多处理 N 个场景（调试用）",
    )
    args = p.parse_args()
    raw_root = (args.scenes_raw_root or SCENES_RAW_ROOT).resolve()

    gpu = args.gpu if args.gpu is not None else get_render_gpu()
    clean_to_four = not args.no_clean

    if args.scene_dir is not None:
        scene_dir = args.scene_dir.resolve()
        if not scene_dir.is_dir():
            raise SystemExit(f"不是目录: {scene_dir}")
        room_type, seed = _parse_scene_dir(scene_dir.name)
        msg = _process_one(
            scene_dir,
            room_type=room_type,
            seed=seed,
            render_width=args.render_width,
            gpu=gpu,
            clean_to_four=clean_to_four,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(msg)
        return

    if args.room_type is None:
        raise SystemExit("请指定单个场景目录作为位置参数，或使用 --room-type 做批量处理")

    dirs = _iter_scene_dirs(args.room_type, raw_root)
    if args.limit is not None:
        dirs = dirs[: max(0, args.limit)]

    print(f"RAW 根: {raw_root}，房型: {args.room_type}，共 {len(dirs)} 个场景目录", flush=True)
    for d in dirs:
        rt, sd = _parse_scene_dir(d.name)
        assert rt == args.room_type
        msg = _process_one(
            d,
            room_type=rt,
            seed=sd,
            render_width=args.render_width,
            gpu=gpu,
            clean_to_four=clean_to_four,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(msg, flush=True)


if __name__ == "__main__":
    main()
