"""
单场景 Blender 内管线：``GT.json``、``info.json``、PNG / npz 清理。

仅在带 ``bpy`` 的子进程中运行（由 ``core`` 启动 ``preprocess.py --blend`` 触发）。
"""

from __future__ import annotations

import glob
import os
import sys
from typing import Any, Dict, Optional

from .defaults import CANONICAL_RENDER_PNG, PUBLISH_RENDER_PNG
from .core import _generation_dir, _list_png_files_in_scene_dir


def _scene_output_dir(blend_path: str) -> str:
    return os.path.dirname(os.path.abspath(blend_path))


def run_pipeline(
    blend_path: str,
    write_semantics: bool = True,
    compute_overlap: bool = True,
) -> Dict[str, Any]:
    import bpy

    generation_dir = _generation_dir()
    if generation_dir not in sys.path:
        sys.path.insert(0, generation_dir)

    from .extract_gt import resolve_room_metrics_box, write_gt_json
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
            _, overlap_pc = write_info_json_v2(
                output_dir, cam, compute_overlap=compute_overlap
            )
            result["overlap_pair_count"] = overlap_pc
            if overlap_pc is not None and overlap_pc > 0:
                print(
                    f"[WARN] furniture_overlap: pair_count={overlap_pc} (see info.json)"
                )
        else:
            print("[WARN] no render camera, skip info.json")

    _remove_npz_files(output_dir)
    _normalize_render_png_filename(output_dir)

    result["ok"] = True
    print("\n[DONE] Preprocessing complete.\n")
    return result


def _normalize_render_png_filename(output_dir: str) -> None:
    """
    优先将 ``Image_0_0_0001_0.png`` 或首个 ``Image_*.png`` 规范为 ``image.png``，并删其余 ``Image_*.png``。
    若无 ``Image_*.png`` 而已有 ``image.png`` 则不动。
    否则在目录内任选一个 ``.png``（按文件名排序）重命名为 ``image.png``（与 Infinigen 导出名无关亦可）。
    """
    dest = os.path.join(output_dir, PUBLISH_RENDER_PNG)
    pattern = os.path.join(output_dir, "Image_*.png")
    candidates = sorted(glob.glob(pattern))
    if candidates:
        preferred = os.path.join(output_dir, CANONICAL_RENDER_PNG)
        src = preferred if preferred in candidates else candidates[0]
        try:
            if os.path.abspath(src) != os.path.abspath(dest):
                if os.path.isfile(dest):
                    os.unlink(dest)
                os.replace(src, dest)
                print(f"[pipeline] render PNG -> {PUBLISH_RENDER_PNG}")
        except OSError as e:
            print(f"[WARN] could not rename render PNG to {PUBLISH_RENDER_PNG}: {e}")
            return
        for p in glob.glob(pattern):
            try:
                if os.path.isfile(p):
                    os.unlink(p)
                    print(f"[pipeline] removed {os.path.basename(p)}")
            except OSError as e:
                print(f"[WARN] could not remove {p}: {e}")
        return

    if os.path.isfile(dest):
        return
    pngs = _list_png_files_in_scene_dir(output_dir)
    if not pngs:
        return
    src = pngs[0]
    try:
        if os.path.abspath(src) != os.path.abspath(dest):
            os.replace(src, dest)
            print(
                f"[pipeline] render PNG -> {PUBLISH_RENDER_PNG} (from {os.path.basename(src)})"
            )
    except OSError as e:
        print(f"[WARN] could not rename render PNG to {PUBLISH_RENDER_PNG}: {e}")


def _remove_npz_files(output_dir: str) -> None:
    """删除目录内 *.npz（相机矩阵已写入 info.json 后不再需要）。"""
    pattern = os.path.join(output_dir, "*.npz")
    for p in glob.glob(pattern):
        try:
            os.unlink(p)
            print(f"[pipeline] removed {os.path.basename(p)}")
        except OSError as e:
            print(f"[WARN] could not remove {p}: {e}")


# 兼容旧名（本模块内曾叫 _run_pipeline）
_run_pipeline = run_pipeline
