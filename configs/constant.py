from __future__ import annotations

import re

ROOM_TYPES = [
    "bedroom",
    "homestudio",
    "livingroom",
    "kitchen",
    "bathroom",
    "diningroom",
    "office",
    "classroom",
    "meetingroom",
    "library",
]

# 对外 canonical id → Infinigen gin / 内部 room_type（复合词无下划线，scene_id 形如 livingroom_s123 便于解析）
CANONICAL_TO_INFINIGEN: dict[str, str] = {
    "bedroom": "bedroom",
    "homestudio": "homestudio",
    "livingroom": "living-room",
    "kitchen": "kitchen",
    "bathroom": "bathroom",
    "diningroom": "dining-room",
    "office": "open-office",
    "classroom": "classroom",
    "meetingroom": "meeting-room",
    "library": "library",
}

LIVING_SPACE_ROOM_TYPES: frozenset[str] = frozenset(
    {
        "bedroom",
        "homestudio",
        "livingroom",
        "kitchen",
        "bathroom",
        "diningroom",
    }
)

WORKING_SPACE_ROOM_TYPES: frozenset[str] = frozenset(
    {
        "office",
        "classroom",
        "meetingroom",
        "library",
    }
)


def infinigen_room_type(canonical: str) -> str:
    """Canonical ``ROOM_TYPES`` 项 → Infinigen ``compose_single_room.room_type`` 等使用的字符串。"""
    try:
        return CANONICAL_TO_INFINIGEN[canonical]
    except KeyError as e:
        raise KeyError(f"未知房型 canonical: {canonical!r}；须为 {list(ROOM_TYPES)}") from e


def canonicalize_asset_category(category: str) -> str:
    """
    Infinigen 图书馆中每个书架 **块** 形如 ``library_block_007``；语义上同属 ``library_block``。
    GT / ``objects_in_view`` / 推理资产表统一用该 canonical 名，避免「一种家具多占多个 category」。
    """
    raw = str(category).strip()
    s = raw.lower()
    if re.fullmatch(r"library_block_\d+", s):
        return "library_block"
    return raw


# objects_in_view：是否「有效物体」由 GT（extract_gt / collect_assets）界定；实现上仅需
# 像素 ≥ MIN_VISIBLE_PIXELS、object_index_map 可解析、且 metadata 名与 GT id 对齐——三者即「镜内可见的有效物体」，无需再对 map 名做 spawn/围护子串过滤。
# 兼容旧名 / 外部脚本
ASSET_INCLUDE_KEYWORDS: tuple[str, ...] = ("spawn_asset",)
ASSET_FILTER_KEYWORD = "spawn_asset"

# --- instance_segmentation 标量备忘（1280×720，与聊天里抽查一致，便于今后调阈值）---
# library_s1199075478：library_block_007 → pass_index 44，2514 px（metadata 名 library_block_007_001）。
# bathroom_s776419884：SingleCabinetFactory(1614902).spawn_asset(3234064) → idx 45，15830 px。
# openoffice_s643270004：SimpleBookcaseFactory(8462203).spawn_asset(8733329) → idx 67 为 1307 px；同 spawn_asset 另段 idx 69 本视角 0 px。
# openoffice_s643270004：落地灯两段 mesh → idx 22 为 1445 px，idx 25 为 693 px。
# office_s741428374：SimpleDeskFactory(...).002 → idx 75 为 113 px；SwivelChairFactory(...).002 → idx 115 为 266 px。
# kitchen_s511530340：夹缝里 LargePlantContainer 曾约 149 px（极小块参考）。
#
# Visibility threshold (objects with fewer visible pixels are excluded from in-view).
MIN_VISIBLE_PIXELS = 300