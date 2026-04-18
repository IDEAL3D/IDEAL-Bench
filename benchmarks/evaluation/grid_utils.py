"""
grid_utils.py — Grid helpers shared by metrics_grid and check_gt_grid (no CLI, no logging).

Public: extract_grid_params, get_targets, get_room_type, load_objects_from_payload
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SUPPORTED_ROOMS = frozenset({"classroom", "office", "library"})
DESK_KEYWORDS = ("desk", "Desk")


@dataclass
class GridTarget:
    """One grid-eval target for a scene."""
    label: str
    hits: List[Tuple[float, float, dict]]
    note: str
    skip_reason: str


@dataclass
class GridParams:
    """Grid geometry from aligned centers."""
    R: int
    C: int
    row_ys: List[float]
    col_xs: List[float]
    row_spacing: Optional[float]
    col_spacing: Optional[float]
    half_w: float
    half_h: float
    cells: List[Tuple[float, float, float, float]]

    def has_gt(self, cx: float, cy: float) -> bool:
        """Unused by current pipeline; kept on GridParams for compatibility."""
        return abs(cx - cx) <= self.half_w and abs(cy - cy) <= self.half_h

    def find_cell(self, px: float, py: float) -> Optional[Tuple[float, float, float, float]]:
        """Cell containing (px, py), or None."""
        for cell in self.cells:
            cx, cy, hw, hh = cell
            if abs(px - cx) <= hw and abs(py - cy) <= hh:
                return cell
        return None


def _seed(obj_id: str) -> str:
    """Factory seed from id string, e.g. ...Factory(6269255)... -> 6269255."""
    m = re.search(r"\((\d+)\)", str(obj_id))
    return m.group(1) if m else obj_id


def _is_desk(obj: dict) -> bool:
    return any(k in str(obj.get("id", "")) for k in DESK_KEYWORDS)


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


def extract_grid_params(
    centers: List[Tuple[float, float]],
    round_digits: int = 2,
) -> Optional[GridParams]:
    """Build GridParams from axis-aligned, evenly spaced 2D centers; None if count < 2."""
    if len(centers) < 2:
        return None

    col_xs = sorted(set(round(x, round_digits) for x, y in centers))
    row_ys = sorted(set(round(y, round_digits) for x, y in centers))
    R, C = len(row_ys), len(col_xs)

    row_sp = _median_spacing(row_ys)
    col_sp = _median_spacing(col_xs)

    if row_sp is None:
        row_sp = col_sp
    if col_sp is None:
        col_sp = row_sp

    half_h = row_sp / 2 if row_sp else 0.5
    half_w = col_sp / 2 if col_sp else 0.5

    cells = [
        (cx, ry, half_w, half_h)
        for ry in row_ys
        for cx in col_xs
    ]

    return GridParams(
        R=R,
        C=C,
        row_ys=row_ys,
        col_xs=col_xs,
        row_spacing=row_sp,
        col_spacing=col_sp,
        half_w=half_w,
        half_h=half_h,
        cells=cells,
    )


def get_targets(room: str, objects: list) -> List[GridTarget]:
    """Select grid targets for classroom / office / library."""
    room = room.strip().lower()

    if room in ("classroom", "office"):
        desks = [o for o in objects if _is_desk(o) and _xy(o)]
        if not desks:
            return [
                GridTarget(
                    label="desk",
                    hits=[],
                    note="no desk assets",
                    skip_reason="no_target",
                )
            ]

        groups: Dict[str, List] = {}
        for o in desks:
            groups.setdefault(_seed(o.get("id", "")), []).append(o)
        best = max(groups, key=lambda s: len(groups[s]))
        main = groups[best]
        excluded = len(desks) - len(main)

        hits = [(_xy(o)[0], _xy(o)[1], o) for o in main]
        label = "student_desk" if room == "classroom" else "desk"
        note = (
            f"desks={len(desks)}, seed_groups={len(groups)}, "
            f"chosen_seed={best}(n={len(main)}), excluded={excluded}"
        )
        skip = "" if len(hits) >= 2 else f"n_a_insufficient(n={len(hits)})"
        return [GridTarget(label=label, hits=hits, note=note, skip_reason=skip)]

    if room == "library":
        hits = [
            (_xy(o)[0], _xy(o)[1], o)
            for o in objects
            if str(o.get("category", "")).strip().lower() == "library_block" and _xy(o)
        ]
        if not hits:
            return [
                GridTarget(
                    label="library_block",
                    hits=[],
                    note="no library_block",
                    skip_reason="no_target",
                )
            ]
        skip = "" if len(hits) >= 2 else f"n_a_insufficient(n={len(hits)})"
        return [
            GridTarget(
                label="library_block",
                hits=hits,
                note=f"library_block_count={len(hits)}",
                skip_reason=skip,
            )
        ]

    return []


def get_room_type(payload: dict) -> Optional[str]:
    """Read scene.room.type from a GT or pred payload."""
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        return None
    room = scene.get("room")
    if not isinstance(room, dict):
        return None
    t = room.get("type")
    return str(t).strip().lower() if t else None


def load_objects_from_payload(payload: dict) -> list:
    """Return objects list from payload."""
    scene = payload.get("scene")
    if isinstance(scene, dict):
        return scene.get("objects", [])
    return payload.get("objects", [])
