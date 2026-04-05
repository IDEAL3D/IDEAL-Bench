#!/usr/bin/env python3
"""
IDEAL 重建管线（完整流程）

**任务发现**：从 ``result/<protocol>/<scene_id>/<model_id>.json`` 解析预测（逻辑见 ``runtime.py``）。

**每个任务按顺序执行**（上一步成功才进入下一步）：

1. **compose.py**（内部调用 **match.py** 做类别队列配对）— 写回预测几何、合并 ``GT.json``，写出 ``{model_id}_render.json``（与预测同目录）。
2. **blender_scripts/render.py** — 只读 ``{model_id}_render.json``，另存 ``{model_id}.blend``、渲染 ``{model_id}.png``；**不**改写 ``data/.../scene.blend``。

布局::

  输入预测: result/<protocol>/<scene_id>/<model_id>.json
  输出:     同目录 {model_id}_render.json, {model_id}.blend, {model_id}.png

用法（先在仓库根目录，即 ``git clone`` 得到的文件夹内；默认文件夹名多为 ``IDEAL``）::
  cd reconstruction
  conda activate infinigen

  python reconstruct.py --protocol floor # floor 参考系全部场景
  python reconstruct.py --scene-id bedroom_s1412833193 --gemini # 指定场景和模型
  python reconstruct.py --task-dir result/floor/bedroom_s1412833192 --model-id gpt-5.2-2025-12-11
  python reconstruct.py --prediction result/floor/.../model.json --dry-run

  # 只跑 compose（仍会写出 _render.json），不跑 Blender 渲染
  python reconstruct.py --scene-id ... --gemini --skip-render
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 路径布局、任务发现、bpy / Blender 可执行文件（脚本直跑与 ``python -m`` 均可用）
try:
    from reconstruction import runtime as _rt
except ImportError:
    import runtime as _rt


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _compose_script() -> Path:
    return Path(__file__).resolve().parent / "compose.py"


def _render_script() -> Path:
    return Path(__file__).resolve().parent / "blender_scripts" / "render.py"


def resolve_gemini_prediction(project_dir: Path, protocol: str, scene_id: str) -> Path:
    """
    解析 ``result/<protocol>/<scene_id>/`` 下的 gemini 推理 JSON。
    优先 ``gemini-3-flash-preview.json``，否则取首个 ``*gemini*.json``（排除 ``*_render.json`` 等非预测文件）。
    """
    d = _rt.result_root(project_dir) / protocol / scene_id
    if not d.is_dir():
        raise FileNotFoundError(f"不存在场景结果目录: {d}")
    preferred = d / "gemini-3-flash-preview.json"
    if preferred.is_file():
        return preferred.resolve()

    def _is_prediction_json(p: Path) -> bool:
        if p.name.endswith("_render.json"):
            return False
        return _rt.is_model_prediction_json(p)

    cands = sorted(p for p in d.glob("*gemini*.json") if _is_prediction_json(p))
    if not cands:
        raise FileNotFoundError(
            f"未找到 gemini 预测 JSON（需 gemini-3-flash-preview.json 或 *gemini*.json）: {d}"
        )
    return cands[0].resolve()


def parse_args() -> argparse.Namespace:
    root = _project_dir()
    p = argparse.ArgumentParser(
        description="IDEAL：compose.py → {model}_render.json → render.py → blend / png"
    )
    p.add_argument(
        "--gemini",
        action="store_true",
        help="单场景：使用 result/<protocol>/<scene_id>/ 下 gemini 推理 JSON（需 --scene-id）",
    )
    p.add_argument(
        "--scene-id",
        default=None,
        help="场景目录名；与 --gemini 联用；批量时可用 --scene 过滤",
    )
    p.add_argument("--protocol", default=None, choices=("floor", "camera"))
    p.add_argument("--model-id", default=None)
    p.add_argument("--scene", action="append", dest="scenes", default=None)
    p.add_argument("--task-dir", type=Path, default=None)
    p.add_argument("--prediction", type=Path, default=None)
    p.add_argument(
        "--scenes-root",
        type=Path,
        default=root / "data" / "scenes2test",
        help="场景根目录：需含 <scene_id>/objects_in_view.json（或 info.json）、GT.json、scene.blend",
    )
    p.add_argument(
        "--blend-root",
        type=Path,
        default=None,
        help="scene.blend 父目录；省略则与 --scenes-root 相同",
    )
    p.add_argument(
        "--skip-render",
        action="store_true",
        help="只运行 compose.py，不调用 render.py（仍会生成 _render.json；不生成 .blend / 渲染图）",
    )
    p.add_argument(
        "--runner",
        choices=("bpy", "blender"),
        default=os.environ.get("IDEAL_RECON_RUNNER", "bpy"),
        help=(
            "render.py：bpy 用当前 Python（需 pip bpy）；"
            "blender 用 blender --background --python（需可执行文件）。"
            "也可用环境变量 IDEAL_RECON_RUNNER 覆盖默认值。"
        ),
    )
    p.add_argument(
        "--blender",
        default=os.environ.get("BLENDER_EXECUTABLE", "blender"),
        help="仅 --runner blender 时使用",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="打印 compose 与 render 命令，不执行",
    )
    return p.parse_args()


def collect_tasks(
    args: argparse.Namespace,
) -> list[tuple[str, str, str, Path]]:
    """返回 (protocol, model_id, scene_id, prediction_path)。"""
    project_dir = _project_dir()
    scene_filter = set(args.scenes) if args.scenes else None
    tasks: list[tuple[str, str, str, Path]] = []

    if args.gemini:
        if not args.scene_id:
            raise ValueError("--gemini 需要同时指定 --scene-id")
        if args.task_dir is not None or args.prediction is not None:
            raise ValueError("--gemini 模式不要同时使用 --task-dir 或 --prediction")
        proto = args.protocol or "floor"
        pred_path = resolve_gemini_prediction(project_dir, proto, args.scene_id)
        mid = pred_path.stem
        tasks.append((proto, mid, args.scene_id, pred_path))
        return tasks

    if args.task_dir is not None and args.prediction is not None:
        raise ValueError("不能同时使用 --task-dir 与 --prediction")

    if args.prediction is not None:
        pred_file = _rt.resolve_path_for_task(project_dir, args.prediction)
        if not pred_file.is_file():
            raise FileNotFoundError(f"--prediction 不是文件: {pred_file}")
        proto, mid, sid, pred_path = _rt.parse_inference_prediction_file(pred_file)
        tasks.append((proto, mid, sid, pred_path))
    elif args.task_dir is not None:
        td = _rt.resolve_path_for_task(project_dir, args.task_dir)
        if _rt.is_protocol_root_batch_dir(project_dir, td):
            proto = td.name
            for p, m, s, pred in _rt.discover_predictions(
                project_dir, proto, args.model_id, scene_filter
            ):
                tasks.append((p, m, s, pred))
        else:
            pred_file = _rt.resolve_task_prediction_path(
                project_dir, args.task_dir, args.model_id
            )
            proto, mid, sid, pred_path = _rt.parse_inference_prediction_file(pred_file)
            tasks.append((proto, mid, sid, pred_path))
    else:
        sid_filter = scene_filter
        if args.scene_id:
            only = {args.scene_id}
            sid_filter = only if scene_filter is None else scene_filter & only
            if not sid_filter:
                raise ValueError("--scene-id 与 --scene 无交集")
        for proto, mid, sid, pred in _rt.discover_predictions(
            project_dir, args.protocol, args.model_id, sid_filter
        ):
            tasks.append((proto, mid, sid, pred))

    return tasks


def build_compose_cmd(
    args: argparse.Namespace,
    protocol: str,
    model_id: str,
    scene_id: str,
    prediction: Path,
) -> list[str]:
    """compose.py 子进程参数列表。"""
    blend = _rt.scene_blend_path(args.scenes_root, args.blend_root, scene_id)
    sidecar = args.scenes_root / scene_id / "objects_in_view.json"
    legacy = args.scenes_root / scene_id / "info.json"
    info_json = sidecar if sidecar.is_file() else legacy
    if not info_json.is_file():
        raise FileNotFoundError(
            f"compose 需要含 objects 的 objects_in_view.json 或 info.json: {sidecar}"
        )
    gt_json = args.scenes_root / scene_id / "GT.json"
    if not gt_json.is_file():
        raise FileNotFoundError(f"compose 需要 GT.json: {gt_json}")

    return [
        sys.executable,
        str(_compose_script()),
        "--blend",
        str(blend),
        "--prediction",
        str(prediction.resolve()),
        "--info-json",
        str(info_json),
        "--gt-json",
        str(gt_json.resolve()),
        "--protocol",
        protocol,
        "--scene-id",
        scene_id,
        "--model-id",
        model_id,
    ]


def build_render_cmd(
    args: argparse.Namespace,
    protocol: str,
    model_id: str,
    scene_id: str,
    prediction: Path,
    *,
    blender_exe: Path | None,
) -> list[str]:
    """render.py 子进程参数列表（需先有 compose 写出的 {model_id}_render.json）。"""
    blend = _rt.scene_blend_path(args.scenes_root, args.blend_root, scene_id)
    render_json = prediction.parent / f"{model_id}_render.json"
    out_png = prediction.parent / f"{model_id}.png"
    render_py = _render_script()

    tail = [
        str(blend.resolve()),
        str(render_json.resolve()),
        model_id,
        protocol,
        str(out_png.resolve()),
    ]

    if args.runner == "blender":
        exe = blender_exe if blender_exe is not None else Path(args.blender)
        return [str(exe), "--background", "--python", str(render_py), "--"] + tail
    return [sys.executable, str(render_py), "--"] + tail


def run_one_task(
    args: argparse.Namespace,
    proto: str,
    mid: str,
    sid: str,
    pred: Path,
    *,
    blender_exe: Path | None,
) -> int:
    """顺序执行 compose → render；返回进程退出码（0 成功）。"""
    project_dir = _project_dir()

    compose_cmd = build_compose_cmd(args, proto, mid, sid, pred)
    if args.dry_run:
        print("[reconstruct] compose:", " ".join(compose_cmd))
        if not args.skip_render:
            try:
                rc_cmd = build_render_cmd(
                    args,
                    proto,
                    mid,
                    sid,
                    pred,
                    blender_exe=blender_exe
                    if args.runner == "blender"
                    else None,
                )
                print("[reconstruct] render:", " ".join(rc_cmd))
            except FileNotFoundError as e:
                print(f"[reconstruct] render 跳过（{e}）", file=sys.stderr)
        return 0

    r = subprocess.run(compose_cmd, cwd=str(project_dir))
    if r.returncode != 0:
        print(f"[reconstruct] compose.py 失败: scene={sid} model={mid}", file=sys.stderr)
        return r.returncode

    if args.skip_render:
        print(f"[reconstruct] 已跳过 render（--skip-render）: {sid} {mid}", file=sys.stderr)
        return 0

    if args.runner == "bpy":
        _rt.ensure_bpy_importable()

    try:
        rc_cmd = build_render_cmd(
            args,
            proto,
            mid,
            sid,
            pred,
            blender_exe=blender_exe if args.runner == "blender" else None,
        )
    except FileNotFoundError as e:
        print(f"[reconstruct] render 无法构造命令: {e}", file=sys.stderr)
        return 1

    r2 = subprocess.run(rc_cmd, cwd=str(project_dir))
    if r2.returncode != 0:
        print(f"[reconstruct] render.py 失败: scene={sid} model={mid}", file=sys.stderr)
        return r2.returncode

    return 0


def main() -> int:
    args = parse_args()

    try:
        tasks = collect_tasks(args)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1

    if not tasks:
        print("未找到任何预测 JSON（检查 result/ 或参数）", file=sys.stderr)
        return 1

    if len(tasks) > 1:
        protos = sorted({p for p, _, _, _ in tasks})
        print(
            f"[reconstruct] {len(tasks)} 个任务, protocol={','.join(protos)}",
            file=sys.stderr,
        )

    blender_exe: Path | None = None
    if not args.dry_run and not args.skip_render and args.runner == "blender":
        blender_exe = _rt.resolve_blender_executable(args.blender)

    for proto, mid, sid, pred in tasks:
        code = run_one_task(
            args,
            proto,
            mid,
            sid,
            pred,
            blender_exe=blender_exe,
        )
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    sys.exit(main())
