"""
test_pipeline.py - Unit + probe tests for reconstruction middleware.

Covers two modules:

    benchmarks/camera_transform.py
    benchmarks/reconstruction/pose_inverse.py

Modes
-----
(1) Unit tests (default): run a fixed set of sanity cases and print PASS/FAIL.

        python benchmarks/tests/test_pipeline.py

(2) Probe mode: feed a hand-crafted prediction through the two modules and
    print intermediate values. Useful for debugging a real model output
    before running the full compose+render pipeline.

        python benchmarks/tests/test_pipeline.py --probe \
            --camera-rz 1.502 \
            --pred-position 1.0 2.0 0.5 \
            --pred-dimensions 1.5 0.5 0.8 \
            --pred-yaw 180 \
            --gt-position 1.05 2.1 0.5 \
            --gt-dimensions 1.5 0.5 0.8 \
            --orig-location 1.06 2.08 0.14 \
            --orig-dimensions 0.5 1.5 0.8 \
            --category bed

Log files
---------
When invoked with ``--log <path>``, stdout and stderr are tee'd into the
log file. Default log location (when --log is passed without a value) is
``benchmarks/tests/logs/<utc_timestamp>.log``.

Test invariants
---------------
(A) Identity roundtrip: pred == GT and ratios == 1 implies final_location
    == original_blender.location.  This is the definition of "inverse".

(B) Yaw exemption: yaw-exempt categories return final_rot_z = 0 and no
    W/D swap regardless of pred.rotation_yaw.

(C) W/D swap: non-exempt pred [W, D, H] -> final [D, W, H].

(D) Rotation mapping: final_rot_z = (pred_yaw + 90) mod 360 for non-exempt.

(E) Scale propagation: ratios are applied componentwise to offset_std
    before rotation.

(F) Camera yaw snap: any rz in [-45, +45] degrees from a cardinal direction
    produces the expected 90-degree snapped delta.

(G) Camera transform linearity: pred positions are rigidly rotated around
    the world origin by delta_deg; yaws are incremented by delta_deg.
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

# Make "benchmarks.xxx" importable regardless of where this file lives on disk.
_REPO_ROOT = Path(__file__).resolve().parents[2]   # .../IDEAL/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.camera_transform import (  # noqa: E402
    camera_yaw_deg,
    snap_delta_deg,
    transform_pred_object,
)
from benchmarks.reconstruction.pose_inverse import (  # noqa: E402
    invert_pose,
    is_yaw_exempt,
)


# ---------------------------------------------------------------------------
# Small assertion helpers
# ---------------------------------------------------------------------------


def _approx(a, b, tol=1e-4):
    if isinstance(a, list):
        return all(abs(x - y) < tol for x, y in zip(a, b))
    return abs(a - b) < tol


_passed: list[str] = []
_failed: list[tuple[str, str]] = []


def _run(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        _failed.append((name, str(exc)))
        print(f"FAIL {name}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        _failed.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        return
    _passed.append(name)
    print(f"PASS {name}")


# ---------------------------------------------------------------------------
# pose_inverse tests
# ---------------------------------------------------------------------------


def test_pose_identity_non_exempt():
    """(A) pred == GT, ratios = 1 -> final_location = orig_loc (non-exempt)."""
    gt = {
        "position":   [-1.12, 1.09, 0.59],
        "dimensions": [1.72, 2.39, 0.92],
        "rotation_yaw": 270,
    }
    pred = dict(gt)
    # Blender stores post-swap dimensions, so [D, W, H].
    orig = {
        "location":   [-1.10, 1.05, 0.56],
        "dimensions": [2.39, 1.72, 0.92],
    }
    out = invert_pose(pred, gt, orig, "bed")
    assert _approx(out["final_rotation_z"], 0.0), out["final_rotation_z"]
    assert _approx(out["final_dimensions"], [2.39, 1.72, 0.92]), out["final_dimensions"]
    assert _approx(out["final_location"], orig["location"]), (
        f"got {out['final_location']} vs expected {orig['location']}"
    )


def test_pose_identity_exempt():
    """(A) Same invariant for yaw-exempt (floor_lamp, no swap)."""
    gt = {
        "position":   [-0.91, -1.89, 0.80],
        "dimensions": [0.33, 0.33, 1.36],
        "rotation_yaw": None,
    }
    pred = dict(gt)
    orig = {
        "location":   [-0.91, -1.89, 0.14],
        "dimensions": [0.33, 0.33, 1.36],
    }
    out = invert_pose(pred, gt, orig, "floor_lamp")
    assert out["final_rotation_z"] == 0.0
    assert _approx(out["final_dimensions"], [0.33, 0.33, 1.36])
    assert _approx(out["final_location"], orig["location"])


def test_pose_exempt_flag():
    """(B) Exemption membership list stays in sync with scene_config."""
    assert is_yaw_exempt("floor_lamp")
    assert is_yaw_exempt("rug")
    assert is_yaw_exempt("large_plant_container")
    assert not is_yaw_exempt("bed")
    # table_dining family explicitly NOT exempt (scheme 1 convention)
    assert not is_yaw_exempt("coffee_table")
    assert not is_yaw_exempt("table_dining")
    assert not is_yaw_exempt("conference_table")


def test_pose_exempt_ignores_pred_yaw():
    """(B) Exempt categories must zero out rot_z even if pred sends a yaw."""
    gt = {"position": [-0.28, 0.03, 0.13], "dimensions": [3.22, 3.28, 0.02],
          "rotation_yaw": None}
    pred = {"position": [-0.28, 0.03, 0.13], "dimensions": [3.22, 3.28, 0.02],
            "rotation_yaw": 180}   # bogus yaw; must be ignored
    orig = {"location": [-0.28, 0.03, 0.13], "dimensions": [3.22, 3.28, 0.02]}
    out = invert_pose(pred, gt, orig, "rug")
    assert out["final_rotation_z"] == 0.0
    assert _approx(out["final_dimensions"], [3.22, 3.28, 0.02])


def test_pose_wd_swap_non_exempt():
    """(C) non-exempt pred [W, D, H] -> final [D, W, H]."""
    gt = {"position": [0, 0, 0.5], "dimensions": [0.8, 0.6, 0.7], "rotation_yaw": 0}
    pred = {"position": [0, 0, 0.5], "dimensions": [1.0, 0.5, 0.7], "rotation_yaw": 0}
    orig = {"location": [0, 0, 0.5], "dimensions": [0.6, 0.8, 0.7]}
    out = invert_pose(pred, gt, orig, "simple_desk")
    assert _approx(out["final_dimensions"], [0.5, 1.0, 0.7]), out["final_dimensions"]


def test_pose_rotation_mapping():
    """(D) final_rot_z = (pred_yaw + 90) mod 360 for non-exempt."""
    cases = [(0, 90), (90, 180), (180, 270), (270, 0), (45, 135)]
    for pred_yaw, expected in cases:
        gt = {"position": [0, 0, 0], "dimensions": [1, 1, 1], "rotation_yaw": 0}
        pred = {"position": [0, 0, 0], "dimensions": [1, 1, 1],
                "rotation_yaw": pred_yaw}
        orig = {"location": [0, 0, 0], "dimensions": [1, 1, 1]}
        out = invert_pose(pred, gt, orig, "bed")
        assert _approx(out["final_rotation_z"], expected), (
            f"pred_yaw={pred_yaw}: got {out['final_rotation_z']}, "
            f"expected {expected}"
        )


def test_pose_scale_propagation():
    """(E) ratios applied componentwise to offset_std (pre-rotation)."""
    gt = {"position": [0, 0, 0], "dimensions": [1, 1, 1], "rotation_yaw": None}
    # ratios = [2, 1, 1], exempt -> no swap, no rotation
    pred = {"position": [5, 5, 5], "dimensions": [2, 1, 1], "rotation_yaw": None}
    orig = {"location": [0.1, 0, 0], "dimensions": [1, 1, 1]}
    out = invert_pose(pred, gt, orig, "rug")
    # offset_std = [0.1, 0, 0]; scaled = [0.2, 0, 0]; final = pred_pos + scaled
    assert _approx(out["final_location"], [5.2, 5, 5])


def test_pose_rotation_rotates_offset():
    """(D)+(E) Non-exempt with pred_yaw=0 -> final_rot_z=90; offset gets rotated."""
    gt = {"position": [0, 0, 0], "dimensions": [1, 1, 1], "rotation_yaw": 0}
    pred = {"position": [0, 0, 0], "dimensions": [1, 1, 1], "rotation_yaw": 0}
    orig = {"location": [0.1, 0, 0], "dimensions": [1, 1, 1]}
    # swap applies (both dims are 1 so no numerical change); ratios = 1;
    # offset_std = [0.1, 0, 0]; rotated 90 deg CCW = [0, 0.1, 0];
    # final = pred_pos + rotated = [0, 0.1, 0].
    out = invert_pose(pred, gt, orig, "bed")
    assert _approx(out["final_rotation_z"], 90.0)
    assert _approx(out["final_location"], [0, 0.1, 0])


# ---------------------------------------------------------------------------
# camera_transform tests
# ---------------------------------------------------------------------------


def test_camera_yaw_cardinal_plus_y():
    """Camera at rz=0 looks +Y -> yaw=0, delta=0."""
    cam = {"rotation_euler": [0.0, 0.0, 0.0]}
    assert _approx(camera_yaw_deg(cam), 0.0)
    assert snap_delta_deg(0.0) == 0


def test_camera_yaw_cardinal_plus_x():
    """Camera pointing +X has rz = -pi/2 -> yaw = +90 -> delta = -90."""
    cam = {"rotation_euler": [0.0, 0.0, -math.pi / 2]}
    yaw = camera_yaw_deg(cam)
    assert _approx(yaw, 90.0), yaw
    assert snap_delta_deg(yaw) == -90


def test_camera_yaw_cardinal_minus_x():
    """Camera pointing -X has rz = +pi/2 -> yaw = -90 -> delta = +90."""
    cam = {"rotation_euler": [0.0, 0.0, math.pi / 2]}
    yaw = camera_yaw_deg(cam)
    assert _approx(yaw, -90.0), yaw
    assert snap_delta_deg(yaw) == 90


def test_camera_yaw_bedroom_case():
    """(F) The real bedroom_s14704319 case from camera_transform docstring."""
    cam = {"rotation_euler": [0.0, 0.0, 1.502]}
    yaw = camera_yaw_deg(cam)
    # Expected ~-86.1 per the docstring
    assert -90 < yaw < -80, yaw
    assert snap_delta_deg(yaw) == 90


def test_camera_transform_pred_object():
    """(G) delta=+90 rotates XY by 90 CCW and adds 90 to yaw."""
    pred = {"position": [1.0, 0.0, 0.5], "rotation_yaw": 0}
    out = transform_pred_object(pred, 90)
    assert _approx(out["position"][:2], [0.0, 1.0]), out["position"]
    assert out["rotation_yaw"] == 90

    # delta=-90: XY rotates 90 CW, yaw drops 90 -> wraps to 270
    pred2 = {"position": [0.0, 1.0, 0.5], "rotation_yaw": 0}
    out2 = transform_pred_object(pred2, -90)
    assert _approx(out2["position"][:2], [1.0, 0.0]), out2["position"]
    assert out2["rotation_yaw"] == 270


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------


def _run_probe(ns: argparse.Namespace) -> None:
    """Feed a hand-crafted pred through camera_transform + invert_pose.

    Prints the values at each stage so the user can diff them against
    expectations when a real prediction misbehaves.
    """
    print("=== PROBE MODE ===")
    print(f"camera.rotation_euler.z = {ns.camera_rz}")
    yaw = camera_yaw_deg({"rotation_euler": [0.0, 0.0, ns.camera_rz]})
    delta = snap_delta_deg(yaw)
    print(f"continuous camera yaw = {yaw:.3f} deg")
    print(f"snapped delta = {delta} deg (applied to pred)")

    pred_cam = {
        "position":   list(ns.pred_position),
        "dimensions": list(ns.pred_dimensions),
        "rotation_yaw": ns.pred_yaw,
    }
    print(f"\npred (camera frame):\n  position   = {pred_cam['position']}")
    print(f"  dimensions = {pred_cam['dimensions']}")
    print(f"  yaw        = {pred_cam['rotation_yaw']}")

    pred_world = transform_pred_object(pred_cam, delta)
    print(f"\npred (world frame, after delta={delta}):")
    print(f"  position   = {pred_world['position']}")
    print(f"  yaw        = {pred_world['rotation_yaw']}")

    gt = {
        "position":   list(ns.gt_position),
        "dimensions": list(ns.gt_dimensions),
        "rotation_yaw": ns.gt_yaw,
    }
    orig = {
        "location":   list(ns.orig_location),
        "dimensions": list(ns.orig_dimensions),
    }
    print(f"\nGT:\n  position   = {gt['position']}")
    print(f"  dimensions = {gt['dimensions']}")
    print(f"  yaw        = {gt['rotation_yaw']}")
    print(f"\noriginal (from scene.blend):")
    print(f"  location   = {orig['location']}")
    print(f"  dimensions = {orig['dimensions']}")

    # Copy dimensions because invert_pose only reads pred.dimensions from
    # the camera-frame dict (which is identical to world-frame dims).
    pred_world["dimensions"] = pred_cam["dimensions"]
    out = invert_pose(pred_world, gt, orig, ns.category)
    print(f"\ninvert_pose (category={ns.category}):")
    print(f"  final_location    = {out['final_location']}")
    print(f"  final_rotation_z  = {out['final_rotation_z']} deg")
    print(f"  final_dimensions  = {out['final_dimensions']}")

    if ns.category and not is_yaw_exempt(ns.category):
        # Floor-standing Z policy mirrored here for clarity.
        fl = out["final_location"]
        fl[2] = orig["location"][2]
        print(f"  after floor-Z pin = {fl}")


# ---------------------------------------------------------------------------
# Log tee
# ---------------------------------------------------------------------------


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


def _open_log(arg_value: str | None) -> Path | None:
    if arg_value is None:
        return None
    if arg_value == "":
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        default = Path(__file__).resolve().parent / "logs" / f"{ts}.log"
        default.parent.mkdir(parents=True, exist_ok=True)
        return default
    p = Path(arg_value).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--probe", action="store_true",
                   help="run a hand-crafted prediction through the modules "
                        "instead of the unit test suite")
    p.add_argument("--log", nargs="?", const="", default=None,
                   metavar="PATH",
                   help="tee stdout/stderr to a log file; with no value, "
                        "writes to benchmarks/tests/logs/<utc>.log")

    # Probe parameters.
    p.add_argument("--camera-rz", type=float, default=0.0)
    p.add_argument("--pred-position", type=float, nargs=3, default=[0, 0, 0])
    p.add_argument("--pred-dimensions", type=float, nargs=3, default=[1, 1, 1])
    p.add_argument("--pred-yaw", type=int, default=0)
    p.add_argument("--gt-position", type=float, nargs=3, default=[0, 0, 0])
    p.add_argument("--gt-dimensions", type=float, nargs=3, default=[1, 1, 1])
    p.add_argument("--gt-yaw", type=int, default=0)
    p.add_argument("--orig-location", type=float, nargs=3, default=[0, 0, 0])
    p.add_argument("--orig-dimensions", type=float, nargs=3, default=[1, 1, 1])
    p.add_argument("--category", default="bed")

    return p.parse_args()


def _run_all_unit_tests() -> int:
    tests = [
        ("test_pose_identity_non_exempt",    test_pose_identity_non_exempt),
        ("test_pose_identity_exempt",        test_pose_identity_exempt),
        ("test_pose_exempt_flag",            test_pose_exempt_flag),
        ("test_pose_exempt_ignores_pred_yaw", test_pose_exempt_ignores_pred_yaw),
        ("test_pose_wd_swap_non_exempt",     test_pose_wd_swap_non_exempt),
        ("test_pose_rotation_mapping",       test_pose_rotation_mapping),
        ("test_pose_scale_propagation",      test_pose_scale_propagation),
        ("test_pose_rotation_rotates_offset", test_pose_rotation_rotates_offset),
        ("test_camera_yaw_cardinal_plus_y",  test_camera_yaw_cardinal_plus_y),
        ("test_camera_yaw_cardinal_plus_x",  test_camera_yaw_cardinal_plus_x),
        ("test_camera_yaw_cardinal_minus_x", test_camera_yaw_cardinal_minus_x),
        ("test_camera_yaw_bedroom_case",     test_camera_yaw_bedroom_case),
        ("test_camera_transform_pred_object", test_camera_transform_pred_object),
    ]
    for name, fn in tests:
        _run(name, fn)

    print()
    print(f"Summary: passed={len(_passed)} failed={len(_failed)}")
    if _failed:
        for name, msg in _failed:
            print(f"  FAIL {name}: {msg}")
        return 1
    return 0


def main() -> int:
    args = _parse_args()
    log_path = _open_log(args.log)

    log_file = None
    stdout_ctx = None
    stderr_ctx = None
    if log_path is not None:
        log_file = open(log_path, "w", encoding="utf-8")
        stdout_ctx = redirect_stdout(_Tee(sys.stdout, log_file))
        stderr_ctx = redirect_stderr(_Tee(sys.stderr, log_file))
        stdout_ctx.__enter__()
        stderr_ctx.__enter__()
        print(f"[test_pipeline] logging to {log_path}")

    try:
        if args.probe:
            _run_probe(args)
            return 0
        return _run_all_unit_tests()
    finally:
        if stderr_ctx is not None:
            stderr_ctx.__exit__(None, None, None)
        if stdout_ctx is not None:
            stdout_ctx.__exit__(None, None, None)
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    sys.exit(main())