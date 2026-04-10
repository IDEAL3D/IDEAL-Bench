"""
Collection exclusions, ``unique_assets`` enumeration, categories, camera/room path helpers.

Runs in Blender for ``collect_assets`` / ``find_render_camera`` / ``identify_camera_room``.
Shared by GT, overlap checks, and (via re-exports) legacy ``extract_camview`` imports.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_GENERATION_DIR = os.path.dirname(_PKG_DIR)
if _GENERATION_DIR not in sys.path:
    sys.path.insert(0, _GENERATION_DIR)

_REPO_ROOT = os.path.dirname(_GENERATION_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from configs.constant import canonicalize_asset_category

from generation_common.scene_geometry import raycast_floor, room_height, world_aabb

# Legacy preprocess/config.py: collection rules shared by GT / overlap (no bpy at import time).
ROOM_STRUCTURE_COLLECTIONS = frozenset(
    {
        "unique_assets:room_wall",
        "unique_assets:room_floor",
        "unique_assets:room_ceiling",
        "unique_assets:room_exterior",
        "unique_assets:staircases",
    }
)
ASSET_GT_EXCLUDE_COLLECTIONS = ROOM_STRUCTURE_COLLECTIONS | frozenset(
    {
        "unique_assets:doors",
        "unique_assets:windows",
        "unique_assets:door_casings",
        "skirting",
    }
)
FULL_DELETE_COLLECTION_PREFIXES = (
    "door_base_elements",
)

KEEP_CATEGORIES = frozenset(
    {
        "chair",
        "office_chair",
        "bar_chair",
        "sofa",
        "table_dining",
        "table_cocktail",
        "simple_desk",
        "table_top",
        "large_shelf",
        "cell_shelf",
        "simple_bookcase",
        "triangle_shelf",
        "single_cabinet",
        "cabinet",
        "cabinet_drawer",
        "kitchen_cabinet",
        "kitchen_space",
        "countertop",
        "wall_shelf",
        "bed",
        "lamp",
        "floor_lamp",
        "desk_lamp",
        "ceiling_light",
        "ceiling_classic_lamp",
        "tv",
        "beverage_fridge",
        "microwave",
        "oven",
        "dishwasher",
        "toilet",
        "sink",
        "bathtub",
        "rug",
        "wall_art",
        "plant_container",
        "large_plant_container",
        "aquarium_tank",
        "range_hood",
    }
)


def scene_id_from_output_dir(dir_path: str) -> str:
    """Basename of the scene folder containing ``scene.blend`` (e.g. ``kitchen_medium_s1759680675``)."""
    return os.path.basename(os.path.normpath(os.path.abspath(dir_path)))


def factory_to_category(obj_name: str) -> str:
    m = re.match(r"^([A-Za-z]+?)Factory", obj_name)
    if m:
        camel = m.group(1)
        cat = re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()
    else:
        base = obj_name.split("(")[0].split(".")[0]
        cat = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower() or "unknown"
    return canonicalize_asset_category(cat)


def extract_room_type(blend_path: str) -> str:
    scene_dir = os.path.basename(os.path.dirname(os.path.dirname(blend_path)))
    parts = scene_dir.split("_")
    return parts[0] if parts else "unknown"


RENDER_CAMERA_NAME = "camera_0_0"


def find_render_camera():
    import bpy

    obj = bpy.data.objects.get(RENDER_CAMERA_NAME)
    if obj is None:
        return None
    if obj.type == "CAMERA":
        return obj
    for c in getattr(obj, "children", []):
        if c.type == "CAMERA":
            return c
    return None


def identify_camera_room() -> Optional[Dict[str, Any]]:
    cam = find_render_camera()
    if cam is None:
        print("[generation_common.asset_rules] ERROR: render camera not found.")
        return None

    cam_loc = cam.matrix_world.to_translation()
    floor_obj, _ = raycast_floor(cam_loc)
    if floor_obj is None:
        print("[generation_common.asset_rules] ERROR: no floor below camera.")
        return None

    rid = floor_obj.name.split(".")[0]
    flo, fhi = world_aabb(floor_obj)
    cx = 0.5 * (flo.x + fhi.x)
    cy = 0.5 * (flo.y + fhi.y)
    ztop = fhi.z
    W = round(fhi.x - flo.x, 2)
    D = round(fhi.y - flo.y, 2)
    H = round(room_height(rid, ztop), 2)

    print(f"[generation_common.asset_rules] room={rid}  size=({W}, {D}, {H}) (ray-cast)")
    return {
        "cam_obj": cam,
        "room_id": rid,
        "floor_center": (cx, cy, ztop),
        "floor_aabb": (flo, fhi),
        "W": W,
        "D": D,
        "H": H,
    }


def collect_assets() -> list:
    """Top-level objects under ``unique_assets``, excluding structure/doors/windows/skirting collections (same set as GT)."""
    import bpy

    ua = bpy.data.collections.get("unique_assets")
    if ua is None:
        return []

    skip_names: set = set()
    for col_name in ASSET_GT_EXCLUDE_COLLECTIONS:
        col = bpy.data.collections.get(col_name)
        if col:
            for o in col.all_objects:
                skip_names.add(o.name)

    for col in bpy.data.collections:
        if any(col.name.startswith(p) for p in FULL_DELETE_COLLECTION_PREFIXES):
            for o in col.all_objects:
                skip_names.add(o.name)

    return [
        o for o in ua.all_objects if o.name not in skip_names and o.parent is None
    ]
