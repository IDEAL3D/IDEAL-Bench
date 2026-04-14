"""
重建流水线的 **runtime** 层：固定 ``result/`` 路径布局、任务发现、以及 ``bpy`` 导入检查（无 CLI）。

命名说明：与 Go/Rust 等生态中 ``runtime`` 表示「程序运行所需环境与前置条件」的用法一致，
区别于业务脚本 ``reconstruct.py`` / ``match.py``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_path_for_task(project_dir: Path, raw: Path) -> Path:
    """
    解析 ``--task-dir`` / ``--prediction``。

    相对路径时：若 ``cwd / raw`` 存在则用其解析结果，否则用 ``project_dir / raw``（IDEAL 仓库根下的相对路径）。
    """
    raw = raw.expanduser()
    if raw.is_absolute():
        return raw.resolve()
    cwd_hit = (Path.cwd() / raw).resolve()
    proj_hit = (project_dir / raw).resolve()
    if cwd_hit.exists():
        return cwd_hit
    return proj_hit


def result_root(project_dir: Path) -> Path:
    """仓库根 ``IDEAL/result/``：推理、重建、benchmark 等按场景聚合的产物。"""
    return project_dir / "result"


def is_protocol_root_batch_dir(project_dir: Path, resolved_dir: Path) -> bool:
    """
    是否为 ``result/floor`` 或 ``result/camera``（其下为各 ``scene_id`` 子目录）。
    用于 ``--task-dir`` 批量重建该协议下全部场景。
    """
    d = resolved_dir.resolve()
    if not d.is_dir() or d.name not in ("floor", "camera"):
        return False
    return d.parent.resolve() == result_root(project_dir).resolve()


def is_model_prediction_json(path: Path) -> bool:
    """与 VLM 预测 JSON 区分：排除 benchmark 等侧车文件。"""
    if path.suffix != ".json":
        return False
    name = path.name
    if name.endswith("_benchmark.json") or name.endswith("_bench.json"):
        return False
    if name in ("manifest.json", "summary.json"):
        return False
    return True


def ensure_bpy_importable() -> None:
    """重建 ``render.py`` 子进程前要求当前环境能 ``import bpy``（与 InfiniGen 常用 pip/conda bpy 一致）。"""
    try:
        import importlib.util

        if importlib.util.find_spec("bpy") is None:
            raise ImportError("no bpy")
    except ImportError:
        print(
            "当前解释器无法 import bpy。\n"
            "请安装：pip install 'bpy>=4.2,<5'（见 requirements.txt），或使用已配置好 bpy 的 conda 环境。\n"
            "重建渲染仅支持该方式，不再查找系统 ``blender`` 可执行文件。",
            file=sys.stderr,
        )
        sys.exit(2)


def parse_inference_prediction_file(pred_file: Path) -> tuple[str, str, str, Path]:
    """
    解析 ``(protocol, model_id, scene_id, prediction_path)``。

    当前布局：``.../result/<protocol>/<scene_id>/<model_id>.json``（``result`` 在仓库根下）。

    兼容：``.../inference/result/.../json/<model_id>.json``、
    ``.../runs/<run_id>/inference/.../prediction.json``。
    """
    pred_file = pred_file.resolve()
    parts = pred_file.parts

    for i, seg in enumerate(parts):
        if seg != "result":
            continue
        if i > 0 and parts[i - 1] == "inference":
            continue
        if (
            i + 3 < len(parts)
            and parts[i + 3] == pred_file.name
            and pred_file.suffix == ".json"
        ):
            protocol = parts[i + 1]
            scene_id = parts[i + 2]
            if protocol in ("floor", "camera"):
                model_id = pred_file.stem
                return protocol, model_id, scene_id, pred_file

    for i, seg in enumerate(parts):
        if seg == "result" and i >= 1 and parts[i - 1] == "inference":
            if (
                i + 4 < len(parts)
                and parts[i + 3] == "json"
                and pred_file.suffix == ".json"
            ):
                protocol = parts[i + 1]
                scene_id = parts[i + 2]
                model_id = pred_file.stem
                return protocol, model_id, scene_id, pred_file
            break

    if "runs" in parts and "inference" in parts:
        i = parts.index("inference")
        if (
            i >= 1
            and i + 3 < len(parts)
            and parts[i + 1] != "result"
            and pred_file.name == "prediction.json"
        ):
            protocol = parts[i + 1]
            model_id = parts[i + 2]
            scene_id = parts[i + 3]
            return protocol, model_id, scene_id, pred_file
    raise ValueError(
        "无法解析预测 JSON 路径。需为 .../result/<protocol>/<scene_id>/<model_id>.json "
        "或旧版嵌套 / runs 下 prediction.json"
    )


def resolve_task_prediction_path(
    project_dir: Path, task_dir: Path, model_id: str | None
) -> Path:
    """
    从 ``--task-dir`` 得到单个预测 JSON（场景目录扁平、或旧 ``json/``、或 runs 任务目录）。
    """
    td = resolve_path_for_task(project_dir, task_dir)
    if td.is_file():
        if td.suffix == ".json" or td.name == "prediction.json":
            return td
        raise ValueError(f"--task-dir 指向了不支持的文件: {td}")
    if not td.is_dir():
        raise ValueError(f"--task-dir 不存在: {td}")

    legacy = td / "prediction.json"
    if legacy.is_file():
        return legacy

    if td.name == "json":
        jfs = sorted(p for p in td.glob("*.json") if is_model_prediction_json(p))
        if model_id:
            p = td / f"{model_id}.json"
            if not p.is_file():
                raise ValueError(f"缺少文件: {p}")
            return p
        if len(jfs) == 1:
            return jfs[0]
        raise ValueError(
            f"目录 {td} 下有多个 .json，请用 --model-id 指定模型文件名（不含 .json）"
        )

    jfs = sorted(p for p in td.glob("*.json") if is_model_prediction_json(p))
    if jfs:
        if model_id:
            p = td / f"{model_id}.json"
            if not p.is_file():
                raise ValueError(f"缺少文件: {p}")
            return p
        if len(jfs) == 1:
            return jfs[0]
        raise ValueError(
            f"场景目录 {td} 下有多个预测 JSON，请用 --model-id 指定"
        )

    jdir = td / "json"
    if jdir.is_dir():
        jfs = sorted(p for p in jdir.glob("*.json") if is_model_prediction_json(p))
        if model_id:
            p = jdir / f"{model_id}.json"
            if not p.is_file():
                raise ValueError(f"缺少文件: {p}")
            return p
        if len(jfs) == 1:
            return jfs[0]
        raise ValueError(
            f"场景目录 {td} 的 json/ 下有多个预测 JSON，请用 --model-id 指定"
        )

    raise ValueError(
        f"无法从目录推断预测 JSON（需要 prediction.json、场景下模型 .json、或 json/）: {td}"
    )


def discover_predictions(
    project_dir: Path,
    protocol: str | None,
    model_filter: str | None,
    scene_filter: set[str] | None,
) -> list[tuple[str, str, str, Path]]:
    """
    扫描 ``result/<protocol>/<scene_id>/*.json``（排除 ``*_benchmark.json`` 等）。
    若仍存在旧 ``json/`` 子目录，也扫描其中文件。
    返回列表项: (protocol, model_id, scene_id, path)
    """
    root = result_root(project_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"不存在结果目录: {root}")

    out: list[tuple[str, str, str, Path]] = []
    protocols = (
        [protocol] if protocol else sorted(p.name for p in root.iterdir() if p.is_dir())
    )
    for proto in protocols:
        pdir = root / proto
        if not pdir.is_dir():
            continue
        for scene_dir in sorted(pdir.iterdir()):
            if not scene_dir.is_dir():
                continue
            sid = scene_dir.name
            if scene_filter is not None and sid not in scene_filter:
                continue

            def add_from_dir(d: Path) -> None:
                for jf in sorted(d.glob("*.json")):
                    if not is_model_prediction_json(jf):
                        continue
                    mid = jf.stem
                    if model_filter and mid != model_filter:
                        continue
                    out.append((proto, mid, sid, jf.resolve()))

            flat_preds = [
                jf
                for jf in sorted(scene_dir.glob("*.json"))
                if is_model_prediction_json(jf)
            ]
            if flat_preds:
                add_from_dir(scene_dir)
            else:
                nested = scene_dir / "json"
                if nested.is_dir():
                    add_from_dir(nested)
    return out


def scene_blend_path(scenes_root: Path, blend_root: Path | None, scene_id: str) -> Path:
    """``{root}/{scene_id}/scene.blend``；root 为 ``blend-root`` 若给定，否则为 ``scenes-root``。"""
    root = blend_root if blend_root is not None else scenes_root
    return root / scene_id / "scene.blend"
