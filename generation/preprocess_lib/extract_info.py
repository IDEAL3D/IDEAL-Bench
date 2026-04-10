"""
Build and write ``objects_in_view.json`` (including furniture overlap summary fields); does not emit a standalone ``furniture_overlap.json``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from .defaults import (
    FURNITURE_OVERLAP_JSON,
    FURNITURE_OVERLAP_PAIR_COUNT_KEY,
    FURNITURE_OVERLAP_VOLUME_TOTAL_KEY,
    OBJECTS_IN_VIEW_JSON,
)
from .extract_camview import build_instance_seg_view_payload, scene_id_from_output_dir
from .overlap_check import compute_furniture_overlap


def build_objects_in_view_only_payload(*, scene_id: str, blend_dir: str) -> Dict[str, Any]:
    base = build_instance_seg_view_payload(scene_id, blend_dir)
    return {
        "scene_id": base["scene_id"],
        "categories_in_view_count": base["categories_in_view_count"],
        "categories_in_view": base["categories_in_view"],
        "objects_in_view_count": base["objects_in_view_count"],
        "objects": base["objects"],
    }


def write_info_json_v2(blend_dir: str) -> Tuple[str, Optional[int]]:
    """Write ``objects_in_view.json`` (overlap pair_count / volume summary); remove legacy ``furniture_overlap.json`` if present."""
    sid = scene_id_from_output_dir(blend_dir)
    payload = build_objects_in_view_only_payload(scene_id=sid, blend_dir=blend_dir)

    fo = compute_furniture_overlap()
    n_pairs: Optional[int] = None
    if isinstance(fo, dict) and fo.get("computed") is True:
        n_pairs = int(fo.get("pair_count", 0))
        payload[FURNITURE_OVERLAP_PAIR_COUNT_KEY] = n_pairs
        vol_total = fo.get("overlap_volume_m3_total")
        if vol_total is not None:
            payload[FURNITURE_OVERLAP_VOLUME_TOTAL_KEY] = vol_total
    else:
        payload[FURNITURE_OVERLAP_PAIR_COUNT_KEY] = 0

    path_ovie = os.path.join(blend_dir, OBJECTS_IN_VIEW_JSON)
    with open(path_ovie, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    path_fo = os.path.join(blend_dir, FURNITURE_OVERLAP_JSON)
    try:
        if os.path.isfile(path_fo):
            os.unlink(path_fo)
    except OSError:
        pass

    print(
        f"[extract_info] wrote {path_ovie}: categories_in_view_count="
        f"{payload.get('categories_in_view_count', '?')}, "
        f"objects_in_view_count={payload.get('objects_in_view_count', '?')}, "
        f"len(objects)={len(payload.get('objects') or [])}, "
        f"furniture_overlap pairs={n_pairs if n_pairs is not None else 0}"
    )
    return path_ovie, n_pairs


write_info_json = write_info_json_v2
