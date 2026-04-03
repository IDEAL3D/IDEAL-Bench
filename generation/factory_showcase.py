#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 Infinigen 的多个 AssetFactory 陈列在规则网格上：每个资产占据 cell_size×cell_size 米，
资产置于格子中心（XY），脚底落在 z=0，并保存为单个 .blend。

**运行方式与 ``generation/generate.py`` / ``generate_box.py`` 相同**：在已配置 Infinigen 的 conda 环境里直接::

    cd /path/to/IDL3D/generation
    python factory_showcase.py

底层与 ``python -m infinigen_examples.single`` 一样，需要 **当前解释器能 ``import bpy``**（你的 ``(infinigen)`` 环境若已能跑 ``generate.py``，即可同样跑本脚本）。本脚本不会单独调用 ``blender`` 可执行文件。

默认输出：``INFINIGEN/SCENES_SKETCH/factory.blend``（与 generate_box 默认试跑目录一致）；可用 ``--output`` 改路径。

默认仅使用 ``tests/assets/list_indoor_meshes.txt``（不含自然列表）。清单中的 ``infinigen.assets.objects.tableware.*`` 一律跳过。
若使用 ``--scan-subclasses``，可能混入其它未在清单中的工厂（tableware 仍会过滤）。

Gin 会同时加载 ``configs_indoor`` 与 ``configs_nature``（仅为了解析 ``base.gin`` 绑定），**不会**因此按自然类清单生成资产。

若从 ``generation/`` 目录启动，脚本会把 ``../infinigen-idl3d`` 加入 ``sys.path``；运行时会 ``chdir`` 到该源码根目录（与 ``generate_box`` 子进程 cwd 一致）。

常用参数:
  --cell-size 5       格子边长（米）
  --ncols 0           0 表示自动 ≈sqrt(N)
  --base-seed 0       第 i 个工厂使用实例 seed base_seed + i
  --max-factories 20  只生成前 20 个（调试）
  --scan-subclasses   扫描并合并全部已加载的 AssetFactory 子类（很慢）
  --discover-sim      与 --scan-subclasses 联用：同时加载 sim_objects
  --draw-cells        为每个格子画贴地线框（便于对齐 5m）
