"""
Inverse of ``extract_gt`` for one object: given GT JSON, Blender ``(location, dimensions)`` from
``scene.blend``, and world-frame prediction, return ``final_location``, ``final_rotation_z`` (deg),
and ``final_dimensions`` for write-back. Pure math, no ``bpy``.

Rotation cases: yaw-exempt (lamps, plants, …) → 0; table-dining family → use pred/GT yaw as Blender Z;
other furniture → +90° offset between GT and Blender conventions. Dimension swap when GT used semantic W/D
swap; scale local offset from GT to pred sizes; re-apply rotation. Z clamp for floor objects is done in
``compose`` (see internal Z-axis policy notes).

API: ``invert_pose``, ``is_yaw_exempt``, ``is_table_dining_family``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

# Scene-level config is the single source of truth for yaw-exempt categories.
# Tests under benchmarks/tests/ add the repo root to sys.path, so the
# canonical import works there too.
try:
    from benchmarks.common.scene_config import YAW_IGNORE_CATEGORIES
except ImportError:  # pragma: no cover - standalone fallback
    YAW_IGNORE_CATEGORIES = frozenset({
        "plant", "large_plant_container", "floor_lamp", "rug", "ceiling_lamp",
    })

# table_dining family: coffee_table, conference_table, table_dining. These
# assets receive a +90 deg compensation inside extract_gt (see
# ``TABLE_DINING_FAMILY_YAW_FIX`` there). Net effect: the GT-convention yaw
# and dims for these assets coincide with the Blender convention directly --
# no extra +90 deg on rotation and no W/D swap on dims when writing back.
try:
    from generation.generate_lib.extract_gt import (
        TABLE_DINING_FAMILY_YAW_FIX,
    )
except ImportError:  # pragma: no cover - standalone fallback
    TABLE_DINING_FAMILY_YAW_FIX = frozenset({
        "coffee_table", "table_dining", "conference_table",
    })


# ---------------------------------------------------------------------------
# Category rules
# ---------------------------------------------------------------------------


def is_yaw_exempt(category: str) -> bool:
    """Return True if the category has no meaningful yaw.

    Yaw-exempt categories skip the ``+90 deg`` mapping and the W/D swap during
    inverse pose computation.
    """
    return category.strip().lower() in YAW_IGNORE_CATEGORIES


def is_table_dining_family(category: str) -> bool:
    """Return True for coffee_table / conference_table / table_dining.

    These assets are handled identically to Blender convention: the GT
    ``rotation_yaw`` equals the Blender ``rotation_euler.z`` (in degrees)
    and the GT ``dimensions`` match the Blender ``obj.dimensions`` directly.
    This is because ``extract_gt`` applies a +90 deg compensation that
    cancels out the +90 deg mapping we apply to regular non-exempt assets.
    """
    return category.strip().lower() in TABLE_DINING_FAMILY_YAW_FIX


# ---------------------------------------------------------------------------
# Small vector helpers (kept local to avoid a numpy dependency in this module)
# ---------------------------------------------------------------------------


def _as_xyz(v: Sequence[float]) -> List[float]:
    """Return ``[x, y, z]`` from a 2- or 3-element sequence, padding Z with 0."""
    if len(v) >= 3:
        return [float(v[0]), float(v[1]), float(v[2])]
    if len(v) == 2:
        return [float(v[0]), float(v[1]), 0.0]
    raise ValueError(f"Expected 2- or 3-element sequence, got {v!r}")


def _rotate_xy(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a 2-D vector CCW by *deg* degrees."""
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c - y * s, x * s + y * c


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def invert_pose(
    pred_obj: Dict[str, Any],
    gt_obj: Dict[str, Any],
    original_blender: Dict[str, Any],
    category: str,
) -> Dict[str, Any]:
    """Compute the Blender pose to write back for one matched object.

    Parameters
    ----------
    pred_obj:
        Predicted object dict *after* camera-to-world transform. Requires
        ``position`` (``[x, y, z]``) and ``dimensions`` (``[W, D, H]``).
        ``rotation_yaw`` may be ``None`` for yaw-exempt categories.
    gt_obj:
        GT object dict from ``GT.json``. Requires ``position``.
    original_blender:
        Original Blender state read from ``scene.blend``::

            {"location":   [x, y, z],   # obj.location
             "dimensions": [x, y, z]}   # obj.dimensions

    category:
        Object category string (e.g. ``"bed"``). Controls yaw exemption and
        W/D swap.

    Returns
    -------
    dict with three keys::

        {"final_location":    [x, y, z],   # metres, world frame
         "final_rotation_z":  float_deg,   # degrees, CCW
         "final_dimensions":  [x, y, z]}   # metres, Blender axes (W_x, D_y, H_z)

    Notes
    -----
    - Z is NOT overwritten here. The caller (compose.py) applies the
      wall-mounted vs floor-standing policy after this function returns.
    - Rotation is returned in degrees; render.py converts to radians when
      assigning to ``obj.rotation_euler.z``.
    """
    cat = category.strip().lower()
    exempt = is_yaw_exempt(cat)
    table_dining = is_table_dining_family(cat)

    pred_pos = _as_xyz(pred_obj["position"])
    gt_pos = _as_xyz(gt_obj["position"])
    orig_loc = _as_xyz(original_blender["location"])
    orig_dim = _as_xyz(original_blender["dimensions"])
    pred_dim = _as_xyz(pred_obj["dimensions"])

    # -- Step 1: rotation (final blender rotation_z) -------------------------
    # Three cases:
    #   (a) yaw-exempt (lamp/plant/rug/...):        final_rot_z = 0
    #   (b) table_dining family (coffee_table/...): final_rot_z = pred_yaw
    #       extract_gt applies a +90 deg compensation that makes the GT-convention
    #       yaw equal the Blender rotation_z directly.
    #   (c) everything else (bed/sofa/desk/...):    final_rot_z = pred_yaw + 90
    gt_yaw = gt_obj.get("rotation_yaw")
    if exempt:
        final_rot_z_deg = 0.0
        blender_rot_orig_deg = 0.0
    else:
        pred_yaw = pred_obj.get("rotation_yaw")
        if pred_yaw is None:
            # Non-exempt with missing pred yaw: treat as 0 and let the
            # downstream metric flag it. Don't raise; we still want a
            # renderable scene.
            pred_yaw = 0.0
        # Regular non-exempt assets carry a +90 deg offset; table_dining family
        # does not (its +90 offset is already absorbed inside extract_gt).
        yaw_offset_deg = 0.0 if table_dining else 90.0
        final_rot_z_deg = (float(pred_yaw) + yaw_offset_deg) % 360.0
        # The original blender rotation_z at GT time. We need this to un-apply
        # the rotation baked into (orig_loc - gt_pos). Uses the same offset
        # convention.
        if gt_yaw is None:
            blender_rot_orig_deg = 0.0
        else:
            blender_rot_orig_deg = (float(gt_yaw) + yaw_offset_deg) % 360.0

    # -- Step 2: W/D swap on Blender axes ------------------------------------
    # pred_dim is in GT convention [W, D, H]. Blender's obj.dimensions is
    # [X, Y, Z]. Regular non-exempt assets face +X in Blender at yaw=0, so
    # GT_W (side) = Blender Y and GT_D (forward) = Blender X -- requires swap.
    # Yaw-exempt and table_dining family: no swap (GT dims == Blender dims).
    if exempt or table_dining:
        final_dim = [pred_dim[0], pred_dim[1], pred_dim[2]]
    else:
        final_dim = [pred_dim[1], pred_dim[0], pred_dim[2]]  # swap X<->Y

    # -- Step 3: local-frame offset (bug fix) --------------------------------
    # (orig_loc - gt_pos) is the "origin minus centre" vector in the CURRENT
    # world frame, i.e. already rotated by the original blender rotation.
    # To scale it along the object's local axes we must first un-rotate it
    # back to local, then later re-rotate by the new (pred-driven) angle.
    #
    # extract_gt produces:
    #     gt_pos  = matrix_world_orig @ local_centre
    #              = orig_loc + R(blender_rot_orig) @ S_orig @ local_centre
    # so:
    #     (orig_loc - gt_pos) = -R(blender_rot_orig) @ S_orig @ local_centre
    # We want, for the rebuilt scene:
    #     final_loc = pred_pos - R(blender_rot_new) @ S_new @ local_centre
    #
    # Rearranging, and absorbing the S_new / S_orig ratio componentwise:
    #     offset_local     = R(-blender_rot_orig) @ (orig_loc - gt_pos)_xy
    #                        # now sitting on local-x, local-y axes
    #     offset_local_scl = offset_local * (final_dim_xy / orig_dim_xy)
    #                        # scaled to the new dimensions
    #     offset_world_new = R(blender_rot_new) @ offset_local_scl
    #     final_loc_xy     = pred_pos_xy + offset_world_new_xy
    #
    # Z is independent: Z rotation preserves Z, so Z offset just gets scaled.
    off_world_x = orig_loc[0] - gt_pos[0]
    off_world_y = orig_loc[1] - gt_pos[1]
    off_world_z = orig_loc[2] - gt_pos[2]

    # Un-rotate by the original blender rotation (XY only).
    off_local_x, off_local_y = _rotate_xy(
        off_world_x, off_world_y, -blender_rot_orig_deg
    )

    # Componentwise scale on Blender axes (so we use final_dim, not pred_dim).
    def _ratio(num: float, den: float) -> float:
        return num / den if abs(den) > 1e-9 else 1.0

    rx = _ratio(final_dim[0], orig_dim[0])
    ry = _ratio(final_dim[1], orig_dim[1])
    rz = _ratio(final_dim[2], orig_dim[2])
    off_local_x *= rx
    off_local_y *= ry
    off_local_scaled_z = off_world_z * rz

    # Re-rotate by the new blender rotation (XY only).
    off_new_x, off_new_y = _rotate_xy(
        off_local_x, off_local_y, final_rot_z_deg
    )

    final_loc = [
        pred_pos[0] + off_new_x,
        pred_pos[1] + off_new_y,
        pred_pos[2] + off_local_scaled_z,
    ]

    return {
        "final_location": [round(v, 4) for v in final_loc],
        "final_rotation_z": round(final_rot_z_deg, 3),
        "final_dimensions": [round(v, 4) for v in final_dim],
    }