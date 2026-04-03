#!/usr/bin/env python
"""
对已生成但未正确收尾的场景目录：补写 metadata.json，并删除除四文件外的所有内容。

四文件：scene.blend, color.png, instance_segmentation.png, metadata.json

示例::

    cd /path/to/IDL3D/generation
    python generate_lib/finalize_existing_scene.py /path/to/INFINIGEN/SCENES_SKETCH/classroom_s717851252

目录名须为 ``{roomtype}_s{seed}``（与 generate_box 一致）；``object_index_map`` 若存在
``.gt_object_index_pass/Objects*.json`` 则从其中读取，否则为 ``{}``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_LIB = Path(__file__).resolve().parent
for _p in (_REPO_ROOT, _GEN_LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from generate_box import (  # noqa: E402
    _delete_all_except_final_four,
    _object_index_map_from_objects_json,
    build_instance_map_block,
    resolve_render_metadata,
)

# 目录名无连字符 → generate_box 使用的 --room-type（与 metadata.room_type 一致）
_SLUG_TO_ROOM_TYPE = {
    "livingroom": "living-room",
    "diningroom": "dining-room",
    "openoffice": "open-office",
    "meetingroom": "meeting-room",
}


def _parse_scene_dir(name: str) -> tuple[str, int]:
    m = re.match(r"^(.+)_s(\d+)$", name)
    if not m:
        raise ValueError(f"目录名应为 {{roomtype}}_s{{seed}}，当前: {name!r}")
    slug = m.group(1)
    seed = int(m.group(2))
    room_type = _SLUG_TO_ROOM_TYPE.get(slug, slug)
    return room_type, seed


def main() -> None:
    p = argparse.ArgumentParser(description="补写 metadata.json 并清理场景目录至四文件")
    p.add_argument("scene_dir", type=Path, help="场景目录路径")
    p.add_argument(
        "--render-width",
        type=int,
        choices=(1280, 640),
        default=1280,
        help="与生成时一致；仅用于无 camview npz 时的占位 metadata",
    )
    args = p.parse_args()
    scene_dir = args.scene_dir.resolve()
    if not scene_dir.is_dir():
        raise SystemExit(f"不是目录: {scene_dir}")

    for fn in ("scene.blend", "color.png", "instance_segmentation.png"):
        if not (scene_dir / fn).is_file():
            raise SystemExit(f"缺少 {fn}: {scene_dir}")

    room_type, _seed = _parse_scene_dir(scene_dir.name)
    blend = scene_dir / "scene.blend"

    ns = SimpleNamespace(render_width=args.render_width)
    render_meta = resolve_render_metadata(blend, scene_dir, ns)

    index_map: dict[str, str] = {}
    gt = scene_dir / ".gt_object_index_pass"
    if gt.is_dir():
        objs = sorted(gt.glob("Objects*.json"))
        if objs:
            tree = json.loads(objs[0].read_text(encoding="utf-8"))
            index_map = _object_index_map_from_objects_json(tree)

    metadata = {
        "scene_id": scene_dir.name,
        "room_type": room_type,
        "render": render_meta,
        "instance_map": build_instance_map_block(index_map),
    }
    (scene_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写入 {scene_dir / 'metadata.json'}")

    _delete_all_except_final_four(scene_dir)
    tmp = scene_dir.parent / "tmp"
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print(f"已清理，仅保留四文件: {scene_dir}")


if __name__ == "__main__":
    main()
