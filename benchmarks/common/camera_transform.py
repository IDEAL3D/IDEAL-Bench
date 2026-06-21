"""
Camera-frame → world-frame transform for the floor protocol (``compose`` and optional metric use).

The model treats +Y as camera forward. Horizontal camera yaw is read from ``camera.rotation_euler[2]`` in
``metadata.json``, snapped to the nearest 90°, and mapped to a correction delta by ``snap_delta_deg``. That
delta rotates XY ``position`` about the scene origin and adds to ``rotation_yaw`` (mod 360). ``dimensions``
and Z are left untouched: sizes stay in the object's canonical frame and do not depend on camera heading.

API: ``load_camera_meta``, ``camera_yaw_deg``, ``snap_delta_deg``, ``transform_pred_objects``,
``transform_pred_scene``.
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Camera metadata loader
# ---------------------------------------------------------------------------


def load_camera_meta(scene_dir: Path) -> dict[str, Any] | None:
    """Load the camera sub-dict from ``metadata.json`` in *scene_dir*.

    Returns ``None`` if the file is missing, malformed, or lacks
    ``rotation_euler``.
    """
    metadata_path = Path(scene_dir) / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        with open(metadata_path, encoding="utf-8") as f:
            meta = json.load(f)
        cam = meta.get("camera")
        if not isinstance(cam, dict):
            return None
        euler = cam.get("rotation_euler")
        if not isinstance(euler, list) or len(euler) < 3:
            return None
        return cam
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Angle computation
# ---------------------------------------------------------------------------


def camera_yaw_deg(camera_meta: dict[str, Any]) -> float:
    """Return the camera's horizontal yaw angle in degrees (CCW from world +Y).

    Uses only rz = rotation_euler[2] (the Z / horizontal-yaw component).

    Derivation
    ----------
    Blender camera at rest (euler = [0, 0, 0]) looks toward world +Y.
    After Rz(rz), the horizontal forward vector is:

        fwd = Rz(rz) @ [0, 1, 0] = [-sin(rz), cos(rz)]

    The angle of this vector from world +Y (measured CCW) is:

        yaw = atan2(fwd_x, fwd_y) = atan2(-sin(rz), cos(rz))

    We ignore rx (tilt) and ry (roll) because the model reasons about a flat
    floor plan and projects everything onto the XY plane.

    Verified with bedroom_s14704319
    --------------------------------
      Camera at [1.703, -0.151], points toward origin → true forward ≈ (-0.995, 0.088)
      true yaw ≈ -84.9°.
      rz = 1.502 → atan2(-sin(1.502), cos(1.502)) ≈ -86.1° ✓
      snap → -90° → delta = +90°
      bed pred yaw=180 → world yaw=(180+90)%360=270° = GT ✓

    Returns degrees (float, range -180 to +180).
    """
    rz = float(camera_meta["rotation_euler"][2])
    return math.degrees(math.atan2(-math.sin(rz), math.cos(rz)))


def snap_delta_deg(yaw_cam_deg: float) -> int:
    """Map camera yaw to a rotation delta using explicit 90° quadrant intervals.

    ``yaw_cam_deg`` is the angle from world +Y to camera forward, CCW positive,
    range [-180, +180].  Returns the delta (degrees, CCW positive) to apply to
    predicted object positions and yaws so they align with the world frame.

    Interval logic
    --------------
    [-45,  +45] → camera faces +Y → delta =    0° (no transform needed)
    [+45, +135] → camera faces +X → delta =  -90° (rotate CW 90°)
    [+135,+180] \
    [-180,-135] → camera faces -Y → delta = ±180°
    [-135, -45] → camera faces -X → delta =  +90° (rotate CCW 90°)

    Equivalently: delta = -round(yaw_cam / 90) * 90
    (negate the snapped yaw, because the transform undoes the camera rotation).
    """
    # Normalise to [-180, 180]
    yaw = ((yaw_cam_deg + 180) % 360) - 180
    snapped = round(yaw / 90) * 90
    return int(-snapped)


# ---------------------------------------------------------------------------
# Per-object transform
# ---------------------------------------------------------------------------


def _rotate_position(x: float, y: float, delta_rad: float) -> tuple[float, float]:
    c, s = math.cos(delta_rad), math.sin(delta_rad)
    return x * c - y * s, x * s + y * c


def transform_pred_object(
    obj: dict[str, Any],
    delta_deg: int,
) -> dict[str, Any]:
    """Apply the camera-to-world transform to a single predicted object dict.

    Only position (XY) and rotation_yaw are modified:
    - position is rotated by delta_deg around the room center (origin).
    - rotation_yaw is offset by delta_deg to convert from model axes to world axes.
    - dimensions are never modified: the model outputs them in the object's own
      canonical frame (yaw=0), which is independent of camera orientation.

    Z coordinate is never modified.
    Returns a shallow copy; does not modify the input dict.
    """
    obj = dict(obj)
    delta_rad = math.radians(delta_deg)

    # Position (XY only; Z kept as-is)
    pp = obj.get("position")
    if isinstance(pp, list) and len(pp) >= 2:
        x_w, y_w = _rotate_position(float(pp[0]), float(pp[1]), delta_rad)
        z = float(pp[2]) if len(pp) >= 3 else 0.0
        obj["position"] = [round(x_w, 4), round(y_w, 4), round(z, 4)]

    # Yaw: offset by delta to convert from model axes to world axes
    yaw = obj.get("rotation_yaw")
    if yaw is not None:
        obj["rotation_yaw"] = int((float(yaw) + delta_deg) % 360)

    return obj


# ---------------------------------------------------------------------------
# Scene-level transform
# ---------------------------------------------------------------------------


def transform_pred_objects(
    objects: list[dict[str, Any]],
    delta_deg: int,
    *,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Transform a list of predicted object dicts from camera frame to world frame."""
    transformed = []
    for obj in objects:
        tr = transform_pred_object(obj, delta_deg)
        if verbose:
            oid = obj.get("id", "?")
            op = obj.get("position", [])
            tp = tr.get("position", [])
            oy = obj.get("rotation_yaw")
            ty = tr.get("rotation_yaw")
            print(
                f"[camera_transform]   {oid}: pos {op} → {tp}  yaw {oy} → {ty}",
                file=sys.stderr,
            )
        transformed.append(tr)
    return transformed


