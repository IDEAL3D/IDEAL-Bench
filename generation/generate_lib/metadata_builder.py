#!/usr/bin/env python3
"""
从 scene.blend、Objects*.json 构建 ``metadata.json`` 字典（供 ``core`` 等 ``json.dumps`` 落盘）。

相机：仅 ``blender_dump_metadata.py`` 子进程或当前进程 ``bpy``；失败则抛错，不写占位数据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.infinigen_paths import INFINIGEN_IDL3D_ROOT  # noqa: E402

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent

RENDER_ROTATION_UNIT = "radians"
RENDER_ROTATION_ORDER = "XYZ"


def _attach_render_rotation_fields(render: dict) -> dict:
    out = dict(render)
    out["rotation_unit"] = RENDER_ROTATION_UNIT
    out["rotation_order"] = RENDER_ROTATION_ORDER
    return out


def build_instance_map_block(object_index_map: dict[str, str]) -> dict:
    """Per-scene ``instance_map`` payload；字段语义见 ``docs/METADATA_SCHEMA.md``。"""
    return {
        "format": "grayscale_uint16",
        "bits_per_channel": 16,
        "background_value": 0,
        "object_index_map": object_index_map,
    }


def _object_index_map_from_objects_json(data: dict) -> dict[str, str]:
    """由 Infinigen ``Objects*.json`` 构造 ``object_index_map``：凡 ``object_index > 0`` 的物体均写入，不做关键词筛选。"""
    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    for name in sorted(data.keys()):
        info = data[name]
        if not isinstance(info, dict):
            continue
        oi = info.get("object_index")
        if oi is None:
            continue
        idx = int(oi)
        if idx <= 0:
            continue
        base = re.sub(r"[^a-zA-Z0-9]+", "_", str(name)).strip("_").lower() or "object"
        counts[base] = counts.get(base, 0) + 1
        n = counts[base]
        label = f"{base}_{n:03d}"
        out[str(idx)] = label
    return out


def _find_blender_executable() -> str:
    override = os.environ.get("BLENDER")
    if override and Path(override).is_file():
        return override
    import shutil

    w = shutil.which("blender")
    if w:
        return w
    ex = sys.executable or ""
    if "blender" in ex.lower():
        return ex
    raise RuntimeError(
        "未找到 blender 可执行文件：请将 blender 加入 PATH，或设置环境变量 BLENDER=/path/to/blender"
    )


def _run_blender_dump_render_metadata(blend_path: Path, out_json: Path) -> None:
    dump_script = SCRIPT_DIR / "blender_dump_metadata.py"
    if not dump_script.is_file():
        raise FileNotFoundError(f"缺少 {dump_script}")
    blender = _find_blender_executable()
    cmd = [
        blender,
        "-b",
        str(blend_path),
        "--python",
        str(dump_script),
        "--",
        str(out_json),
    ]
    logger.info("[CMD] %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(INFINIGEN_IDL3D_ROOT))


def _render_metadata_from_bpy_open(blend_path: Path) -> dict | None:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        bpy.ops.wm.open_mainfile(filepath=str(blend_path))
        cam = bpy.context.scene.camera
        if cam is None:
            return None
        loc, rot_quat, _sca = cam.matrix_world.decompose()
        euler = rot_quat.to_euler(RENDER_ROTATION_ORDER)
        return {
            "resolution": [
                int(bpy.context.scene.render.resolution_x),
                int(bpy.context.scene.render.resolution_y),
            ],
            "camera_location": [float(loc.x), float(loc.y), float(loc.z)],
            "camera_rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
            "focal_length_mm": float(cam.data.lens),
            "sensor_width": float(cam.data.sensor_width),
        }
    except Exception as e:
        logger.warning("bpy 打开 scene.blend 读取相机失败: %s", e)
        return None


def resolve_render_metadata(blend_path: Path, args) -> dict:
    """仅从 blend 读取相机：先 ``blender_dump_metadata.py`` 子进程，再当前进程 ``bpy``；均失败则抛错。"""
    blend_path = Path(blend_path)
    tmp_json = blend_path.parent / ".camera_fragment.json"
    err_sub: Exception | None = None
    try:
        _run_blender_dump_render_metadata(blend_path, tmp_json)
        meta = json.loads(tmp_json.read_text(encoding="utf-8"))
        logger.info("metadata.render：blender 子进程 dump（含 sensor_width）")
        return _attach_render_rotation_fields(meta)
    except Exception as e:
        err_sub = e
        logger.warning("blender 子进程导出相机失败: %s", e)
    finally:
        tmp_json.unlink(missing_ok=True)

    meta = _render_metadata_from_bpy_open(blend_path)
    if meta is not None:
        logger.info("metadata.render：当前进程 bpy")
        return _attach_render_rotation_fields(meta)

    raise RuntimeError(
        f"无法读取相机 metadata（子进程与 bpy 均失败），blend={blend_path}；"
        f"子进程错误: {err_sub!r}"
    )


def build_metadata(
    scene_id: str,
    canonical_room_type: str,
    blend_path: Path,
    objects_json_path: Path,
    args,
) -> dict:
    """主入口：返回完整 metadata 字典，供调用方 ``json.dumps``。"""
    data = json.loads(Path(objects_json_path).read_text(encoding="utf-8"))
    index_map = _object_index_map_from_objects_json(data)
    render_meta = resolve_render_metadata(Path(blend_path), args)
    return {
        "scene_id": scene_id,
        "room_type": canonical_room_type,
        "render": render_meta,
        "instance_map": build_instance_map_block(index_map),
    }
