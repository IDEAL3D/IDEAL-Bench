#!/usr/bin/env python3
"""
组合渲染载荷：在 ``match.py`` 完成类别队列配对之后，将预测几何写回工作层、
与完整 ``GT.json`` 按 id 合并，写出 ``{model_id}_render.json``（pseudo-GT，供 ``render.py``）。

命名说明：**compose** 表示将「镜头内匹配层 + 全文 GT」组合成单一可渲染 JSON，与图形/编译管线中
*compose*（合成最终产物）的用法一致。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

try:
    from reconstruction import match as _match
except ImportError:
    import match as _match


WALL_MOUNTED_CATEGORIES = frozenset(
    {
        "mirror",
        "wall_art",
    }
)


def merge_gt_with_info_layer(gt: dict[str, Any], info_layer: dict[str, Any]) -> dict[str, Any]:
    """以 GT 为全文基底；用 info_layer 的 ``scene.objects`` 按 id 覆盖或追加。"""
    out = copy.deepcopy(gt)
    scene = out.setdefault("scene", {})
    room = scene.setdefault("room", {})
    gt_objs: list[dict[str, Any]] = scene.setdefault("objects", [])
    if not isinstance(gt_objs, list):
        raise ValueError("GT scene.objects 须为 list")

    id_to_index: dict[str, int] = {}
    for i, o in enumerate(gt_objs):
        if isinstance(o, dict) and o.get("id") is not None:
            id_to_index[str(o["id"])] = i

    layer_objs = (info_layer.get("scene") or {}).get("objects")
    if layer_objs is None:
        layer_objs = []
    if not isinstance(layer_objs, list):
        raise ValueError("info_layer scene.objects 须为 list")

    for co in layer_objs:
        if not isinstance(co, dict):
            continue
        oid = co.get("id")
        if oid is None:
            continue
        oid = str(oid)
        if oid in id_to_index:
            gt_objs[id_to_index[oid]] = copy.deepcopy(co)
        else:
            gt_objs.append(copy.deepcopy(co))
            id_to_index[oid] = len(gt_objs) - 1

    room["asset_count"] = len(gt_objs)
    return out


def apply_object_overlay(cache_obj: dict[str, Any], pred_obj: dict[str, Any]) -> None:
    """将单条预测的几何语义写到底稿物体上。"""
    cat = (cache_obj.get("category") or pred_obj.get("category") or "").strip().lower()
    wall_mounted = cat in WALL_MOUNTED_CATEGORIES

    pp = pred_obj.get("position")
    cp = cache_obj.get("position")
    if not isinstance(pp, list) or len(pp) < 2:
        raise ValueError(
            f"预测物体 position 需要至少 x,y: id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    if wall_mounted:
        if len(pp) < 3:
            raise ValueError(
                f"墙上类资产需要预测 position 含 z（3 个数）: id={pred_obj.get('id')!r} category={cat!r}"
            )
        cache_obj["position"] = [float(pp[0]), float(pp[1]), float(pp[2])]
    else:
        z_keep = cp[2] if isinstance(cp, list) and len(cp) >= 3 else 0.0
        cache_obj["position"] = [float(pp[0]), float(pp[1]), float(z_keep)]

    dim = pred_obj.get("dimensions")
    if not isinstance(dim, list) or len(dim) < 3:
        raise ValueError(
            f"预测物体 dimensions 需要三个数: id={pred_obj.get('id')!r} category={pred_obj.get('category')!r}"
        )
    cache_obj["dimensions"] = [float(dim[0]), float(dim[1]), float(dim[2])]

    if "rotation_yaw" in pred_obj:
        cache_obj["rotation_yaw"] = pred_obj["rotation_yaw"]


def apply_prediction_overlays(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """对 ``match_category_queue`` 返回的配对逐条写回几何。"""
    for pred_obj, cache_obj in pairs:
        apply_object_overlay(cache_obj, pred_obj)


def run_compose(
    info_path: Path,
    gt_path: Path,
    prediction_path: Path,
    render_out: Path,
) -> None:
    info_path = info_path.resolve()
    gt_path = gt_path.resolve()
    prediction_path = prediction_path.resolve()
    render_out = render_out.resolve()

    info_layer = _match.cache_from_info_objects(info_path)
    with open(prediction_path, encoding="utf-8") as f:
        pred: dict[str, Any] = json.load(f)
    with open(gt_path, encoding="utf-8") as f:
        gt_data: dict[str, Any] = json.load(f)

    pairs = _match.match_category_queue(info_layer, pred.get("scene") or {})
    apply_prediction_overlays(pairs)
    pseudo = merge_gt_with_info_layer(gt_data, info_layer)

    render_out.parent.mkdir(parents=True, exist_ok=True)
    with open(render_out, "w", encoding="utf-8") as f:
        json.dump(pseudo, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IDL3D：match → 写回几何 → 合并 GT → {model_id}_render.json"
    )
    p.add_argument("--blend", type=Path, required=True, help="场景 scene.blend（预留）")
    p.add_argument("--prediction", type=Path, required=True, help="VLM 预测 JSON")
    p.add_argument(
        "--info-json",
        type=Path,
        required=True,
        help="data/.../info.json；从其 objects 字段作匹配底稿",
    )
    p.add_argument(
        "--gt-json",
        type=Path,
        required=True,
        help="data/.../GT.json；全文合并后写出 {model_id}_render.json",
    )
    p.add_argument(
        "--protocol",
        required=True,
        choices=("floor", "camera"),
        help="floor / camera",
    )
    p.add_argument("--scene-id", required=True, help="场景目录名")
    p.add_argument("--model-id", required=True, help="预测文件名 stem；输出 {model_id}_render.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    pred = args.prediction.resolve()
    render_out = pred.parent / f"{args.model_id}_render.json"
    try:
        run_compose(args.info_json, args.gt_json, pred, render_out)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[compose] {e}", file=sys.stderr)
        return 1
    print(f"[compose] 已写入 {render_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