def transform_pred_scene(
    pred: dict[str, Any],
    scene_dir: Path,
    *,
    verbose: bool = False,
) -> tuple[dict[str, Any], int | None]:
    """Transform all predicted objects in *pred* from camera frame to world frame.

    Loads camera metadata from ``<scene_dir>/metadata.json``, computes the
    snapped rotation delta, and applies the transform to every object in
    ``pred["scene"]["objects"]``.

    Returns
    -------
    (transformed_pred, delta_deg)
        ``transformed_pred`` is a deep-copy of *pred* with updated objects.
        ``delta_deg`` is the applied rotation (multiple of 90, or None if
        metadata was unavailable and the transform was skipped).

    If metadata is missing, *pred* is returned unchanged with a warning and
    ``delta_deg = None``.
    """
    cam_meta = load_camera_meta(scene_dir)
    if cam_meta is None:
        print(
            "[camera_transform] WARNING: metadata.json not found or camera euler "
            "unavailable — skipping coordinate transform. "
            "Predicted positions may be misaligned.",
            file=sys.stderr,
        )
        return pred, None

    yaw_deg = camera_yaw_deg(cam_meta)
    delta = snap_delta_deg(yaw_deg)

    if verbose:
        print(
            f"[camera_transform] camera yaw = {yaw_deg:.1f}°  "
            f"→ snapped delta = {delta}°",
            file=sys.stderr,
        )

    scene = pred.get("scene") or {}
    objects = scene.get("objects") or []
    if not isinstance(objects, list):
        return pred, delta

    transformed_objects = transform_pred_objects(
        objects, delta, verbose=verbose
    )

    result = copy.deepcopy(pred)
    result.setdefault("scene", {})["objects"] = transformed_objects

    return result, delta