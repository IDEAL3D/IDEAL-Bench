"""
组装并写出 ``info.json``（v2：无 ``image``；字段顺序与语义以本模块为准）。

- **镜头 / 视锥内资产、内外参、与 GT 对齐的 ``objects``**：由 ``extract_camview`` 提供
  （``build_camview_payload_v2`` 等）。
- **家具体素重叠**：由 ``overlap_check.compute_furniture_overlap`` 提供。

后续若有其它 ``info`` 块，在本模块合并进 ``build_info_payload_v2`` / ``write_info_json_v2`` 即可。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from .defaults import INFO_JSON_FILENAME
from .extract_camview import build_camview_payload_v2, scene_id_from_output_dir
from .overlap_check import compute_furniture_overlap


def build_info_payload_v2(
    cam_obj,
    blend_dir: str,
    compute_overlap: bool = True,
    *,
    scene_id: str,
) -> Dict[str, Any]:
    """合并各子模块结果，得到完整 ``info.json`` 字典。"""
    base = build_camview_payload_v2(cam_obj, scene_id, blend_dir)
    if compute_overlap:
        fo = compute_furniture_overlap()
    else:
        fo = {
            "computed": False,
            "reason": "disabled (--no-overlap)",
        }
    return {
        "scene_id": base["scene_id"],
        "categories_in_view": base["categories_in_view"],
        "asset_count_in_view": base["asset_count_in_view"],
        "objects": base["objects"],
        "furniture_overlap": fo,
        "intrinsic": base["intrinsic"],
        "extrinsic": base["extrinsic"],
        "blender": base["blender"],
    }


def write_info_json_v2(
    blend_dir: str, cam_obj, compute_overlap: bool = True
) -> Tuple[str, Optional[int]]:
    """写出 v2 版 ``info.json``；返回 ``(路径, pair_count)``。"""
    path = os.path.join(blend_dir, INFO_JSON_FILENAME)
    sid = scene_id_from_output_dir(blend_dir)
    payload = build_info_payload_v2(
        cam_obj,
        blend_dir,
        compute_overlap=compute_overlap,
        scene_id=sid,
    )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    fo = payload.get("furniture_overlap") or {}
    n_pairs: Optional[int] = None
    if isinstance(fo, dict) and fo.get("computed") is True:
        n_pairs = int(fo.get("pair_count", 0))
    print(
        f"[extract_info] wrote {path}: {len(payload['categories_in_view'])} categories_in_view, "
        f"asset_count_in_view={payload.get('asset_count_in_view', '?')}, "
        f"objects={len(payload.get('objects') or [])}, "
        f"furniture_overlap pairs={n_pairs if n_pairs is not None else 'n/a'}"
    )
    return path, n_pairs


write_info_json = write_info_json_v2
