#!/usr/bin/env python
"""
矩形单房间场景生成脚本。

通过 infinigen_examples.single 生成指定类型、指定尺寸的矩形室内场景。
输出目录：<INFINIGEN>/SCENES_SKETCH/{roomtype}_s{seed}/（默认），例如 bedroom_s12345（住宅类不再含尺寸档位名）。
默认父目录用于**临时试跑、调参、改场景**；正式批量数据集请用 ``generate.py --scenes-raw`` → ``SCENES_RAW``。
批量场景父目录可用 ``--scenes-root`` 改为其它路径（如 …/SCENES_RAW）。

每个场景目录 ``{roomtype}_s{seed}`` 最终仅保留四样：

- ``scene.blend`` — Blender 源文件
- ``color.png`` — 正常 RGB（原 ``Image_*.png``）
- ``instance_segmentation.png`` — Object Index pass，灰度 uint8（背景为 0）
- ``metadata.json`` — 场景 id、房型、相机与实例索引表（object_index → 语义 id）

流程：先 ``coarse``+``render`` 出 RGB；在清理前再跑一趟 ``@flat/render_image`` 仅用于 Object Index；
再写 ``metadata.json``（优先当前进程 ``bpy`` 打开 ``scene.blend`` 读相机；否则 ``blender -b`` 子进程；
再否则用 ``camview*.npz`` 推算）。最后删至上述四文件。

运行示例:
    # 默认 1 个场景，中等尺寸
    python generate_box.py

    # 100 个小房间（单档位批量）
    python generate_box.py --num-scenes 3 --room-size large

    # 小中大各 3 个，共 9 个（混合档位）
    python generate_box.py --room-size-mix small:3,medium:3,large:3

    # 客厅大尺寸，输出目录如 SCENES_SKETCH/livingroom_large_s42
    python generate_box.py --room-type living-room --room-size large

    # 指定起始 seed、自定义 overrides
    python generate_box.py --start-seed 42 --num-scenes 5 --room-size large
    python generate_box.py --overrides "single_room_floor_plan.room_type='kitchen'" "compose_single_room.room_type='kitchen'"

房间类型（--room-type，可选）:
    住宅共 6 类：bedroom, homestudio, living-room, kitchen, bathroom, dining-room；默认 bedroom。
    homestudio 对应原 large 卧室（≥36 m² 家具档）。

房间尺寸档位（--room-size / --room-size-mix）:
    - **仅非住宅类房型**可用：由用户指定 small/medium/large(/xlarge)。
    - **住宅类 6 种**：不在命令行选档；厨房/洗手间/饭厅固定单档；bedroom 与 living-room 在 small/medium
      两档间按 **50%**（由每个场景的 seed 可复现随机）；homestudio 固定大单间尺寸。
    - **working_space 四类**（open-office / meeting-room / classroom / library）均为 **三档**（small / medium / large）；
      未指定 ``--room-size`` 时，**每个场景**在三档间 **各 1/3** 随机（同一批 ``--num-scenes N`` 内各场景独立抽样，非整批同一档）。
    运行时用「可用档位」校验：该房型不支持的档位会报错。

    本仓库当前入口**仅支持** living_space 6 类 + working_space 4 类（共 10 种 ``room-type``），不含商业空间。

灯光调参（--light-multiplier）:
    控制室内灯光（顶灯 + 台灯 + 落地灯）的功率倍率。
    Infinigen 默认每盏灯 40-100W，渲染曝光 exposure=3（8 倍亮度放大，来自 base_indoors.gin）。
    单房间场景光反射弱于原版多房间 demo，需要额外提升灯光功率来补偿。

    参数值      效果              适用场景
    ────────    ──────────────    ────────────────────────────────
    1.0         原始功率，较暗    模拟夜晚 / 仅靠窗光的氛围
    3.0         偏暗，柔和        傍晚 / 暖光氛围
    5.0 (默认)  与原版 demo 接近  日常室内，推荐 baseline
    8.0         明亮              白天充足光照 / 展示用途
    10.0        非常亮            商业样板间 / 过曝风格

    示例:
        python generation/generate_box.py --light-multiplier 5    # baseline，与 Infinigen 样板房一致
        python generation/generate_box.py --light-multiplier 8    # 更亮
        python generation/generate_box.py --light-multiplier 3    # 偏暗氛围

    也可通过 --overrides 精细控制:
        --overrides "configure_render_cycles.exposure=4"    # 提高渲染曝光（2^4=16 倍）
        --overrides "nishita_lighting.strength=0.4"         # 增强天空/环境光

输出分辨率（--render-width）:
    默认 1280 → 1280×720（与 base.gin 一致）；``640`` → 640×360，通过 gin 覆盖
    ``execute_tasks.generate_resolution`` 与 ``get_sensor_coords.W/H``。

渲染采样数（--render-samples）与去噪（--denoise）:
    base.gin 默认 8192 samples（极高，室内场景容易 OOM）。
    本脚本默认 128 samples + 开启去噪（兼顾质量与速度）。

    参数值      去噪    效果                适用场景
    ────────    ─────   ──────────────────  ────────────────────────
    128 (默认)  开      快速出图            测试阶段 / 快速预览
    256         开      高质量              日常生成
    512         关      较高质量            不想用去噪时
    1024        关      高质量              追求低噪点
    8192        关      原始默认            极慢，显存需求极大

    示例:
        python generation/generate_box.py                                       # 默认 128+denoise
        python generation/generate_box.py --render-samples 256                  # 更高质量
        python generation/generate_box.py --render-samples 512 --no-denoise     # 不去噪
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from imageio import imwrite

from infinigen_paths import INFINIGEN_IDL3D_ROOT, SCENES_SKETCH_ROOT

SCRIPT_DIR = Path(__file__).resolve().parent

# 默认场景父目录：试跑 / 调参与改结果（非正式 RAW 数据集目录，位于同级 INFINIGEN 仓库）

logging.basicConfig(
    format="[%(asctime)s.%(msecs)03d] [%(module)s] [%(levelname)s] | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# 与 generate.py 一致：固定墙厚；地板网格挤出约为该值的一半（BlueprintSolidifier SOLIDIFY）。
# 设为 None 则不在此注入 gin（可与 generate.py 分叉，独立跑本脚本时注意）。
FIXED_WALL_THICKNESS_M: float | None = 0.25

# 房间净空（墙高）：按面积档位对 RoomConstants.global_params.wall_height 注入 uniform 区间。
# 未列档位不注入，沿用 gin 默认 ("uniform", 2.8, 3.2)。
WALL_HEIGHT_UNIFORM_BY_TIER: dict[str, tuple[float, float]] = {
    "small": (2.8, 3.2),
    "medium": (3.0, 4.0),
    "large": (4.0, 6.0),
    # 商业 xlarge、住宅 homestudio(studio) 无单独约定时与 large 同档净空
    "xlarge": (4.0, 6.0),
    "studio": (4.0, 6.0),
}

# ---------------------------------------------------------------------------
# 房间尺寸档位  (min_w, max_w, min_d, max_d) 米
# ---------------------------------------------------------------------------
# 通用 fallback（未来新增 room-type 且未写入 ROOM_SIZE_TIERS 时使用）：仅三档
_DEFAULT_SIZE_TIERS: dict[str, tuple[float, float, float, float]] = {
    "small":  (2.5, 3.5, 2.5, 3.5),
    "medium": (3.5, 6.0, 3.5, 6.0),
    "large":  (6.0, 8.0, 6.0, 8.0),
}

# 按房间类型的专属尺寸档位。
# 住宅 living_space（6 类）：厨房/洗手间/饭厅仅固定一档；卧室与客厅各两档由脚本按 seed 50% 抽样；
# homestudio 为原 large 卧室（≥36 m² 家具档），由用户显式选择房型。
ROOM_SIZE_TIERS: dict[str, dict[str, tuple[float, float, float, float]]] = {
    # --- 住宅 living_space ---
    "bedroom": {
        "small":  (3.0, 3.9, 3.0, 3.9),   # <16 m² 平均 → 单人床
        "medium": (4.0, 5.5, 4.0, 5.5),   # 16–36 m² 平均 → 双人床
    },
    "homestudio": {
        "studio": (5.0, 7.0, 5.0, 7.0),   # 原 large 卧室 / home-studio
    },
    "living-room": {
        "small":  (3.0, 5.0, 3.0, 5.0),
        "medium": (4.0, 8.0, 4.0, 8.0),
    },
    "kitchen": {
        "medium": (3.2, 5.0, 3.2, 5.0),
    },
    "bathroom": {
        "medium": (2.5, 3.2, 2.5, 3.2),
    },
    "dining-room": {
        "medium": (3.5, 5.5, 3.5, 5.5),
    },
    # --- 大空间 ---
    "open-office": {
        "small":  (3.0, 4.0, 3.0, 4.0),        # ~9-16 m²   单人办公室
        "medium": (4.0, 6.0, 4.0, 6.0),        # ~16-36 m²  普通多人办公室
        "large":  (6.0, 8.0, 6.0, 8.0),        # ~36-64 m²  普通多人办公室
    },
    "meeting-room": {
        "small":  (3.5, 5.0, 3.0, 4.5),        # ~10-22 m²
        "medium": (5.0, 7.0, 4.0, 5.5),        # ~20-38 m²
        "large":  (7.0, 10.0, 5.5, 7.0),       # ~38-70 m²
    },
    "classroom": {
        "small":  (6.0, 6.5,  8.0, 8.5),       # 3 组 × 6-7 行 ≈ 36-42 座
        "medium": (6.0, 7.0,  8.5, 9.5),       # 3 组 × 7-8 行 ≈ 42-48 座（标准）
        "large":  (6.5, 7.5,  9.0, 10.0),      # 3-4 组 × 7-8 行 ≈ 42-64 座
    },
    # (min_w, max_w, min_d, max_d)；与 ../docs/WORKING_SPACE_DESIGN.md §2.4、§3.4 一致
    "library": {
        "small":  (7.5, 10.0,  6.0, 9.5),       # 小型阅览室；目标块数与排数见 working_space._place_library_grid
        "medium": (10.0, 15.0,  9.5, 16.0),     # 普通图书馆
        "large":  (16.0, 30.0, 18.0, 36.0),     # 大型图书馆（由原 large/xlarge 合并上移）
    },
}

# 所有可能的档位名（取各房间类型档位名的并集）
ALL_SIZE_TIER_NAMES: list[str] = sorted(
    set(_DEFAULT_SIZE_TIERS)
    | {t for tiers in ROOM_SIZE_TIERS.values() for t in tiers},
    key=lambda x: list(_DEFAULT_SIZE_TIERS).index(x) if x in _DEFAULT_SIZE_TIERS else 99,
)


def get_available_tiers(room_type: str) -> list[str]:
    """返回指定房间类型的所有可用档位名。"""
    room_tiers = ROOM_SIZE_TIERS.get(room_type, {})
    if room_tiers:
        return list(room_tiers)
    return list(_DEFAULT_SIZE_TIERS)


def _is_living_space_room(room_type: str) -> bool:
    return room_type in LIVING_SPACE_ROOM_TYPES


def resolve_living_space_tier(room_type: str, seed: int) -> str:
    """住宅类：每个场景一个内部档位（写入 gin size_override，不出现在输出目录名）。"""
    rng = random.Random(seed)
    if room_type in ("kitchen", "bathroom", "dining-room"):
        return "medium"
    if room_type == "homestudio":
        return "studio"
    if room_type in ("bedroom", "living-room"):
        return rng.choice(["small", "medium"])
    raise ValueError(f"非住宅 living_space 房型: {room_type!r}")


def get_size_for_room(
    room_type: str, tier: str
) -> tuple[float, float, float, float]:
    """查询房间的尺寸档位。有专属定义时只查专属；否则回退到默认。"""
    avail = get_available_tiers(room_type)
    if tier not in avail:
        raise ValueError(
            f"房间类型 '{room_type}' 不支持档位 '{tier}'。可用: {avail}"
        )
    room_tiers = ROOM_SIZE_TIERS.get(room_type, {})
    if tier in room_tiers:
        return room_tiers[tier]
    return _DEFAULT_SIZE_TIERS[tier]
# 住宅（living_space）六种房型：用户只选 room-type；卧室/客厅内在两档间 50% 抽样（由 seed 决定）。
LIVING_SPACE_ROOM_TYPES: frozenset[str] = frozenset(
    {
        "bedroom",
        "homestudio",
        "living-room",
        "kitchen",
        "bathroom",
        "dining-room",
    }
)

# 办公/教学（working_space）：与 generate.py 一致；未指定 --room-size 时每场景 small/medium/large 各 1/3
WORKING_SPACE_ROOM_TYPES: frozenset[str] = frozenset(
    {
        "open-office",
        "meeting-room",
        "classroom",
        "library",
    }
)

# 房间类型 → GIN 配置文件 & 约束模块 映射
ROOM_CONFIG_MAP = {
    # 住宅 (living_space)
    "bedroom":     "living_space",
    "homestudio":  "living_space",
    "living-room": "living_space",
    "kitchen":     "living_space",
    "bathroom":    "living_space",
    "dining-room": "living_space",
    # 办公/教学 (working_space)
    "open-office":  "working_space",
    "meeting-room": "working_space",
    "classroom":    "working_space",
    "library":      "working_space",
}
ROOM_TYPES = tuple(ROOM_CONFIG_MAP.keys())

def _gin_module_for_room(room_type: str) -> str:
    """Return the constraint module path for the given room type."""
    config_name = ROOM_CONFIG_MAP[room_type]
    return f"infinigen_examples.constraints.{config_name}"

SIZE_OVERRIDE_GIN_TEMPLATE = "{module}.floor_plan.size_override"
FURNITURE_SIZE_OVERRIDE_GIN_TEMPLATE = "{module}.furniture_constraints.size_override"


def get_render_gpu() -> str:
    """选择用于渲染的 GPU 编号。默认 2；后续可接入智能选择模块。"""
    # TODO: 智能选择 GPU 模块
    return "2"


def _room_folder_name(room_type: str) -> str:
    """用于目录名的房型标识，如 living-room -> livingroom。"""
    return room_type.replace("-", "")


def _size_override_gin_value(tup: tuple[float, float, float, float]) -> str:
    """返回可传入 gin overrides 的 size_override 字符串。"""
    return f"({tup[0]},{tup[1]},{tup[2]},{tup[3]})"


def _parse_room_size_mix(mix_str: str) -> list[str]:
    """
    解析 --room-size-mix，例如 "small:3,medium:3,large:3" -> [small,small,small, medium,medium,medium, large,large,large]。
    返回按顺序的档位列表，长度为总场景数。
    """
    out = []
    for part in mix_str.strip().split(","):
        part = part.strip()
        if ":" not in part:
            raise ValueError(f"room-size-mix 每项须为 档位:数量，例如 small:3，当前: {part!r}")
        name, count_str = part.split(":", 1)
        name = name.strip().lower()
        if name not in ALL_SIZE_TIER_NAMES:
            raise ValueError(f"未知档位 {name!r}，可选: {ALL_SIZE_TIER_NAMES}")
        try:
            n = int(count_str.strip())
        except ValueError:
            raise ValueError(f"room-size-mix 数量须为整数: {count_str!r}")
        if n < 1:
            raise ValueError(f"room-size-mix 数量须 >= 1: {n}")
        out.extend([name] * n)
    return out


def _run(cmd: list[str], gpu: str | None = None) -> None:
    """在 ``IDL3D/infinigen-idl3d`` 根目录下运行子进程命令。"""
    logger.info("[CMD] %s", " ".join(cmd))
    env = None
    if gpu is not None:
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = gpu
        logger.info("CUDA_VISIBLE_DEVICES=%s", gpu)
    subprocess.run(cmd, check=True, cwd=str(INFINIGEN_IDL3D_ROOT), env=env)


FINAL_KEEP_NAMES = frozenset(
    {"scene.blend", "color.png", "instance_segmentation.png", "metadata.json"}
)


def _find_blender_executable() -> str:
    """用于 ``blender -b scene.blend --python ...``（无 bpy 的宿主 Python 无法读相机）。"""
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


def _mat_to_euler_xyz(R: np.ndarray) -> tuple[float, float, float]:
    """世界→相机旋转矩阵的 XYZ 欧拉角（与 npz 回退配套，非 Blender 原生 euler）。"""
    R = np.asarray(R, dtype=np.float64)
    sy = math.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])
    singular = sy < 1e-6
    if not singular:
        x = math.atan2(R[2, 1], R[2, 2])
        y = math.atan2(-R[2, 0], sy)
        z = math.atan2(R[1, 0], R[0, 0])
    else:
        x = math.atan2(-R[1, 2], R[1, 1])
        y = math.atan2(-R[2, 0], sy)
        z = 0.0
    return (x, y, z)


def _render_metadata_from_camview_npz(output_folder: Path) -> dict | None:
    """从 Infinigen 首趟渲染生成的 ``camview*.npz``（K, T, HW）推算 ``render`` 块。"""
    npzs = sorted(output_folder.glob("camview*.npz"))
    if not npzs:
        return None
    z = np.load(npzs[0])
    K = np.asarray(z["K"], dtype=np.float64)
    T = np.asarray(z["T"], dtype=np.float64)
    HW = np.asarray(z["HW"]).reshape(-1)
    if HW.size < 2:
        return None
    H, W = int(HW[0]), int(HW[1])
    fx = float(K[0, 0])
    sensor_mm = 36.0
    focal_mm = fx * sensor_mm / float(W)
    R = T[:3, :3]
    t = T[:3, 3]
    cam_center = (-R.T @ t.reshape(3, 1)).reshape(3)
    ex, ey, ez = _mat_to_euler_xyz(R)
    return {
        "resolution": [W, H],
        "camera_location": [float(cam_center[0]), float(cam_center[1]), float(cam_center[2])],
        "camera_rotation_euler": [ex, ey, ez],
        "focal_length_mm": focal_mm,
        "sensor_width": sensor_mm,
    }


def _render_metadata_minimal_from_args(args) -> dict:
    rw = getattr(args, "render_width", 1280)
    if rw == 640:
        w, h = 640, 360
    else:
        w, h = 1280, 720
    return {
        "resolution": [w, h],
        "camera_location": [0.0, 0.0, 0.0],
        "camera_rotation_euler": [0.0, 0.0, 0.0],
        "focal_length_mm": 0.0,
        "sensor_width": 0.0,
    }


def _render_metadata_from_bpy_open(blend_path: Path) -> dict | None:
    """当前解释器已链接 bpy 时（如 ``blender --python generate_box.py``），直接打开 blend 读相机。"""
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
        euler = rot_quat.to_euler()
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


def resolve_render_metadata(blend_path: Path, output_folder: Path, args) -> dict:
    """写入 metadata 所需的 ``render`` 对象；多路回退，保证总能得到字典。"""
    meta = _render_metadata_from_bpy_open(blend_path)
    if meta is not None:
        logger.info("metadata.render：自当前进程 bpy 读取 scene.blend")
        return meta

    try:
        cam_fragment = output_folder / ".camera_fragment.json"
        _run_blender_dump_render_metadata(blend_path, cam_fragment)
        meta = json.loads(cam_fragment.read_text(encoding="utf-8"))
        cam_fragment.unlink(missing_ok=True)
        logger.info("metadata.render：独立 blender 子进程写出")
        return meta
    except Exception as e:
        logger.warning("独立 blender 导出相机失败（可忽略若后续 npz 成功）: %s", e)
        cam_fragment = output_folder / ".camera_fragment.json"
        cam_fragment.unlink(missing_ok=True)

    meta = _render_metadata_from_camview_npz(output_folder)
    if meta is not None:
        logger.info("metadata.render：自 camview*.npz 推算（sensor_width 按 36mm 全画幅由 K 反推 focal）")
        return meta

    logger.warning(
        "metadata.render：无 bpy 且无 camview*.npz，使用占位分辨率；请检查环境或重新渲染"
    )
    return _render_metadata_minimal_from_args(args)


def _object_index_map_from_objects_json(data: dict) -> dict[str, str]:
    """Objects*.json：物体名为键，object_index 为 Blender Object Index。"""
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


def _build_gt_object_index_command(
    seed: int,
    input_folder: Path,
    gt_output_folder: Path,
    args,
) -> list[str]:
    """第二趟：flat / Object Index，输出至单独子目录，避免覆盖 color.png。"""
    w, h = (640, 360) if args.render_width == 640 else (1280, 720)
    cmd: list[str] = [
        sys.executable,
        "-m",
        "infinigen_examples.single",
        "--seed",
        f"s{seed}",
        "--task",
        "render",
        "--input_folder",
        str(input_folder),
        "--output_folder",
        str(gt_output_folder),
        "--configs",
        *args.configs,
        "--overrides",
        "render.render_image_func=@flat/render_image",
        "flat/render_image.override_num_samples=16",
        "flat/render_image.skip_reorganize_frames=True",
        f"execute_tasks.generate_resolution=({w}, {h})",
        f"get_sensor_coords.W={w}",
        f"get_sensor_coords.H={h}",
    ]
    # 勿并入用户 --overrides，以免覆盖 render.render_image_func（必须为 flat）
    if args.task_uniqname is not None:
        cmd += ["--task_uniqname", args.task_uniqname]
    if args.debug:
        cmd += ["--debug", *args.debug]
    return cmd


def _rename_rgb_to_color_png(folder: Path) -> Path:
    """将首帧 ``Image_*.png`` 重命名为 ``color.png``。"""
    images = sorted(folder.glob("Image_*.png"))
    if not images:
        raise FileNotFoundError(f"未找到 Image_*.png: {folder}")
    src = images[0]
    dst = folder / "color.png"
    if src.resolve() != dst.resolve():
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    return dst


def _write_instance_segmentation_png(npy_path: Path, out_png: Path) -> None:
    arr = np.load(npy_path)
    if arr.ndim != 2:
        arr = arr.reshape(arr.shape[0], arr.shape[1])
    max_v = int(arr.max()) if arr.size else 0
    if max_v > 255:
        logger.warning(
            "Object Index 最大值 %d > 255，已 clip 到 uint8；metadata 仍用完整索引字符串键",
            max_v,
        )
    u8 = np.clip(arr, 0, 255).astype(np.uint8)
    imwrite(out_png, u8)


def _finalize_scene_outputs(
    output_folder: Path,
    seed: int,
    room_type: str,
    args,
    gpu: str | None,
) -> None:
    """在首次 coarse+render 之后：Object Index 第二趟、metadata、仅保留四文件。"""
    output_folder = Path(output_folder).resolve()
    blend_path = output_folder / "scene.blend"
    if not blend_path.is_file():
        raise FileNotFoundError(f"缺少 scene.blend: {output_folder}")

    _rename_rgb_to_color_png(output_folder)

    gt_dir = output_folder / ".gt_object_index_pass"
    if gt_dir.exists():
        shutil.rmtree(gt_dir, ignore_errors=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    try:
        gt_cmd = _build_gt_object_index_command(seed, output_folder, gt_dir, args)
        _run(gt_cmd, gpu=gpu)
    except subprocess.CalledProcessError as e:
        logger.exception("Object Index（flat render）失败")
        raise RuntimeError(
            "生成 instance_segmentation 失败：请确认场景目录含 assets/ 且与 coarse 时一致"
        ) from e

    seg_npy = sorted(gt_dir.glob("ObjectSegmentation*.npy"))
    if not seg_npy:
        raise FileNotFoundError(f"未找到 ObjectSegmentation*.npy: {gt_dir}")
    objs_json = sorted(gt_dir.glob("Objects*.json"))
    if not objs_json:
        raise FileNotFoundError(f"未找到 Objects*.json: {gt_dir}")

    _write_instance_segmentation_png(seg_npy[0], output_folder / "instance_segmentation.png")

    objects_tree = json.loads(objs_json[0].read_text(encoding="utf-8"))
    index_map = _object_index_map_from_objects_json(objects_tree)

    render_meta = resolve_render_metadata(blend_path, output_folder, args)

    room_folder = _room_folder_name(room_type)
    scene_id = f"{room_folder}_s{seed}"
    metadata = {
        "scene_id": scene_id,
        "room_type": room_type,
        "render": render_meta,
        "instance_map": {
            "format": "grayscale_uint8",
            "background_value": 0,
            "object_index_map": index_map,
        },
    }
    (output_folder / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _delete_all_except_final_four(output_folder)
    tmp_dir = output_folder.parent / "tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _delete_all_except_final_four(output_folder: Path) -> None:
    """仅保留 scene.blend、color.png、instance_segmentation.png、metadata.json。"""
    output_folder = Path(output_folder)
    for p in list(output_folder.iterdir()):
        if p.name in FINAL_KEEP_NAMES:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
    for name in FINAL_KEEP_NAMES:
        if not (output_folder / name).is_file():
            logger.warning("最终输出缺少文件: %s", name)
    logger.info("清理完成，仅保留: %s", ", ".join(sorted(FINAL_KEEP_NAMES)))


def _build_single_command(
    seed: int,
    size_name: str,
    room_type: str,
    output_folder: Path,
    args,
) -> list[str]:
    """构建调用 infinigen_examples.single 的命令。"""
    base = [sys.executable, "-m", "infinigen_examples.single"]
    cli_seed = f"s{seed}"

    cmd: list[str] = base + [
        "--seed",
        cli_seed,
        "--task",
        *args.task,
        "--output_folder",
        str(output_folder),
        "--configs",
        *args.configs,
    ]

    size_override = get_size_for_room(room_type, size_name)
    size_gin = _size_override_gin_value(size_override)
    module = _gin_module_for_room(room_type)
    size_gin_key = SIZE_OVERRIDE_GIN_TEMPLATE.format(module=module)
    furn_size_gin_key = FURNITURE_SIZE_OVERRIDE_GIN_TEMPLATE.format(module=module)
    default_overrides = [
        "compose_single_room.terrain_enabled=False",
        "compose_single_room.solve_small_enabled=False",
        "full/render_image.passes_to_save=[]",
        "full/render_image.skip_reorganize_frames=True",
        f"configure_render_cycles.num_samples={args.render_samples}",
        f"configure_render_cycles.denoise={str(args.denoise)}",
        f"{size_gin_key}={size_gin}",
        f"{furn_size_gin_key}={size_gin}",
        f"{module}.floor_plan.room_type='{room_type}'",
        f"compose_single_room.room_type='{room_type}'",
    ]
    if FIXED_WALL_THICKNESS_M is not None:
        default_overrides.append(
            f"RoomConstants.global_params.wall_thickness={float(FIXED_WALL_THICKNESS_M)}"
        )
    wh = WALL_HEIGHT_UNIFORM_BY_TIER.get(size_name)
    if wh is not None:
        lo, hi = wh
        default_overrides.append(
            f'RoomConstants.global_params.wall_height=("uniform", {lo}, {hi})'
        )
    if args.light_multiplier is not None:
        default_overrides.append(f"compose_single_room.light_energy_multiplier={args.light_multiplier}")
    rw = getattr(args, "render_width", 1280)
    if rw == 640:
        w, h = 640, 360
    else:
        w, h = 1280, 720
    default_overrides.extend(
        [
            f"execute_tasks.generate_resolution=({w}, {h})",
            f"get_sensor_coords.W={w}",
            f"get_sensor_coords.H={h}",
        ]
    )
    overrides = default_overrides + list(args.overrides)
    if overrides:
        cmd += ["--overrides", *overrides]

    if args.input_folder is not None:
        cmd += ["--input_folder", str(args.input_folder)]
    if args.task_uniqname is not None:
        cmd += ["--task_uniqname", args.task_uniqname]
    if args.debug:
        cmd += ["--debug", *args.debug]

    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成矩形单房间室内场景（通过 infinigen_examples.single）。"
    )
    parser.add_argument("--start-seed", type=int, default=None, help="起始 seed，不提供则用随机大整数；批量时依次 +1")
    parser.add_argument("--num-scenes", type=int, default=1, help="生成多少个场景（未指定 --room-size-mix 时生效）")
    parser.add_argument(
        "--room-size",
        type=str,
        default=None,
        help=(
            "房间尺寸档位（仅非住宅类房型生效；住宅类见脚本说明，此参数会被忽略）。"
            "未指定时：working_space 四类（open-office / meeting-room / classroom / library）"
            "在每个场景 small/medium/large 间各 1/3 随机；其它非住宅房型为 medium。"
        ),
    )
    parser.add_argument(
        "--room-size-mix",
        type=str,
        default=None,
        help="混合档位（仅非住宅类房型）：如 small:3,medium:3；总场景数=各数量之和",
    )
    parser.add_argument(
        "--room-type",
        type=str,
        default="bedroom",
        choices=list(ROOM_TYPES),
        help="房间类型，用于场景内容与输出目录名，如 bedroom、living-room",
    )
    parser.add_argument(
        "--task",
        nargs="+",
        default=["coarse", "render"],
        choices=["coarse", "populate", "fine_terrain", "ground_truth", "render", "mesh_save", "export"],
        help="任务阶段，默认 coarse+render 自动出图",
    )
    parser.add_argument(
        "--light-multiplier",
        type=float,
        default=None,
        help="灯光亮度倍率（覆盖 GIN 中的默认值 5.0）。值越大越亮，建议范围 3-10。",
    )
    parser.add_argument(
        "--render-samples",
        type=int,
        default=128,
        help=(
            "Cycles 渲染采样数（默认 128，base.gin 原始值为 8192）。"
            "搭配 --denoise（默认开启）可获得高质量结果；"
            "不开 denoise 时建议 512-1024。"
        ),
    )
    parser.add_argument(
        "--denoise",
        action="store_true",
        default=True,
        help="启用 Cycles 去噪器（默认开启），可大幅降低所需采样数。",
    )
    parser.add_argument(
        "--no-denoise",
        action="store_true",
        default=False,
        help="关闭 Cycles 去噪器。",
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help=(
            "指定使用的 GPU 编号（通过 CUDA_VISIBLE_DEVICES），如 --gpu 2。"
            "不指定时默认使用 get_render_gpu()（当前为 2）。多卡用逗号分隔如 --gpu 2,6。"
        ),
    )
    parser.add_argument("--configs", nargs="+", default=None, help="Gin 配置文件名（默认按 room-type 自动选择，如 living_space、working_space）")
    parser.add_argument(
        "--render-width",
        type=int,
        choices=(1280, 640),
        default=1280,
        metavar="W",
        help="输出图像分辨率（宽×高）：1280→1280×720（默认），640→640×360；覆盖 execute_tasks.generate_resolution 与 get_sensor_coords",
    )
    parser.add_argument("--overrides", nargs="+", default=[], help="临时覆盖 gin 参数，格式如 模块.参数=值")
    parser.add_argument(
        "--scenes-root",
        type=Path,
        default=None,
        help=f"批量场景父目录，默认 {SCENES_SKETCH_ROOT}；子目录为 {{roomtype}}_s{{seed}}/",
    )
    parser.add_argument(
        "--output_folder",
        type=Path,
        default=None,
        help="输出目录，默认 SCENES_SKETCH/scene_seed_XXXX",
    )
    parser.add_argument("--input_folder", type=Path, default=None, help="输入目录（仅 render 单独跑时用）")
    parser.add_argument("--task_uniqname", type=str, default=None)
    parser.add_argument("--debug", type=str, nargs="*", default=None, help="调试模式")
    args = parser.parse_args()

    if args.no_denoise:
        args.denoise = False
    if args.gpu is None:
        args.gpu = get_render_gpu()

    # 根据 room-type 自动选择 GIN 配置
    if args.configs is None:
        args.configs = [ROOM_CONFIG_MAP[args.room_type]]

    scenes_root = (
        args.scenes_root if args.scenes_root is not None else SCENES_SKETCH_ROOT
    ).resolve()
    scenes_root.mkdir(parents=True, exist_ok=True)

    if args.start_seed is None:
        args.start_seed = secrets.randbelow(2**31 - 1)
        print(f"[INFO] start-seed 未指定，使用随机值: {args.start_seed}")

    room_type = args.room_type
    room_folder = _room_folder_name(room_type)
    living = _is_living_space_room(room_type)

    # 确定每个场景的档位列表
    if living:
        if args.room_size_mix is not None:
            parser.error(
                "住宅类房型（bedroom / homestudio / living-room / kitchen / bathroom / dining-room）"
                "不使用 --room-size-mix；档位由脚本按规则随机或固定。"
            )
        if args.room_size is not None and args.room_size != "medium":
            logger.warning(
                "住宅类房型忽略 --room-size=%s（使用内部 small/medium 或固定档）。",
                args.room_size,
            )
        num_scenes = args.num_scenes
        size_list = [
            resolve_living_space_tier(room_type, args.start_seed + i)
            for i in range(num_scenes)
        ]
    elif args.room_size_mix is not None:
        size_list = _parse_room_size_mix(args.room_size_mix)
        num_scenes = len(size_list)
    else:
        num_scenes = args.num_scenes
        if args.room_size is None:
            if room_type in WORKING_SPACE_ROOM_TYPES:
                rng = random.Random(args.start_seed)
                size_list = [
                    rng.choice(["small", "medium", "large"]) for _ in range(num_scenes)
                ]
            else:
                size_list = ["medium"] * num_scenes
        else:
            size_list = [args.room_size] * num_scenes

    avail = get_available_tiers(room_type)
    for tier in set(size_list):
        if tier not in avail:
            parser.error(
                f"房间类型 '{room_type}' 不支持档位 '{tier}'。"
                f"可用: {avail}"
            )

    for i in range(num_scenes):
        seed = args.start_seed + i
        size_name = size_list[i]
        if num_scenes == 1 and args.output_folder is not None:
            output_folder = Path(args.output_folder).resolve()
        else:
            output_folder = scenes_root / f"{room_folder}_s{seed}"
        output_folder.mkdir(parents=True, exist_ok=True)

        print(f"\n=== Scene {i+1}/{num_scenes}: seed={seed}, room={room_type}, size={size_name} ===")
        cmd = _build_single_command(seed, size_name, room_type, output_folder, args)
        _run(cmd, gpu=args.gpu)

        if "render" in args.task:
            _finalize_scene_outputs(output_folder, seed, room_type, args, gpu=args.gpu)
            print(
                f"[OK] 场景已保存: scene.blend, color.png, instance_segmentation.png, metadata.json → {output_folder}"
            )
        else:
            logger.warning(
                "task 未包含 render，跳过 Object Index / metadata；目录中可能仍有 coarse 中间文件，请自行清理。"
            )
            print(f"[OK] 场景（无 render 后处理）: {output_folder}")


if __name__ == "__main__":
    main()
