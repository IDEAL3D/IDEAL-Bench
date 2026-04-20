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

# Public canonical id → Infinigen gin / internal room_type (no underscore in compounds; scene_id like livingroom_s123).
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
    """Map a ``ROOM_TYPES`` canonical id to Infinigen ``compose_single_room.room_type`` (etc.)."""
    try:
        return CANONICAL_TO_INFINIGEN[canonical]
    except KeyError as e:
        raise KeyError(
            f"Unknown canonical room type {canonical!r}; expected one of {list(ROOM_TYPES)}"
        ) from e


def canonicalize_asset_category(category: str) -> str:
    """
    Infinigen library shelf **blocks** look like ``library_block_007``; semantically they are ``library_block``.
    GT / ``objects_in_view`` / inference asset lists use this canonical name so one furniture type does not
    split across multiple category strings.
    """
    raw = str(category).strip()
    s = raw.lower()
    if re.fullmatch(r"library_block_\d+", s):
        return "library_block"
    return raw


# objects_in_view: “valid object” is defined by GT (extract_gt / collect_assets). In practice we only need
# pixels ≥ MIN_VISIBLE_PIXELS, a resolvable object_index_map, and metadata names aligned with GT ids—together
# that is “in-view valid objects”; no extra spawn/enclosure substring filter on map names.
# Legacy alias for external scripts
ASSET_INCLUDE_KEYWORDS: tuple[str, ...] = ("spawn_asset",)
ASSET_FILTER_KEYWORD = "spawn_asset"

# --- instance_segmentation notes (1280×720 spot checks; for future threshold tuning) ---
# library_s1199075478: library_block_007 → pass_index 44, 2514 px (metadata name library_block_007_001).
# bathroom_s776419884: SingleCabinetFactory(1614902).spawn_asset(3234064) → idx 45, 15830 px.
# openoffice_s643270004: SimpleBookcaseFactory(8462203).spawn_asset(8733329) → idx 67, 1307 px; same spawn idx 69 0 px this view.
# openoffice_s643270004: floor lamp two mesh pieces → idx 22 1445 px, idx 25 693 px.
# office_s741428374: SimpleDeskFactory(...).002 → idx 75 113 px; SwivelChairFactory(...).002 → idx 115 266 px.
# kitchen_s511530340: LargePlantContainer in gap ~149 px (very small patch reference).
#
# Visibility threshold (objects with fewer visible pixels are excluded from in-view).
MIN_VISIBLE_PIXELS = 300
