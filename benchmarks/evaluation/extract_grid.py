"""
extract_grid.py — GT grid parameter extraction for IDEAL Dimension 5 evaluation.

Provides the core logic for extracting a structured grid from GT object positions
and identifying which objects participate in the grid evaluation pipeline.

This module is consumed by:
  - benchmarks/evaluation/metrics_grid.py  (D5 metric computation)
  - benchmarks/evaluation/check_gt_grid.py (internal GT validation tool)

Public API
----------
  extract_grid_params(centers, round_digits) -> GridParams | None
  get_targets(room_type, objects)            -> list[GridTarget]
  get_room_type(payload)                     -> str | None
  load_objects_from_payload(payload)         -> list[dict]
  GridParams   — dataclass holding extracted grid geometry
  GridTarget   — dataclass holding target objects and skip reason
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Import category config from scene_config (benchmarks-level shared config).
_BENCHMARKS_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_ROOT))
from scene_config import GRID_TARGET_CATEGORIES

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GridParams:
    """Grid geometry extracted from GT object center positions."""
    R:           int
    C:           int
    row_ys:      List[float]
    col_xs:      List[float]
    row_spacing: Optional[float]   # None when R == 1
    col_spacing: Optional[float]   # None when C == 1
    half_w:      float             # half cell width  (col_spacing / 2)
    half_h:      float             # half cell height (row_spacing / 2)
    cells:       List[Tuple[float, float, float, float]]  # (cx, cy, half_w, half_h)

    def find_cell(
        self, px: float, py: float
    ) -> Optional[Tuple[float, float, float, float]]:
        """Return the cell that contains point (px, py), or None."""
        for cell in self.cells:
            cx, cy, hw, hh = cell
            if abs(px - cx) <= hw and abs(py - cy) <= hh:
                return cell
        return None


@dataclass
class GridTarget:
    """Objects selected as grid targets for one room type."""
    label:       str                              # e.g. "student_desk", "library_block"
    hits:        List[Tuple[float, float, dict]]  # (x, y, obj)
    note:        str                              # human-readable selection summary
    skip_reason: str                              # non-empty → skip grid computation

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _seed(obj_id: str) -> str:
    """Extract the first parenthesised integer from a factory id string.

    Example: 'SimpleDeskBaseFactory(6269255).spawn_asset(6781847)' -> '6269255'
    """
    m = re.search(r'\((\d+)\)', str(obj_id))
    return m.group(1) if m else obj_id


def _xy(obj: dict) -> Optional[Tuple[float, float]]:
    pos = obj.get("position", [])
    return (float(pos[0]), float(pos[1])) if len(pos) >= 2 else None


def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2


def _median_spacing(coords: List[float]) -> Optional[float]:
    if len(coords) < 2:
        return None
    return _median([abs(coords[i + 1] - coords[i]) for i in range(len(coords) - 1)])

# ---------------------------------------------------------------------------
# Core: grid parameter extraction
# ---------------------------------------------------------------------------

def extract_grid_params(
    centers: List[Tuple[float, float]],
    round_digits: int = 2,
) -> Optional[GridParams]:
    """Extract grid geometry from a set of axis-aligned, evenly-spaced center points.

    Assumptions:
      - Objects are arranged in a grid aligned with the X/Y axes (no rotation).
      - Spacing is uniform along each axis.
      - Coordinates have no noise (guaranteed by procedural generation).

    Parameters
    ----------
    centers:
        List of (x, y) center positions for the target objects.
    round_digits:
        Decimal places used when deduplicating coordinates to identify
        distinct rows and columns (default 2, matching GT precision).

    Returns
    -------
    GridParams if len(centers) >= 2, else None.
    """
    if len(centers) < 2:
        return None

    col_xs = sorted(set(round(x, round_digits) for x, _ in centers))
    row_ys = sorted(set(round(y, round_digits) for _, y in centers))
    R, C   = len(row_ys), len(col_xs)

    row_sp = _median_spacing(row_ys)
    col_sp = _median_spacing(col_xs)

    # Degenerate cases: single row or single column — borrow the other spacing.
    if row_sp is None:
        row_sp = col_sp
    if col_sp is None:
        col_sp = row_sp

    half_h = row_sp / 2 if row_sp else 0.5
    half_w = col_sp / 2 if col_sp else 0.5

    cells = [
        (float(cx), float(ry), half_w, half_h)
        for ry in row_ys
        for cx in col_xs
    ]

    return GridParams(
        R=R, C=C,
        row_ys=[float(v) for v in row_ys],
        col_xs=[float(v) for v in col_xs],
        row_spacing=row_sp,
        col_spacing=col_sp,
        half_w=half_w,
        half_h=half_h,
        cells=cells,
    )

# ---------------------------------------------------------------------------
# Core: target object selection
# ---------------------------------------------------------------------------

def get_targets(room_type: str, objects: list) -> List[GridTarget]:
    """Select objects that participate in the D5 grid evaluation for a given room type.

    Selection logic (classroom and office):
      1. Filter objects whose category appears in GRID_TARGET_CATEGORIES[room_type].
      2. Group by factory seed (first parenthesised integer in the id field).
      3. Select the group with the most instances — this is the grid body
         (e.g. student desks), automatically excluding singletons such as
         the teacher's podium desk which shares the same category but a
         different factory seed.

    Selection logic (library):
      Filter objects with category == 'library_block'.

    Parameters
    ----------
    room_type:
        Canonical room type string (e.g. 'classroom', 'library', 'office').
    objects:
        Raw object list from GT or objects_in_view payload.

    Returns
    -------
    List of GridTarget (usually length 1). skip_reason is non-empty when
    there are insufficient objects to form a grid.
    """
    room_type = room_type.strip().lower()
    target_cats = GRID_TARGET_CATEGORIES.get(room_type, frozenset())

    if not target_cats:
        return []

    # Filter to target categories.
    candidates = [
        o for o in objects
        if str(o.get("category", "")).strip().lower() in target_cats and _xy(o)
    ]

    if not candidates:
        return [GridTarget(
            label=room_type + "_grid",
            hits=[],
            note="no objects matching target categories found",
            skip_reason="no_target",
        )]

    if room_type == "library":
        # Library: all library_block objects form one contiguous grid.
        hits  = [(_xy(o)[0], _xy(o)[1], o) for o in candidates]
        label = "library_block"
        note  = f"library_block count={len(hits)}"
        skip  = "" if len(hits) >= 2 else f"n_a_insufficient(n={len(hits)})"
        return [GridTarget(label=label, hits=hits, note=note, skip_reason=skip)]

    # Classroom / Office: group by factory seed; pick the largest group.
    groups: Dict[str, List] = {}
    for o in candidates:
        groups.setdefault(_seed(o.get("id", "")), []).append(o)

    best     = max(groups, key=lambda s: len(groups[s]))
    main     = groups[best]
    excluded = len(candidates) - len(main)

    # Derive a human-readable label from the winning group's category.
    label = str(main[0].get("category", "desk")).strip().lower()
    hits  = [(_xy(o)[0], _xy(o)[1], o) for o in main]
    note  = (
        f"total={len(candidates)}, seed_groups={len(groups)}, "
        f"selected_seed={best}({len(main)} objects), excluded={excluded}"
    )
    skip = "" if len(hits) >= 2 else f"n_a_insufficient(n={len(hits)})"
    return [GridTarget(label=label, hits=hits, note=note, skip_reason=skip)]

# ---------------------------------------------------------------------------
# Payload utilities
# ---------------------------------------------------------------------------

def get_room_type(payload: dict) -> Optional[str]:
    """Read scene.room.type from a GT or prediction payload dict."""
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        return None
    room = scene.get("room")
    if not isinstance(room, dict):
        return None
    t = room.get("type")
    return str(t).strip().lower() if t else None


def load_objects_from_payload(payload: dict) -> list:
    """Extract the objects list from a payload dict (two supported formats)."""
    scene = payload.get("scene")
    if isinstance(scene, dict):
        return scene.get("objects", [])
    return payload.get("objects", [])