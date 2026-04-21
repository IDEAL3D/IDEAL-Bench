"""
generate_test.py — 生成用于检测的 fake prediction JSON。

流程：
  1. 加载场景 GT + objects_in_view，筛选 in-view 物体。
  1.5 将 GT（世界坐标系）逆变换到相机坐标系（camera_transform.py 正变换的反向），
      使 fake prediction 与真实模型输出的坐标系一致。
  2. 应用用户指定的扰动（offset / scale / rotate）。
  3. 抹除原始资产 id，改为 category_01 格式。
  4. 保存 test_{int}.json，并追加记录到 test_log.csv。

用法示例：
  python generate_test.py --scenes bedroom_s14704319 --offset x 0.5
  python generate_test.py --scenes bedroom_s14704319 living_room_s001 --offset x y 0.5 1.0
  python generate_test.py --scenes bedroom_s14704319 --scale x 0.8
  python generate_test.py --scenes bedroom_s14704319 --rotate 90
  python generate_test.py --scenes bedroom_s14704319 --camera-only
      # 仅世界系→相机系，无 offset/scale/rotate，便于核对变换后的 in-view GT
"""

import argparse
import copy
import csv
import json
import math
import os
import sys

# ── 路径 ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_DIR = os.path.dirname(BENCHMARKS_DIR)
SCENES_ROOT = os.path.join(PROJECT_DIR, "data", "scenes")
OUTPUT_DIR = SCRIPT_DIR      # test_*.json 和 test_log.csv 都保存在 benchmarks/tests/
LOG_CSV = os.path.join(OUTPUT_DIR, "test_log.csv")

# 把 benchmarks/ 加入 path，以便 import camera_transform
if BENCHMARKS_DIR not in sys.path:
    sys.path.insert(0, BENCHMARKS_DIR)
