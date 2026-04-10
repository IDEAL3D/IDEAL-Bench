#!/usr/bin/env python3
"""
物体匹配：以 ``objects_in_view.json``（或旧 ``info.json``）的 ``objects`` 为底稿，按 **category 队列** 将预测与槽位一一对应；
多报则追加 ``placeholder_*``，未占用槽位将几何置 ``null``。

不包含：将预测几何写回物体、与 ``GT.json`` 合并、写出 ``{model_id}_render.json``（见 ``compose.py``）。
"""

from __future__ import annotations

import copy
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

# 模型多报 / 幻觉
PLACEHOLDER_CATEGORY = "placeholder"


def _placeholder_id(seq: int) -> str:
    if seq < 100:
        return f"placeholder_{seq:02d}"
    return f"placeholder_{seq}"


def _nullify_geometry(obj: dict[str, Any]) -> None:
    obj["position"] = None
    obj["dimensions"] = None
    obj["rotation_yaw"] = None


def _placeholder_entry_from_prediction(pred: dict[str, Any], seq: int) -> dict[str, Any]:
    out = copy.deepcopy(pred)
    out["id"] = _placeholder_id(seq)
    out["category"] = PLACEHOLDER_CATEGORY
    return out


def cache_from_info_objects(info_path: Path) -> dict[str, Any]:
    """从侧车 JSON 构造含 ``scene.objects`` 的工作层（深拷贝）。"""
    with open(info_path, encoding="utf-8") as f:
        info: dict[str, Any] = json.load(f)
    objs = info.get("objects")
    if not isinstance(objs, list):
        raise ValueError(f"{info_path} 缺少非空 list 字段 objects")
    return {
        "scene_id": info.get("scene_id"),
        "scene": {
            "objects": copy.deepcopy(objs),
        },
    }


def match_category_queue(
    info_layer: dict[str, Any], pred_scene: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    按类别队列将预测与 ``info_layer`` 中物体配对。

    **原地修改** ``info_layer``：追加占位物体、对未匹配槽位 ``_nullify_geometry``。

    返回 ``(prediction_object, cache_object)`` 列表，仅含**成功对上底稿槽位**的配对
    （占位行不在此列表中；占位条目已含预测深拷贝，由 ``compose`` 侧按需处理）。

    调用方应在返回后对 ``pairs`` 中每一对执行几何写回（``compose.apply_prediction_overlays``）。
    """
    pred_objs = pred_scene.get("objects") or []
    cache_objs = info_layer.get("scene", {}).get("objects") or []
    if not isinstance(pred_objs, list) or not isinstance(cache_objs, list):
        raise ValueError("scene.objects 须为列表")

    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for obj in cache_objs:
        if not isinstance(obj, dict):
            continue
        cat = obj.get("category")
        if cat:
            queues[str(cat)].append(obj)

    matched: set[int] = set()
    placeholder_seq = 0
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for idx, p in enumerate(pred_objs):
        if not isinstance(p, dict):
            continue
        cat = p.get("category")
        if not cat:
            raise ValueError(f"预测第 {idx} 个物体缺少 category")
        cat = str(cat)
        if queues[cat]:
            c_obj = queues[cat].popleft()
            pairs.append((p, c_obj))
            matched.add(id(c_obj))
        else:
            placeholder_seq += 1
            ph = _placeholder_entry_from_prediction(p, placeholder_seq)
            cache_objs.append(ph)
            matched.add(id(ph))
            print(
                f"[match] 无底稿槽位，追加占位: {ph['id']} (原预测 category={cat!r} id={p.get('id')!r})",
                file=sys.stderr,
            )

    for obj in cache_objs:
        if isinstance(obj, dict) and id(obj) not in matched:
            _nullify_geometry(obj)

    return pairs
