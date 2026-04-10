"""
Single-scene Blender pipeline: ``GT.json``, ``objects_in_view.json`` (with furniture overlap summary), npz cleanup.

Runs only in a subprocess where ``bpy`` is available (``core`` invokes ``preprocess.py --blend``).
"""

from __future__ import annotations

import glob
import os
import sys
from typing import Any, Dict, Optional

from .core import _generation_dir
from .defaults import OBJECTS_IN_VIEW_JSON


def _scene_output_dir(blend_path: str) -> str:
    return os.path.dirname(os.path.abspath(blend_path))


def run_pipeline(
    blend_path: str,
    write_semantics: bool = True,
) -> Dict[str, Any]:
    import bpy

    generation_dir = _generation_dir()
    if generation_dir not in sys.path:
        sys.path.insert(0, generation_dir)

    from generate_lib.extract_gt import resolve_room_metrics_box, write_gt_json
    from .extract_info import write_info_json_v2

    result: Dict[str, Any] = {
        "ok": False,
        "scene_label": "",
        "output_dir": "",
        "error": None,
        "overlap_pair_count": None,
    }

    abs_blend = os.path.abspath(blend_path)
    output_dir = _scene_output_dir(abs_blend)
    result["output_dir"] = output_dir
    result["scene_label"] = os.path.basename(output_dir)

    if bpy.data.filepath != abs_blend:
        bpy.ops.wm.open_mainfile(filepath=abs_blend)

    print(f"\n[pipeline] blend : {abs_blend}")
    print(f"[pipeline] output: {output_dir}")

    metrics = resolve_room_metrics_box()
    if metrics is None:
        print("[ERROR] could not resolve room metrics.")
        result["error"] = "room_metrics_unresolved"
        return result

    write_gt_json(output_dir, metrics)

    overlap_pc: Optional[int] = None
    if write_semantics:
        cam = metrics.get("cam_obj")
        if cam is not None:
            _, overlap_pc = write_info_json_v2(output_dir)
            result["overlap_pair_count"] = overlap_pc
            if overlap_pc is not None and overlap_pc > 0:
                print(
                    f"[WARN] furniture_overlap: pair_count={overlap_pc} "
                    f"(see {OBJECTS_IN_VIEW_JSON})"
                )
        else:
            print("[WARN] no render camera, skip objects_in_view.json")

    _remove_npz_files(output_dir)

    result["ok"] = True
    print("\n[DONE] Preprocessing complete.\n")
    return result


def _remove_npz_files(output_dir: str) -> None:
    """Delete all ``*.npz`` files directly under ``output_dir``."""
    pattern = os.path.join(output_dir, "*.npz")
    for p in glob.glob(pattern):
        try:
            os.unlink(p)
            print(f"[pipeline] removed {os.path.basename(p)}")
        except OSError as e:
            print(f"[WARN] could not remove {p}: {e}")
