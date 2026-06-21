"""
scene_config.py — Scene-level semantic configuration shared across evaluation and reconstruction.

This module is the single source of truth for:
  - Which room types use the grid evaluation pipeline (D1 + D2 + D5; D3/D4 not used).
  - Which room types use the standard object-matching pipeline (D1–D4).
  - Which object categories are the grid targets for each grid room type.
  - Yaw symmetry exemptions used in rotation error calculation.

No computation logic lives here — only configuration constants.
Imports from configs/constants.py for the canonical ROOM_TYPES list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "configs") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "configs"))
from constants import ROOM_TYPES

# ---------------------------------------------------------------------------
# Pipeline routing
# ---------------------------------------------------------------------------

# Grid scenes: evaluated with D1 + D2 + D5 only (no object matching; D3/D4 N/A).
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
#
# GRID_TARGET_GROUPS groups categories into named sub-groups so that desks
# and chairs are evaluated with separate GCR/GE metrics for classroom and
# office scenes. Library has a single group (library_block).
#
# GRID_TARGET_CATEGORIES is the flat union per room type, kept for
# backward-compatible is_grid_target() checks and reconstruction routing.

GRID_TARGET_GROUPS: Dict[str, List[Tuple[str, FrozenSet[str]]]] = {
    # classroom: student_desk (main grid body) + simple_desk / simple_desk_base (asset naming
    #   in GT varies); chair is student_chair or swivel_chair depending on scene variant.
    "classroom": [
        ("desk",  frozenset({"simple_desk", "simple_desk_base", "student_desk"})),
        ("chair", frozenset({"swivel_chair", "student_chair"})),
    ],
    # office: simple_desk (+ simple_desk_base if used) + swivel_chair grids.
    "office": [
        ("desk",  frozenset({"simple_desk", "simple_desk_base"})),
        ("chair", frozenset({"swivel_chair"})),
    ],
    # library: library_block forms one contiguous grid.
    "library": [
        ("block", frozenset({"library_block"})),
    ],
}

# Derived flat union — do not edit directly; update GRID_TARGET_GROUPS instead.
GRID_TARGET_CATEGORIES: Dict[str, FrozenSet[str]] = {
    rt: frozenset(cat for _, cats in groups for cat in cats)
    for rt, groups in GRID_TARGET_GROUPS.items()
}


def is_grid_target(room_type: str, category: str) -> bool:
    """Return True if the given category is a grid target for the given room type."""
    targets = GRID_TARGET_CATEGORIES.get(room_type, frozenset())
    return category.strip().lower() in targets


# ---------------------------------------------------------------------------
# Yaw symmetry exemptions
# ---------------------------------------------------------------------------
# Used by numerical_metrics (rotation error) and reconstruction (pose write-back).
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


# ---------------------------------------------------------------------------
# Wall-mounted categories
# ---------------------------------------------------------------------------
# Used by reconstruction (render.py): for these categories, the predicted
# z coordinate is meaningful (the object is attached to a wall, not the floor)
# and is written back verbatim during reconstruction. For all other
# (floor-standing) categories, reconstruction preserves the original
# ``obj.location.z`` from scene.blend regardless of the predicted z.
#
# Rationale: Infinigen asset origins are not uniformly at floor level, so
# overwriting z for floor-standing objects would cause spurious vertical
# jitter that does not reflect any real prediction error. Wall-mounted
# objects don't have this problem — their z genuinely matters.

WALL_MOUNTED_CATEGORIES: FrozenSet[str] = frozenset({
    "mirror",
    "wall_art",
})


def is_wall_mounted(category: str) -> bool:
    """Return True if the given category is wall-mounted (z should be written from prediction)."""
    return category.strip().lower() in WALL_MOUNTED_CATEGORIES


# ---------------------------------------------------------------------------
# Metric thresholds (D2–D3)
# ---------------------------------------------------------------------------
# All thresholds are centralised here so that changing one value automatically
# propagates to numerical_metrics, export, and any downstream analysis code.

# D2: OOR — pairwise pred-pred bounding-box IoU threshold for collision detection.
COLLISION_IOU_THRESHOLD: float = 0.03

# D3: PE — XY position error above this value counts as a failure (metres).
PE_FAIL_THRESHOLD_M: float = 0.3

# D3: IoU — 3D bounding-box IoU below this value counts as a failure (dimensionless).
IOU_PASS_THRESHOLD: float = 0.15

# D3: RE — absolute yaw error above this value counts as a failure (degrees).
RE_FAIL_THRESHOLD_DEG: float = 30.0