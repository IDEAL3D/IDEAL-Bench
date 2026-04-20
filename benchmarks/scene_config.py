"""
scene_config.py — Scene-level semantic configuration shared across evaluation and reconstruction.

This module is the single source of truth for:
  - Which room types use the grid evaluation pipeline (D3 + D4 + D5).
  - Which room types use the standard matching pipeline (D1–D4).
  - Which object categories are the grid targets for each grid room type.
  - Yaw symmetry exemptions used in rotation error calculation.

No computation logic lives here — only configuration constants.
Imports from configs/constants.py for the canonical ROOM_TYPES list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, FrozenSet
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "configs") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "configs"))
from constants import ROOM_TYPES

# ---------------------------------------------------------------------------
# Pipeline routing
# ---------------------------------------------------------------------------

# Grid scenes: evaluated with D3 + D4 + D5 only (no object matching).
GRID_ROOM_TYPES: FrozenSet[str] = frozenset({
    "classroom",
    "library",
    "office",
})

# Non-grid scenes: evaluated with D1–D4 (object matching pipeline).
NON_GRID_ROOM_TYPES: FrozenSet[str] = frozenset(
    rt for rt in ROOM_TYPES if rt not in GRID_ROOM_TYPES
)


def is_grid_scene(room_type: str | None) -> bool:
    """Return True if the room type uses the grid evaluation pipeline."""
    return room_type in GRID_ROOM_TYPES


# ---------------------------------------------------------------------------
# Grid target categories
# ---------------------------------------------------------------------------
# For each grid room type, defines which object categories form the grid
# structure that is evaluated by D5 metrics and written back during
# reconstruction. All other objects in these scenes are ignored (treated
# as distractors).
#
# Source of truth for category strings: GT.json `category` field values.

GRID_TARGET_CATEGORIES: Dict[str, FrozenSet[str]] = {
    "classroom": frozenset({
        "simple_desk",
        "simple_desk_base",
        "student_desk",
        "swivel_chair",
        "student_chair",
    }),
    "office": frozenset({
        "simple_desk",
        "swivel_chair",
    }),
    "library": frozenset({
        "library_block",
    }),
}


def is_grid_target(room_type: str, category: str) -> bool:
    """Return True if the given category is a grid target for the given room type."""
    targets = GRID_TARGET_CATEGORIES.get(room_type, frozenset())
    return category.strip().lower() in targets


# ---------------------------------------------------------------------------
# Yaw symmetry exemptions
# ---------------------------------------------------------------------------
# Used by metrics.py (rotation error) and reconstruction (pose write-back).
#
# Three tiers of symmetry:
#
#   YAW_IGNORE:      Fully symmetric or orientation-irrelevant objects.
#                    Yaw is excluded from all rotation error calculations.
#
#   YAW_180_SYM:     Objects with 180-degree rotational symmetry.
#                    GT yaw θ and θ+180 are considered equivalent.
#                    Error = min(|Δyaw|, 180 - |Δyaw|) already handled by
#                    angular_difference_deg(); no extra folding needed beyond
#                    treating diff > 90 as 180 - diff.
#
#   YAW_90_SYM:      Square footprint objects with 90-degree rotational symmetry.
#                    GT yaw θ, θ+90, θ+180, θ+270 are all equivalent.
#                    Error = angular_difference_deg(yaw % 90, 0).

YAW_IGNORE_CATEGORIES: FrozenSet[str] = frozenset({
    "plant",
    "large_plant_container",
    "floor_lamp",
    "rug",
    "ceiling_lamp",
})

YAW_180_SYMMETRIC_CATEGORIES: FrozenSet[str] = frozenset({
    "simple_desk",
    "simple_desk_base",
    "blackboard",
    "library_block",
    "cell_shelf",
    "coffee_table",
})

YAW_90_SYMMETRIC_CATEGORIES: FrozenSet[str] = frozenset({
    "side_table",
})