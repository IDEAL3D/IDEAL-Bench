#!/usr/bin/env python3
"""
在带 ``bpy`` 的 Python 内执行：读取 **compose.py** 已写好的 ``{model_id}_render.json``（pseudo-GT），
将参数写回场景副本并渲染 PNG；**不再**合并 GT/cache 或重写 render.json。

参数（``--`` 之后）::

  blend_src  render_json  model_id  protocol  output_png

``render_json`` 为 ``result/.../<model_id>_render.json`` 的绝对路径。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import bpy  # noqa: E402
from mathutils import Euler, Vector  # noqa: E402

PLACEHOLDER_CATEGORY = "placeholder"


def _aggregate_mesh_world_aabb_size(root: bpy.types.Object) -> tuple[float, float, float] | None:
    """自 root 起遍历子树，合并所有 MESH 的世界轴对齐包围盒边长（EMPTY 等 dimensions≈0 时用）。"""
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    any_mesh = False

    def walk(o: bpy.types.Object) -> None:
        nonlocal xmin, ymin, zmin, xmax, ymax, zmax, any_mesh
        if o.type == "MESH":
            any_mesh = True
            try:
                deps = bpy.context.evaluated_depsgraph_get()
                o_eval = o.evaluated_get(deps)
            except Exception:
                o_eval = o
            mw = o_eval.matrix_world
            for corner in o_eval.bound_box:
                w = mw @ Vector(corner)
                xmin = min(xmin, w.x)
                xmax = max(xmax, w.x)
                ymin = min(ymin, w.y)
                ymax = max(ymax, w.y)
                zmin = min(zmin, w.z)
                zmax = max(zmax, w.z)
        for ch in o.children:
            walk(ch)

    walk(root)
    if not any_mesh:
        return None
    w, h, d = xmax - xmin, ymax - ymin, zmax - zmin
    if w <= 1e-8 or h <= 1e-8 or d <= 1e-8:
        return None
    return (w, h, d)


def _apply_gt_semantics_pose_to_object(obj: bpy.types.Object, pred: dict[str, Any]) -> None:
    """按 GT 语义写回：旋转 → 缩放 → 设原点位置（与 PREPROCESS_DESIGN / geometric_rules_floor 一致）。"""
    pos = pred.get("position") or [0.0, 0.0, 0.0]
    target = Vector((float(pos[0]), float(pos[1]), float(pos[2])))
    raw_dim = pred.get("dimensions") or [1.0, 1.0, 1.0]
    tp = Vector(
        (
            max(float(raw_dim[0]), 1e-4),
            max(float(raw_dim[1]), 1e-4),
            max(float(raw_dim[2]), 1e-4),
        )
    )
    yaw = pred.get("rotation_yaw")

    if yaw is not None:
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = Euler((0.0, 0.0, math.radians(float(yaw))), "XYZ")

    bpy.context.view_layer.update()

    d = obj.dimensions
    use_agg = max(float(d.x), float(d.y), float(d.z)) < 1e-6
    if use_agg:
        agg = _aggregate_mesh_world_aabb_size(obj)
        if agg is not None:
            dx, dy, dz = agg
            print(
                f"[apply] category={pred.get('category')!r} id={pred.get('id')!r} "
                f"object={obj.name!r}: obj.dimensions≈0，改用子树 MESH 世界 AABB "
                f"({dx:.4f},{dy:.4f},{dz:.4f}) 计算缩放",
                file=sys.stderr,
            )
        else:
            dx, dy, dz = float(d.x), float(d.y), float(d.z)
            print(
                f"[apply] 警告: {obj.name!r} dimensions≈0 且无法从子树 MESH 求 AABB，"
                f"跳过缩放（请检查层级）",
                file=sys.stderr,
            )
    else:
        dx, dy, dz = float(d.x), float(d.y), float(d.z)

    for i, dv in enumerate((dx, dy, dz)):
        if dv > 1e-8:
            obj.scale[i] *= tp[i] / dv

    bpy.context.view_layer.update()

    mw = obj.matrix_world.copy()
    mw.translation = target
    obj.matrix_world = mw
    bpy.context.view_layer.update()


def _argv_after_dd() -> list[str]:
    if "--" not in sys.argv:
        print("render.py: 缺少 ``--`` 之后的参数", file=sys.stderr)
        sys.exit(2)
    return sys.argv[sys.argv.index("--") + 1 :]


def _geometry_all_null(entry: dict[str, Any]) -> bool:
    return (
        entry.get("position") is None
        and entry.get("dimensions") is None
        and entry.get("rotation_yaw") is None
    )


def _create_placeholder_sphere(entry: dict[str, Any]) -> bpy.types.Object | None:
    pid = str(entry.get("id") or "placeholder")
    if _geometry_all_null(entry):
        print(f"[render] placeholder {pid!r} 几何全 null，跳过创建", file=sys.stderr)
        return None

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0.0, 0.0, 0.0))
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        return None
    obj.name = pid

    col = bpy.data.collections.get("unique_assets")
    if col is not None:
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        if obj.name not in col.objects:
            col.objects.link(obj)

    bpy.context.view_layer.update()
    _apply_gt_semantics_pose_to_object(obj, entry)
    return obj


def _apply_entry_to_scene_object(obj: bpy.types.Object, entry: dict[str, Any], *, protocol: str) -> None:
    if protocol != "floor":
        print(
            f"[render] protocol={protocol!r}：跳过写回 {obj.name!r}",
            file=sys.stderr,
        )
        return
    if _geometry_all_null(entry):
        obj.hide_viewport = True
        obj.hide_render = True
        return
    obj.hide_viewport = False
    obj.hide_render = False
    _apply_gt_semantics_pose_to_object(obj, entry)


def apply_render_json_and_export(
    *,
    blend_src: Path,
    render_json: Path,
    model_id: str,
    protocol: str,
    output_png: Path,
) -> None:
    render_json = render_json.resolve()
    with open(render_json, encoding="utf-8") as f:
        pseudo: dict[str, Any] = json.load(f)

    print(f"[render] 读取 {render_json}", file=sys.stderr)

    blend_src = blend_src.resolve()
    if not blend_src.is_file():
        print(f"[render] 找不到源 .blend: {blend_src}", file=sys.stderr)
        sys.exit(1)

    bpy.ops.wm.open_mainfile(filepath=str(blend_src))

    objects_payload = (pseudo.get("scene") or {}).get("objects") or []
    if not isinstance(objects_payload, list):
        objects_payload = []

    for entry in objects_payload:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("id")
        cat = (entry.get("category") or "").strip().lower()
        if oid is None:
            continue
        oid = str(oid)

        ob = bpy.data.objects.get(oid)
        if ob is None:
            if cat == PLACEHOLDER_CATEGORY:
                _create_placeholder_sphere(entry)
                print(f"[render] 已创建占位 mesh: {oid!r}", file=sys.stderr)
            else:
                print(
                    f"[render] WARN: 场景中无对象 {oid!r}，且非 placeholder，跳过",
                    file=sys.stderr,
                )
            continue

        _apply_entry_to_scene_object(ob, entry, protocol=protocol)

    out_dir = render_json.parent
    blend_out = out_dir / f"{model_id}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_out))
    print(f"[render] 已另存场景（未改源文件）: {blend_out}", file=sys.stderr)

    scene = bpy.context.scene
    if scene.camera is None:
        cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
        if not cams:
            print("[render] 场景中无相机", file=sys.stderr)
            sys.exit(1)
        scene.camera = cams[0]

    output_png = output_png.resolve()
    output_png.parent.mkdir(parents=True, exist_ok=True)
    scene.render.image_settings.file_format = "PNG"
    base = str(output_png.with_suffix(""))
    scene.render.filepath = base
    bpy.ops.render.render(write_still=True)
    print(f"[render] PNG -> {output_png}", file=sys.stderr)


def main() -> None:
    args = _argv_after_dd()
    if len(args) != 5:
        print(
            "用法: render.py -- blend_src render_json model_id protocol output_png",
            file=sys.stderr,
        )
        sys.exit(2)

    blend_src, rj, model_id, protocol, out_png = args
    apply_render_json_and_export(
        blend_src=Path(blend_src),
        render_json=Path(rj),
        model_id=model_id,
        protocol=protocol,
        output_png=Path(out_png),
    )


if __name__ == "__main__":
    main()
