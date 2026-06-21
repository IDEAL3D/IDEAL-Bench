"""Instance map + ``instance_segmentation.png`` → in-view object list (aligned with ``GT.json``)."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

_GENERATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

_REPO_ROOT = os.path.dirname(_GENERATION_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from generation_common.asset_rules import factory_to_category, scene_id_from_output_dir

from .defaults import (
    ASPECT_RATIO_MAX,
    GT_JSON_NAME,
    INSTANCE_SEGMENTATION_PNG,
    METADATA_JSON,
)

from configs.constants import MIN_VISIBLE_PIXELS, canonicalize_asset_category


class MissingInViewInfoError(RuntimeError):
    """Raised when in-view pipeline inputs are missing or metadata is unusable (e.g. GT, metadata.json, instance segmentation)."""


# ---------------------------------------------------------------------------
# Internal helpers — unchanged from original
# ---------------------------------------------------------------------------

def _load_instance_segmentation_u16(path: str):
    import numpy as np

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    arr = None
    err_chain: list[Exception] = []
    try:
        import imageio.v3 as iio

        arr = np.asarray(iio.imread(path))
    except Exception as e:
        err_chain.append(e)
        arr = None
    if arr is None:
        try:
            import cv2

            raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if raw is None:
                raise ValueError("cv2.imread returned None")
            arr = np.asarray(raw)
        except Exception as e:
            err_chain.append(e)
            arr = None
    if arr is None:
        try:
            from PIL import Image

            arr = np.array(Image.open(path))
        except Exception as e:
            err_chain.append(e)
            raise RuntimeError(
                f"Cannot read instance segmentation (expected uint16 / integer): {path}; tried imageio, cv2.IMREAD_UNCHANGED, PIL"
            ) from (err_chain[0] if err_chain else None)
    if arr.ndim == 3:
        arr = arr[:, :, 0]
    if not np.issubdtype(arr.dtype, np.integer):
        raise TypeError(
            f"{path}: instance segmentation must be an integer ID map; got dtype={arr.dtype}"
        )
    return arr


def _load_object_index_map(blend_dir: str) -> Dict[str, str]:
    path = os.path.join(blend_dir, METADATA_JSON)
    if not os.path.isfile(path):
        raise MissingInViewInfoError(
            f"Missing {METADATA_JSON} (objects_in_view requires instance_map.object_index_map): {path}"
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MissingInViewInfoError(
            f"Cannot parse {path}: {e}"
        ) from e
    inst = data.get("instance_map")
    if not isinstance(inst, dict):
        raise MissingInViewInfoError(
            f"{path}: missing or invalid instance_map (expected object)"
        )
    oim = inst.get("object_index_map")
    if not isinstance(oim, dict) or len(oim) == 0:
        raise MissingInViewInfoError(
            f"{path}: instance_map.object_index_map must be a non-empty dict; got {oim!r}"
        )
    return {str(k): str(v) for k, v in oim.items()}


def _load_render_size(blend_dir: str) -> Tuple[int, int]:
    """Return (width, height) from metadata.json render.resolution, fallback (1280, 720)."""
    path = os.path.join(blend_dir, METADATA_JSON)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        res = data.get("render", {}).get("resolution")
        if isinstance(res, (list, tuple)) and len(res) == 2:
            return int(res[0]), int(res[1])
    except Exception:
        pass
    return 1280, 720


def _gt_id_to_metadata_base(gtid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(gtid)).strip("_").lower() or "object"


def _metadata_label_to_base(label: str) -> str:
    m = re.match(r"^(.+)_(\d{3})$", label)
    if m:
        return m.group(1)
    return label


def _build_metadata_base_to_gt_id(blend_dir: str) -> Dict[str, str]:
    path = os.path.join(blend_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        raise MissingInViewInfoError(
            f"Missing {GT_JSON_NAME}; cannot align metadata object names to GT ids: {path}"
        )
    with open(path, encoding="utf-8") as f:
        gt_data = json.load(f)
    objs = gt_data.get("scene", {}).get("objects")
    if not isinstance(objs, list):
        return {}
    base_to_id: Dict[str, str] = {}
    for o in objs:
        if not isinstance(o, dict) or o.get("id") is None:
            continue
        gid = str(o["id"])
        b = _gt_id_to_metadata_base(gid)
        if b in base_to_id and base_to_id[b] != gid:
            print(
                f"[build_inview] WARN: metadata base collision {b!r} -> "
                f"keeping {base_to_id[b]!r}, ignoring {gid!r}"
            )
        elif b not in base_to_id:
            base_to_id[b] = gid

    # MattressFactory shares seed/spawn_id with SingleBedFactory but has no GT entry.
    # Register mattressfactory_xxx base → SingleBedFactory GT id so mattress pixels
    # are merged into the bed's actual_pixels during instance map resolution.
    extra: Dict[str, str] = {}
    for base, gid in base_to_id.items():
        if base.startswith("singlebedfactory"):
            mattress_base = re.sub(r"^singlebedfactory", "mattressfactory", base)
            if mattress_base not in base_to_id:
                extra[mattress_base] = gid
    base_to_id.update(extra)

    return base_to_id


def _object_index_counts_from_png(seg_path: str) -> Dict[int, int]:
    import numpy as np

    arr = _load_instance_segmentation_u16(seg_path)
    vals, counts = np.unique(arr.ravel(), return_counts=True)
    out: Dict[int, int] = {}
    for v, c in zip(vals.tolist(), counts.tolist()):
        vi = int(v)
        if vi <= 0:
            continue
        out[vi] = int(c)
    return out


def _id_and_category_from_gt(
    blend_dir: str, gt_ids_in_order: List[str]
) -> List[Dict[str, Any]]:
    """Return minimal object stubs [{id, category}] aligned to GT, without 3D fields."""
    path = os.path.join(blend_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        print(f"[build_inview] WARN: missing {GT_JSON_NAME}, objects=[]")
        return []
    try:
        with open(path, encoding="utf-8") as f:
            gt_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[build_inview] WARN: could not read GT: {e}")
        return []
    objs = gt_data.get("scene", {}).get("objects")
    if not isinstance(objs, list):
        return []
    id_to_category = {
        str(o.get("id")): canonicalize_asset_category(str(o["category"]))
        for o in objs
        if isinstance(o, dict) and o.get("id") is not None and o.get("category") is not None
    }

    out: List[Dict[str, Any]] = []
    for gid in gt_ids_in_order:
        cat = id_to_category.get(gid)
        if cat is None:
            print(f"[build_inview] WARN: in-view id {gid!r} not found in GT, skipped")
            continue
        out.append({"id": gid, "category": cat})
    return out


def _objects_from_gt_for_names(
    blend_dir: str, root_names_in_order: List[str]
) -> List[Dict[str, Any]]:
    """Legacy full-row loader — used only by build_instance_seg_view_payload (filter_scenes.py)."""
    path = os.path.join(blend_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        print(f"[build_inview] WARN: missing {GT_JSON_NAME}, objects=[]")
        return []
    try:
        with open(path, encoding="utf-8") as f:
            gt_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[build_inview] WARN: could not read GT: {e}")
        return []
    objs = gt_data.get("scene", {}).get("objects")
    if not isinstance(objs, list):
        return []
    id_to_row = {
        str(o.get("id")): o for o in objs if isinstance(o, dict) and o.get("id") is not None
    }
    out: List[Dict[str, Any]] = []
    for key in root_names_in_order:
        row = id_to_row.get(key)
        if row is None:
            print(f"[build_inview] WARN: in-view root {key!r} has no GT id, skipped")
            continue
        out.append(copy.deepcopy(row))
    return out


# ---------------------------------------------------------------------------
# Original public API — NOT modified; filter_scenes.py still calls this
# ---------------------------------------------------------------------------

def build_instance_seg_view_payload(scene_id: str, blend_dir: str) -> Dict[str, Any]:
    """Build the same structure written to ``objects_in_view.json`` (categories + objects). Empty lists ⇒ no qualified in-view assets."""
    seg_path = os.path.join(blend_dir, INSTANCE_SEGMENTATION_PNG)
    if not os.path.isfile(seg_path):
        raise MissingInViewInfoError(
            f"Missing {INSTANCE_SEGMENTATION_PNG}; cannot build objects_in_view: {seg_path}"
        )

    object_index_map = _load_object_index_map(blend_dir)
    base_to_gt = _build_metadata_base_to_gt_id(blend_dir)
    counts = _object_index_counts_from_png(seg_path)

    visible_gt_ids: set[str] = set()
    for pi, pc in sorted(counts.items()):
        if pc < MIN_VISIBLE_PIXELS:
            continue
        key = str(int(pi))
        meta_name = object_index_map.get(key)
        if meta_name is None:
            print(
                f"[build_inview] WARN: pass_index {pi} ({pc}px) not in "
                f"object_index_map, skipping"
            )
            continue
        mb = _metadata_label_to_base(meta_name)
        gid = base_to_gt.get(mb)
        if gid is None:
            print(
                f"[build_inview] WARN: metadata object {meta_name!r} "
                f"(base {mb!r}) has no matching GT id, skipping"
            )
            continue
        visible_gt_ids.add(gid)

    names_ordered = sorted(visible_gt_ids)
    objects_out = _objects_from_gt_for_names(blend_dir, names_ordered)
    for o in objects_out:
        c = o.get("category")
        if c is not None:
            o["category"] = canonicalize_asset_category(str(c))
    categories = sorted(
        {str(o["category"]) for o in objects_out if o.get("category") is not None}
    )

    print(
        f"[build_inview] metadata + instance seg: {len(names_ordered)} GT objects in-view "
        f"(≥{MIN_VISIBLE_PIXELS}px, GT-aligned), "
        f"{len(categories)} categories"
    )
    for n in names_ordered:
        print(f"    • {factory_to_category(n):30s}  {n}")

    return {
        "scene_id": scene_id,
        "categories_in_view": categories,
        "categories_in_view_count": len(categories),
        "objects_in_view_count": len(objects_out),
        "objects": objects_out,
    }


# ---------------------------------------------------------------------------
# New: Filter 1 + Filter 2 with per-object diagnostics for inview.py
# ---------------------------------------------------------------------------

def _instance_map_bbox(arr, pass_index: int) -> Optional[Tuple[int, int, int, int]]:
    """Return (x_min, x_max, y_min, y_max) of pixels matching pass_index, or None if empty."""
    import numpy as np

    mask = arr == pass_index
    if not mask.any():
        return None
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return int(cols[0]), int(cols[-1]), int(rows[0]), int(rows[-1])


def _instance_map_centroid(arr, pass_index: int) -> Optional[Tuple[float, float]]:
    """Return (cx, cy) centroid of pixels matching pass_index, or None if empty."""
    import numpy as np

    rows, cols = np.where(arr == pass_index)
    if len(rows) == 0:
        return None
    return float(cols.mean()), float(rows.mean())


def _merge_mattress_pixels(
    gt_id_to_pixels: Dict[str, int],
    gt_id_to_pidx: Dict[str, int],
    arr,
    object_index_map: Dict[str, str],
    base_to_gt: Dict[str, str],
) -> None:
    """Merge MattressFactory pixel counts into their corresponding SingleBedFactory entry."""
    mattress_ids = [gid for gid in list(gt_id_to_pixels) if gid.startswith("MattressFactory")]
    for mattress_id in mattress_ids:
        bed_id = re.sub(r"^MattressFactory", "SingleBedFactory", mattress_id)
        if bed_id in gt_id_to_pixels:
            gt_id_to_pixels[bed_id] += gt_id_to_pixels.pop(mattress_id)
            gt_id_to_pidx.pop(mattress_id, None)
            print(
                f"[build_inview] merged mattress pixels into bed: "
                f"{mattress_id} → {bed_id} "
                f"(combined actual_pixels={gt_id_to_pixels[bed_id]})"
            )
        else:
            gt_id_to_pixels[bed_id] = gt_id_to_pixels.pop(mattress_id)
            gt_id_to_pidx[bed_id] = gt_id_to_pidx.pop(mattress_id)
            print(
                f"[build_inview] mattress visible but bed frame not — "
                f"remapped {mattress_id} → {bed_id}"
            )


def build_inview_with_filters(
    scene_id: str,
    blend_dir: str,
) -> Dict[str, Any]:
    """Filter 1 (visibility) + Filter 2 (aspect ratio) applied with per-object diagnostics."""
    import numpy as np

    seg_path = os.path.join(blend_dir, INSTANCE_SEGMENTATION_PNG)
    if not os.path.isfile(seg_path):
        raise MissingInViewInfoError(
            f"Missing {INSTANCE_SEGMENTATION_PNG}; cannot build objects_in_view: {seg_path}"
        )

    object_index_map = _load_object_index_map(blend_dir)
    base_to_gt = _build_metadata_base_to_gt_id(blend_dir)
    arr = _load_instance_segmentation_u16(seg_path)
    counts = _object_index_counts_from_png(seg_path)
    render_size = _load_render_size(blend_dir)

    gt_id_to_pixels: Dict[str, int] = {}
    gt_id_to_pidx: Dict[str, int] = {}

    for pi, pc in sorted(counts.items()):
        key = str(int(pi))
        meta_name = object_index_map.get(key)
        if meta_name is None:
            continue
        mb = _metadata_label_to_base(meta_name)
        gid = base_to_gt.get(mb)
        if gid is None:
            continue
        if gid not in gt_id_to_pixels or pc > gt_id_to_pixels[gid]:
            gt_id_to_pixels[gid] = pc
            gt_id_to_pidx[gid] = pi

    _merge_mattress_pixels(gt_id_to_pixels, gt_id_to_pidx, arr, object_index_map, base_to_gt)

    filter_1_filtered: List[Dict[str, Any]] = []
    after_f1: Dict[str, int] = {}

    for gid, pc in gt_id_to_pixels.items():
        if pc < MIN_VISIBLE_PIXELS:
            filter_1_filtered.append({"gt_id": gid, "actual_pixels": pc})
        else:
            after_f1[gid] = pc

    objects_inview_original = len(gt_id_to_pixels)

    filter_2_filtered: List[Dict[str, Any]] = []
    after_f2: Set[str] = set()
    gt_id_to_centroid: Dict[str, List[float]] = {}

    for gid, pc in after_f1.items():
        pidx = gt_id_to_pidx.get(gid)
        bbox = _instance_map_bbox(arr, pidx) if pidx is not None else None
        if bbox is None:
            after_f2.add(gid)
            render_w, render_h = render_size
            gt_id_to_centroid[gid] = [render_w / 2.0, render_h / 2.0]
            continue

        x_min, x_max, y_min, y_max = bbox
        dx = max(x_max - x_min, 1)
        dy = max(y_max - y_min, 1)
        ratio = max(dx / dy, dy / dx)
        if ratio > ASPECT_RATIO_MAX:
            filter_2_filtered.append({"gt_id": gid, "aspect_ratio": round(ratio, 2)})
            continue

        after_f2.add(gid)
        centroid = _instance_map_centroid(arr, pidx)
        if centroid is not None:
            gt_id_to_centroid[gid] = list(centroid)
        else:
            render_w, render_h = render_size
            gt_id_to_centroid[gid] = [render_w / 2.0, render_h / 2.0]

    kept_ids_ordered = sorted(after_f2)
    stubs = _id_and_category_from_gt(blend_dir, kept_ids_ordered)
    gt_id_to_stub = {s["id"]: s for s in stubs}

    # Build bed → mattress GT id mapping for F3 projected-pixels merging.
    # MattressFactory has no GT entry so we derive its expected GT id from the bed's.
    gt_id_to_mattress_gt_id: Dict[str, str] = {
        gid: re.sub(r"^SingleBedFactory", "MattressFactory", gid)
        for gid in kept_ids_ordered
        if gid.startswith("SingleBedFactory")
    }

    print(
        f"[build_inview] F1+F2: original={objects_inview_original} "
        f"f1_filtered={len(filter_1_filtered)} "
        f"f2_filtered={len(filter_2_filtered)} "
        f"remaining={len(after_f2)}"
    )

    return {
        "objects_inview_original": objects_inview_original,
        "kept_gt_ids": after_f2,
        "pixel_counts": {gid: after_f1[gid] for gid in after_f2},
        "centroids": gt_id_to_centroid,
        "render_size": list(render_size),
        "filter_1": filter_1_filtered,
        "filter_2": filter_2_filtered,
        "gt_id_to_stub": gt_id_to_stub,
        "kept_ids_ordered": kept_ids_ordered,
        "gt_id_to_mattress_gt_id": gt_id_to_mattress_gt_id,  # bed → mattress GT id
    }


# ---------------------------------------------------------------------------
# No-filter path — used when filter_inview routes a scene via NO_FILTER_ROOM_TYPES (classroom-only today)
# ---------------------------------------------------------------------------

def build_inview_no_filter(
    scene_id: str,
    blend_dir: str,
) -> Dict[str, Any]:
    """Return all GT-aligned objects with any pixels in the instance map, without F1/F2/F3.

    Caller (``filter_inview``) invokes this only for room prefixes in ``NO_FILTER_ROOM_TYPES``
    (``classroom`` at time of writing). Any object appearing in the instance map counts as in-view.

    Returns a dict compatible with ``_write_objects_in_view``:
      - ``kept_ids_ordered``: list[str]
      - ``gt_id_to_stub``: dict[str, dict]
      - ``f3_results``: dict[str, dict] — actual_pixels filled, truncation_ratio/near_edge null
    """
    seg_path = os.path.join(blend_dir, INSTANCE_SEGMENTATION_PNG)
    if not os.path.isfile(seg_path):
        raise MissingInViewInfoError(
            f"Missing {INSTANCE_SEGMENTATION_PNG}; cannot build objects_in_view: {seg_path}"
        )

    object_index_map = _load_object_index_map(blend_dir)
    base_to_gt = _build_metadata_base_to_gt_id(blend_dir)
    counts = _object_index_counts_from_png(seg_path)

    # All objects with any pixels (> 0), no threshold
    gt_id_to_pixels: Dict[str, int] = {}
    for pi, pc in sorted(counts.items()):
        key = str(int(pi))
        meta_name = object_index_map.get(key)
        if meta_name is None:
            continue
        mb = _metadata_label_to_base(meta_name)
        gid = base_to_gt.get(mb)
        if gid is None:
            continue
        if gid not in gt_id_to_pixels or pc > gt_id_to_pixels[gid]:
            gt_id_to_pixels[gid] = pc

    kept_ids_ordered = sorted(gt_id_to_pixels.keys())
    stubs = _id_and_category_from_gt(blend_dir, kept_ids_ordered)
    gt_id_to_stub = {s["id"]: s for s in stubs}

    # f3_results: actual_pixels from instance map, no ratio info
    f3_results: Dict[str, Dict[str, Any]] = {
        gid: {
            "actual_pixels": gt_id_to_pixels[gid],
            "truncation_ratio": None,
            "near_edge": None,
        }
        for gid in kept_ids_ordered
    }

    print(
        f"[build_inview] no-filter: {len(kept_ids_ordered)} GT objects in-view "
        f"(all instance-map pixels > 0, no threshold)"
    )

    return {
        "kept_ids_ordered": kept_ids_ordered,
        "gt_id_to_stub": gt_id_to_stub,
        "f3_results": f3_results,
    }


if __name__ == "__main__":
    import bpy

    d = os.path.dirname(os.path.abspath(bpy.data.filepath)) if bpy.data.filepath else ""
    if d:
        sid = scene_id_from_output_dir(d)
        build_instance_seg_view_payload(sid, d)