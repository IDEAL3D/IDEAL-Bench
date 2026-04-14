#!/usr/bin/env python3
"""
统计 ``instance_segmentation.png`` 中具体物体所占像素数，用于定义最低可见像素阈值。

用法::
    cd /path/to/IDEAL
    python '[INTERNAL]tools/check_instance_pixel.py' /path/to/scene_dir
    python '[INTERNAL]tools/check_instance_pixel.py' /path/to/scene_dir --substr singlecabinet
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_TOOLS = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS.parent
_GENERATION = _REPO_ROOT / "generation"
for _p in (_REPO_ROOT, _GENERATION):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from preprocess_lib.defaults import (  # noqa: E402
    INSTANCE_SEGMENTATION_PNG,
    METADATA_JSON,
)
from preprocess_lib.build_inview import (  # noqa: E402
    MissingInViewInfoError,
    _load_instance_segmentation_u16,
    _load_object_index_map,
)


def _pixel_counts_by_index(seg_path: str) -> Dict[int, int]:
    arr = _load_instance_segmentation_u16(seg_path)
    vals, counts = np.unique(arr.ravel(), return_counts=True)
    out: Dict[int, int] = {}
    for v, c in zip(vals.tolist(), counts.tolist()):
        vi = int(v)
        if vi <= 0:
            continue
        out[vi] = int(c)
    return out


def report_scene(
    scene_dir: str,
    *,
    substr: Optional[str] = None,
    min_pixels: int = 0,
) -> List[Tuple[int, int, str]]:
    """返回 [(pass_index, pixels, object_name), ...] 按像素降序。"""
    scene_dir = os.path.abspath(os.path.expanduser(scene_dir))
    seg_path = os.path.join(scene_dir, INSTANCE_SEGMENTATION_PNG)
    if not os.path.isfile(seg_path):
        raise FileNotFoundError(f"缺少 {INSTANCE_SEGMENTATION_PNG}: {seg_path}")

    object_index_map = _load_object_index_map(scene_dir)
    by_idx = _pixel_counts_by_index(seg_path)

    rows: List[Tuple[int, int, str]] = []
    for pi in sorted(by_idx.keys()):
        px = by_idx[pi]
        if px < min_pixels:
            continue
        name = object_index_map.get(str(pi), "(不在 object_index_map 中)")
        if substr is not None and substr.lower() not in name.lower():
            continue
        rows.append((pi, px, name))

    rows.sort(key=lambda t: -t[1])
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="统计 instance_segmentation.png 各 pass_index 像素数（对 metadata object_index_map）"
    )
    parser.add_argument(
        "scene_dir",
        type=str,
        help=f"场景目录（含 {METADATA_JSON}、{INSTANCE_SEGMENTATION_PNG}）",
    )
    parser.add_argument(
        "--substr",
        type=str,
        default=None,
        help="只显示 object_index_map 名字含有该子串的项（忽略大小写）",
    )
    parser.add_argument(
        "--min-pixels",
        type=int,
        default=0,
        help="至少像素数（默认 0，即列出全部非零 id）",
    )
    args = parser.parse_args(argv)

    try:
        rows = report_scene(
            args.scene_dir, substr=args.substr, min_pixels=args.min_pixels
        )
    except (FileNotFoundError, MissingInViewInfoError) as e:
        print(f"[check_instance_pixel] ERROR: {e}", file=sys.stderr)
        return 1

    h, w = 0, 0
    try:
        arr = _load_instance_segmentation_u16(
            os.path.join(os.path.abspath(args.scene_dir), INSTANCE_SEGMENTATION_PNG)
        )
        h, w = int(arr.shape[0]), int(arr.shape[1])
    except Exception:
        pass

    print(f"scene: {os.path.abspath(args.scene_dir)}")
    if w and h:
        suffix = "（已按筛选条件过滤）" if (args.substr or args.min_pixels > 0) else ""
        print(f"seg: {w}x{h}, 列出 {len(rows)} 行{suffix}")
    print(f"{'idx':>6}  {'pixels':>8}  name")
    for pi, px, name in rows:
        print(f"{pi:>6}  {px:>8}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