"""

from __future__ import annotations

import argparse
import importlib
import os
import logging
import pkgutil
import sys
import traceback
from pathlib import Path

from infinigen_paths import INFINIGEN_IDL3D_ROOT, SCENES_SKETCH_ROOT

# -----------------------------------------------------------------------------
# 包路径：IDL3D/infinigen-idl3d 为 Python 包根（与 generate_box 子进程 cwd 一致）
# -----------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_FINIGEN_SRC = INFINIGEN_IDL3D_ROOT
if _FINIGEN_SRC.is_dir() and str(_FINIGEN_SRC) not in sys.path:
    sys.path.insert(0, str(_FINIGEN_SRC))

import bpy
import gin
import numpy as np
from mathutils import Vector

from infinigen.assets.utils.decorate import read_base_co
from infinigen.assets.utils.misc import subclasses
from infinigen.core import init
from infinigen.core.placement.factory import AssetFactory
from infinigen.core.util import blender as butil
from infinigen.core.util.math import FixedSeed
from infinigen.core.util.test_utils import import_item, load_txt_list

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("factory_showcase")

LIST_INDOOR = _FINIGEN_SRC / "tests/assets/list_indoor_meshes.txt"
# 用户指定不展示餐具等小件生成器（与 list 中路径前缀一致）
_SKIP_TABLEWARE_PREFIX = "infinigen.assets.objects.tableware."


def _is_tableware_dotted_path(dotted: str) -> bool:
    return dotted.startswith(_SKIP_TABLEWARE_PREFIX)


def _is_tableware_factory_class(cls: type) -> bool:
    mod = getattr(cls, "__module__", "") or ""
    return mod.startswith(_SKIP_TABLEWARE_PREFIX.rstrip("."))


def _parse_factory_dotted_paths() -> list[str]:
    paths: list[str] = []
    p = LIST_INDOOR
    if not p.exists():
        logger.warning("列表不存在: %s", p)
        return []
    for line in load_txt_list(p, skip_sharp=True):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if _is_tableware_dotted_path(s):
            continue
        paths.append(s)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for s in paths:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _import_all_submodules(package_name: str) -> None:
    pkg = importlib.import_module(package_name)
    path = getattr(pkg, "__path__", None)
    if path is None:
        return
    for info in pkgutil.walk_packages(path, prefix=f"{package_name}."):
        if info.ispkg:
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:
            logger.debug("跳过子模块 %s: %s", info.name, e)


def _collect_factory_classes_from_lists() -> list[type]:
    classes: list[type] = []
    for dotted in _parse_factory_dotted_paths():
        try:
            with gin.unlock_config():
                obj = import_item(dotted)
        except Exception as e:
            logger.warning("无法导入 %s: %s", dotted, e)
            continue
        if isinstance(obj, type) and issubclass(obj, AssetFactory):
            if _is_tableware_factory_class(obj):
                continue
            classes.append(obj)
        else:
            logger.warning("非 AssetFactory 类，跳过: %s -> %r", dotted, obj)
    return classes


def _merge_class_list(
    base: list[type], extra: list[type] | None
) -> list[type]:
    if not extra:
        return base
    by_name: dict[str, type] = {c.__name__: c for c in base}
    for c in extra:
        if _is_tableware_factory_class(c):
            continue
        by_name.setdefault(c.__name__, c)
    return list(by_name.values())


def _discover_extra_factories(include_sim: bool) -> list[type]:
    _import_all_submodules("infinigen.assets.objects")
    extra: list[type] = []
    if include_sim:
        _import_all_submodules("infinigen.assets.sim_objects")
    for c in subclasses(AssetFactory):
        if c is AssetFactory:
            continue
        mod = getattr(c, "__module__", "") or ""
        if "tests." in mod or mod.endswith(".test_utils"):
            continue
        if mod.startswith(_SKIP_TABLEWARE_PREFIX.rstrip(".")):
            continue
        # 抽象/未实现 create_asset 的会在 spawn 时失败，仍先列入再由 try/except 跳过
        extra.append(c)
    return extra


def _root_obj(obj: bpy.types.Object) -> bpy.types.Object:
    while obj.parent is not None:
        obj = obj.parent
    return obj


def _strip_animation_drivers(obj: bpy.types.Object) -> None:
    if obj.animation_data is not None:
        for d in list(obj.animation_data.drivers.values()):
            obj.driver_remove(d.data_path)


def _world_mesh_bounds(root: bpy.types.Object) -> tuple[float, float, float, float, float, float] | None:
    """返回 (xmin,xmax,ymin,ymax,zmin,zmax) 世界坐标；无网格则 None。"""
    dg = bpy.context.evaluated_depsgraph_get()
    xs, ys, zs = [], [], []
    stack = [root]
    while stack:
        o = stack.pop()
        stack.extend(o.children)
        if o.type != "MESH":
            continue
        try:
            co = read_base_co(o)
        except Exception:
            continue
        if len(co) == 0:
            continue
        mw = o.matrix_world
        for row in co:
            w = mw @ Vector((float(row[0]), float(row[1]), float(row[2])))
            xs.append(w.x)
            ys.append(w.y)
            zs.append(w.z)
    if not zs:
        return None
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def _spawn_one(cls: type, inst_seed: int) -> bpy.types.Object | None:
    """生成资产，将根物体平移使 XY 居中于原点、脚底 z=0；返回根物体。"""
    fac = cls(inst_seed)
    try:
        with FixedSeed(inst_seed):
            if hasattr(fac, "post_init"):
                fac.post_init()
            asset = fac.spawn_asset(inst_seed)
    except Exception as e:
        logger.warning("%s.spawn_asset 失败: %s", cls.__name__, e)
        traceback.print_exc()
        return None
    try:
        fac.finalize_assets(asset)
    except Exception as e:
        logger.warning("%s.finalize_assets 失败: %s", cls.__name__, e)
        traceback.print_exc()
        return None

    root = _root_obj(asset)
    _strip_animation_drivers(root)
    bpy.context.view_layer.update()

    b = _world_mesh_bounds(root)
    if b is None:
        logger.warning("%s 未产生可用 MESH，仍保留空物体", cls.__name__)
        return root

    xmin, xmax, ymin, ymax, zmin, _zmax = b
    cx = (xmin + xmax) / 2
    cy = (ymin + ymax) / 2
    root.location.x -= cx
    root.location.y -= cy
    root.location.z -= zmin
    bpy.context.view_layer.update()
    return root


def _draw_cell_outline(
    col: int, row: int, cell: float, z_eps: float = 0.002
) -> None:
    """在格子上方贴地画一个正方形线框（编辑模式下的边）。"""
    half = cell / 2
    cx = (col + 0.5) * cell
    cy = (row + 0.5) * cell
    z = z_eps
    verts = [
        (cx - half, cy - half, z),
        (cx + half, cy - half, z),
        (cx + half, cy + half, z),
        (cx - half, cy + half, z),
    ]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
    mesh = bpy.data.meshes.new(name=f"cell_{col}_{row}")
    mesh.from_pydata(verts, edges, [])
    obj = bpy.data.objects.new(f"cell_outline_{col}_{row}", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mat = bpy.data.materials.new(name=f"cell_mat_{col}_{row}")
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    out = mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
    em = mat.node_tree.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (0.15, 0.35, 0.9, 1)
    em.inputs["Strength"].default_value = 0.5
    mat.node_tree.links.new(em.outputs["Emission"], out.inputs["Surface"])
    obj.data.materials.append(mat)


def _add_ground_plane(ncols: int, nrows: int, cell: float) -> None:
    width = ncols * cell + cell
    depth = nrows * cell + cell
    bpy.ops.mesh.primitive_plane_add(
        size=1,
        location=((ncols * cell) / 2, (nrows * cell) / 2, 0),
    )
    plane = bpy.context.active_object
    plane.scale = (width / 2, depth / 2, 1)
    plane.name = "showcase_ground"
    mat = bpy.data.materials.new("ground_gray")
    mat.use_nodes = True
    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled:
        principled.inputs["Base Color"].default_value = (0.12, 0.12, 0.13, 1)
        principled.inputs["Roughness"].default_value = 0.85
    plane.data.materials.append(mat)


def parse_args():
    p = argparse.ArgumentParser(description="Infinigen AssetFactory 网格陈列")
    p.add_argument(
        "--output",
        type=Path,
        default=SCENES_SKETCH_ROOT / "factory.blend",
        help=f"输出 .blend 路径（默认 {SCENES_SKETCH_ROOT / 'factory.blend'}）",
    )
    p.add_argument("--cell-size", type=float, default=5.0, help="格子边长（米）")
    p.add_argument(
        "--ncols",
        type=int,
        default=0,
        help="列数；0 为自动 floor(sqrt(N))+",
    )
    p.add_argument("--base-seed", type=int, default=0, help="工厂实例种子基值")
    p.add_argument("--max-factories", type=int, default=0, help="0 表示不限制数量")
    p.add_argument(
        "--discover-sim",
        action="store_true",
        help="扫描 sim_objects + objects 下全部已加载的 AssetFactory 子类并合并",
    )
    p.add_argument(
        "--scan-subclasses",
        action="store_true",
        help="import infinigen.assets.objects 下全部子模块并合并 subclasses(AssetFactory)（较慢）",
    )
    p.add_argument(
        "--draw-cells",
        action="store_true",
        help="绘制每个 5m 格子的贴地线框",
    )
    return init.parse_args_blender(p)


def main() -> None:
    args = parse_args()

    # 与 generate_box 调用 infinigen_examples.single 时一致，便于相对路径与官方行为对齐
    os.chdir(_FINIGEN_SRC)

    # apply_gin_configs 会强制加载 base.gin；该文件在 configs_nature 下，不在 configs_indoor。
    # 仅用于 Gin 绑定，不会因此生成自然类资产（资产列表仍只来自 list_indoor_meshes）。
    init.apply_gin_configs(
        [
            "infinigen_examples/configs_indoor",
            "infinigen_examples/configs_nature",
        ],
        skip_unknown=True,
    )
    init.configure_blender()

    butil.clear_scene()

    factory_classes = _collect_factory_classes_from_lists()
    if args.scan_subclasses:
        discovered = _discover_extra_factories(include_sim=args.discover_sim)
        factory_classes = _merge_class_list(factory_classes, discovered)

    factory_classes.sort(key=lambda c: c.__name__)

    if args.max_factories > 0:
        factory_classes = factory_classes[: args.max_factories]

    n = len(factory_classes)
    if n == 0:
        logger.error("没有找到任何工厂类，退出。")
        return

    ncols = args.ncols
    if ncols <= 0:
        ncols = max(1, int(np.floor(np.sqrt(n))))
    nrows = int(np.ceil(n / ncols))

    logger.info(
        "共 %d 个工厂，网格 %d 列 × %d 行，格宽 %.2f m",
        n,
        ncols,
        nrows,
        args.cell_size,
    )

    _add_ground_plane(ncols, nrows, args.cell_size)

    ok, fail = 0, 0
    for i, cls in enumerate(factory_classes):
        col, row = i % ncols, i // ncols
        cx = (col + 0.5) * args.cell_size
        cy = (row + 0.5) * args.cell_size
        seed = args.base_seed + i
        root = _spawn_one(cls, seed)
        if root is None:
            fail += 1
            continue
        root.location = (
            root.location[0] + cx,
            root.location[1] + cy,
            root.location[2],
        )
        root.name = f"showcase_{cls.__name__}"
        ok += 1
        if args.draw_cells:
            _draw_cell_outline(col, row, args.cell_size)
        if (i + 1) % 10 == 0:
            logger.info("进度 %d / %d", i + 1, n)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    butil.save_blend(args.output, autopack=False)
    logger.info(
        "完成：成功 %d，失败 %d，已保存 %s",
        ok,
        fail,
        args.output.resolve(),
    )


if __name__ == "__main__":
    main()
