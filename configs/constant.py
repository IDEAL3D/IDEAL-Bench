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


# Instance map filtering
ASSET_FILTER_KEYWORD = "spawn_asset"

# Visibility threshold (objects with fewer visible pixels are excluded)
MIN_VISIBLE_PIXELS = 50