from camera_transform import load_camera_meta, camera_yaw_deg, snap_delta_deg  # noqa: E402

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_gt(scene_dir: str) -> dict:
    with open(os.path.join(scene_dir, "GT.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_inview_ids(scene_dir: str) -> set[str]:
    with open(os.path.join(scene_dir, "objects_in_view.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    return {obj["id"] for obj in data.get("objects", [])}


def get_inview_objects(gt: dict, inview_ids: set[str]) -> list[dict]:
    objects = gt.get("scene", {}).get("objects", [])
    return [copy.deepcopy(obj) for obj in objects if obj["id"] in inview_ids]


# ── 步骤 1.5：GT 世界坐标系 → 相机坐标系（camera_transform 正变换的逆） ────────

def world_to_camera(objects: list[dict], scene_dir: str) -> tuple[list[dict], int | None]:
    """将物体从世界坐标系转换到相机坐标系。

    这是 camera_transform.transform_pred_objects (pred→world) 的逆变换，
    等价于将 delta_deg 取反后重新旋转。

    返回 (transformed_objects, inv_delta_deg)；
    若 metadata 不可用则原样返回并给出警告。
    """
    cam_meta = load_camera_meta(scene_dir)
    if cam_meta is None:
        print(
            f"[警告] {os.path.basename(scene_dir)}: metadata.json 不可用，"
            "跳过相机坐标系转换，position/yaw 保持世界坐标系值。",
            file=sys.stderr,
        )
        return objects, None

    yaw_deg = camera_yaw_deg(cam_meta)
    delta_deg = snap_delta_deg(yaw_deg)      # camera_transform 正变换用的 delta
    inv_delta_deg = -delta_deg               # 逆变换：把世界坐标转回相机坐标

    inv_rad = math.radians(inv_delta_deg)
    c, s = math.cos(inv_rad), math.sin(inv_rad)

    result = []
    for obj in objects:
        obj = dict(obj)

        # 旋转 position XY
        pp = obj.get("position")
        if isinstance(pp, list) and len(pp) >= 2:
            x_c = float(pp[0]) * c - float(pp[1]) * s
            y_c = float(pp[0]) * s + float(pp[1]) * c
            z = float(pp[2]) if len(pp) >= 3 else 0.0
            obj["position"] = [round(x_c, 4), round(y_c, 4), round(z, 4)]

        # 调整 yaw
        yaw = obj.get("rotation_yaw")
        if yaw is not None:
            obj["rotation_yaw"] = int((float(yaw) + inv_delta_deg) % 360)

        result.append(obj)

    print(
        f"[相机变换] camera yaw={yaw_deg:.1f}°  delta={delta_deg}°  "
        f"逆变换 inv_delta={inv_delta_deg}°",
        file=sys.stderr,
    )
    return result, inv_delta_deg


# ── 步骤 2：用户指定的扰动 ────────────────────────────────────────────────────

def apply_offset(objects: list[dict], axes: list[str], values: list[float]) -> None:
    for obj in objects:
        pos = obj.get("position", [0, 0, 0])
        for ax, val in zip(axes, values):
            pos[AXIS_INDEX[ax]] = round(pos[AXIS_INDEX[ax]] + val, 6)
        obj["position"] = pos


def apply_scale(objects: list[dict], axes: list[str], values: list[float]) -> None:
    for obj in objects:
        dims = obj.get("dimensions", [1, 1, 1])
        for ax, val in zip(axes, values):
            dims[AXIS_INDEX[ax]] = round(dims[AXIS_INDEX[ax]] * val, 6)
        obj["dimensions"] = dims


def apply_rotate(objects: list[dict], degrees: float) -> None:
    """所有物体绕自身几何中心逆时针旋转，仅改变 rotation_yaw。"""
    for obj in objects:
        if obj.get("rotation_yaw") is not None:
            obj["rotation_yaw"] = (obj["rotation_yaw"] + degrees) % 360


# ── 步骤 3：匿名化 id ─────────────────────────────────────────────────────────

def anonymize_ids(objects: list[dict]) -> list[dict]:
    counter: dict[str, int] = {}
    for obj in objects:
        cat = obj.get("category", "unknown")
        counter[cat] = counter.get(cat, 0) + 1
        obj["id"] = f"{cat}_{counter[cat]:02d}"
    return objects


# ── 步骤 4：组装 / 保存 ───────────────────────────────────────────────────────

def build_prediction(gt: dict, objects: list[dict]) -> dict:
    room = copy.deepcopy(gt.get("scene", {}).get("room", {}))
    room["asset_count"] = len(objects)
    return {"scene": {"room": room, "objects": objects}}


def next_test_index() -> int:
    idx = 1
    while os.path.exists(os.path.join(OUTPUT_DIR, f"test_{idx}.json")):
        idx += 1
    return idx


def append_log(row: dict) -> None:
    fieldnames = ["test_file", "scene_id", "transform", "axes", "values"]
    write_header = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ── 参数解析 ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="生成用于基准检测的 fake prediction JSON。")
    p.add_argument("--scenes", nargs="+", required=True, help="场景 ID 列表")
    p.add_argument("--offset", nargs="+", metavar="",
                   help="位置偏移：轴名… 值…  例如 --offset x 0.5 或 --offset x y z 1 2 3")
    p.add_argument("--scale", nargs="+", metavar="",
                   help="尺寸缩放：轴名… 值…  例如 --scale x 0.8 或 --scale x y 0.8 1.2")
    p.add_argument("--rotate", type=float, metavar="DEGREES",
                   help="逆时针旋转角度（度），例如 --rotate 90")
    p.add_argument(
        "--camera-only",
        action="store_true",
        help="仅做相机坐标系变换，不应用 offset/scale/rotate（与三者互斥）。",
    )
    return p.parse_args()


def parse_axes_values(tokens: list[str], op_name: str) -> tuple[list[str], list[float]]:
    valid_axes = set(AXIS_INDEX.keys())
    axes, values = [], []
    i = 0
    while i < len(tokens) and tokens[i] in valid_axes:
        axes.append(tokens[i])
        i += 1
    while i < len(tokens):
        try:
            values.append(float(tokens[i]))
        except ValueError:
            sys.exit(f"[错误] {op_name} 参数中发现无效数值：{tokens[i]}")
        i += 1
    if not axes:
        sys.exit(f"[错误] {op_name} 至少需要指定一个轴（x/y/z）。")
    if not values:
        sys.exit(f"[错误] {op_name} 至少需要指定一个数值。")
    # 单个值广播到所有轴（如 --scale x y 0.7 → 每个轴都乘 0.7）
    if len(values) == 1 and len(axes) > 1:
        values = values * len(axes)
    if len(axes) != len(values):
        sys.exit(f"[错误] {op_name} 轴数量（{len(axes)}）与数值数量（{len(values)}）不匹配。")
    return axes, values


# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    ops = [args.offset is not None, args.scale is not None, args.rotate is not None]
    if args.camera_only:
        if sum(ops) > 0:
            sys.exit("[错误] --camera-only 不能与 --offset / --scale / --rotate 同时使用。")
        axes, values = [], []
        transform = "camera_only"
        transform_desc = "camera_only (no user transform)"
    else:
        if sum(ops) > 1:
            sys.exit("[错误] --offset / --scale / --rotate 三者只能选其一，不支持组合。")
        if sum(ops) == 0:
            sys.exit(
                "[错误] 必须指定 --camera-only，或 --offset / --scale / --rotate 之一。"
            )

        if args.offset is not None:
            axes, values = parse_axes_values(args.offset, "--offset")
            transform = "offset"
            transform_desc = f"axes={axes}, values={values}"
        elif args.scale is not None:
            axes, values = parse_axes_values(args.scale, "--scale")
            transform = "scale"
            transform_desc = f"axes={axes}, values={values}"
        else:
            axes, values = [], [args.rotate]
            transform = "rotate"
            transform_desc = f"degrees={args.rotate}"

    for scene_id in args.scenes:
        scene_dir = os.path.join(SCENES_ROOT, scene_id)
        if not os.path.isdir(scene_dir):
            print(f"[警告] 场景目录不存在，跳过：{scene_dir}")
            continue

        # 步骤 1：加载并筛选 in-view GT 物体（世界坐标系）
        gt = load_gt(scene_dir)
        inview_ids = load_inview_ids(scene_dir)
        objects = get_inview_objects(gt, inview_ids)
        if not objects:
            print(f"[警告] {scene_id} 没有找到 in-view 物体，跳过。")
            continue

        # 步骤 1.5：转换到相机坐标系（逆相机变换）
        objects, _ = world_to_camera(objects, scene_dir)

        # 步骤 2：应用用户扰动（--camera-only 时跳过）
        if transform == "offset":
            apply_offset(objects, axes, values)
        elif transform == "scale":
            apply_scale(objects, axes, values)
        elif transform == "rotate":
            apply_rotate(objects, values[0])

        # 步骤 3：匿名化 id
        anonymize_ids(objects)

        # 步骤 4：保存 JSON + 记录日志
        prediction = build_prediction(gt, objects)
        idx = next_test_index()
        out_path = os.path.join(OUTPUT_DIR, f"test_{idx}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(prediction, f, indent=2, ensure_ascii=False)
        print(f"[生成] {out_path}  (场景: {scene_id}, 变换: {transform_desc})")

        append_log({
            "test_file": f"test_{idx}.json",
            "scene_id": scene_id,
            "transform": transform,
            "axes": ",".join(axes) if axes else "—",
            "values": ",".join(str(v) for v in values),
        })

    print(f"\n日志已追加至：{LOG_CSV}")


if __name__ == "__main__":
    main()
