#!/usr/bin/env python3
"""
render.py — Blender render step for the IDEAL reconstruction pipeline.

Must be executed with a Python interpreter that can ``import bpy`` (e.g. the
``bpy`` pip package inside a conda environment, or Blender's bundled Python).

Reads the ``{model_id}_render.json`` written by ``compose.py``, applies the
predicted object poses to a copy of the original scene, saves
``{model_id}.blend``, and renders ``{model_id}.png``.  The source
``scene.blend`` is never modified.

Positional arguments (after ``--``)
------------------------------------
  blend_src    Path to the original scene.blend
  render_json  Absolute path to {model_id}_render.json
  model_id     Stem used for output filenames
  protocol     Coordinate protocol: must be "floor" (camera not yet supported)
  output_png   Absolute path for the rendered PNG

Supported protocols
-------------------
- ``floor``:  Predicted x, y, yaw, and dimensions are written back; z comes
              from the GT value already embedded in render_json by compose.py.
- ``camera``: Not yet implemented. Passing this value exits with an error.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import bpy
from mathutils import Euler, Vector

SUPPORTED_PROTOCOLS = ("floor",)
PLACEHOLDER_CATEGORY = "placeholder"


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _aggregate_mesh_aabb_size(
    root: bpy.types.Object,
) -> tuple[float, float, float] | None:
    """Return world-space AABB extents (W, H, D) by walking the subtree.

    Used when ``obj.dimensions`` is near-zero (e.g. EMPTY root objects).
    Returns ``None`` if no MESH children are found or the resulting box is
    degenerate.
    """
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    found_mesh = False

    def _walk(o: bpy.types.Object) -> None:
        nonlocal xmin, ymin, zmin, xmax, ymax, zmax, found_mesh
        if o.type == "MESH":
            found_mesh = True
            deps = bpy.context.evaluated_depsgraph_get()
            o_eval = o.evaluated_get(deps)
            mw = o_eval.matrix_world
            for corner in o_eval.bound_box:
                w = mw @ Vector(corner)
                xmin = min(xmin, w.x); xmax = max(xmax, w.x)
                ymin = min(ymin, w.y); ymax = max(ymax, w.y)
                zmin = min(zmin, w.z); zmax = max(zmax, w.z)
        for child in o.children:
            _walk(child)

    _walk(root)
    if not found_mesh:
        return None
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    if dx <= 1e-8 or dy <= 1e-8 or dz <= 1e-8:
        return None
    return dx, dy, dz


def _apply_pose(obj: bpy.types.Object, entry: dict[str, Any]) -> None:
    """Write rotation, scale, and translation from *entry* onto *obj*.

    Rotation (yaw) is applied first, then scale is adjusted to match
    predicted dimensions, then the world-space origin is set to the
    predicted position (which already contains the GT z from compose.py).
    """
    pos = entry.get("position") or [0.0, 0.0, 0.0]
    target = Vector((float(pos[0]), float(pos[1]), float(pos[2])))

    raw_dim = entry.get("dimensions") or [1.0, 1.0, 1.0]
    target_dim = Vector((
        max(float(raw_dim[0]), 1e-4),
        max(float(raw_dim[1]), 1e-4),
        max(float(raw_dim[2]), 1e-4),
    ))

    yaw = entry.get("rotation_yaw")
    if yaw is not None:
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = Euler((0.0, 0.0, math.radians(float(yaw))), "XYZ")

    bpy.context.view_layer.update()

    # Determine current extents (fall back to subtree AABB for EMPTY roots)
    d = obj.dimensions
    if max(float(d.x), float(d.y), float(d.z)) < 1e-6:
        agg = _aggregate_mesh_aabb_size(obj)
        if agg is not None:
            dx, dy, dz = agg
            print(
                f"[render] {obj.name!r}: obj.dimensions ~ 0, using subtree AABB "
                f"({dx:.4f}, {dy:.4f}, {dz:.4f})",
                file=sys.stderr,
            )
        else:
            print(
                f"[render] WARNING: {obj.name!r} dimensions ~ 0 and no MESH "
                f"children found — skipping scale",
                file=sys.stderr,
            )
            mw = obj.matrix_world.copy()
            mw.translation = target
            obj.matrix_world = mw
            bpy.context.view_layer.update()
            return
    else:
        dx, dy, dz = float(d.x), float(d.y), float(d.z)

    for i, current in enumerate((dx, dy, dz)):
        if current > 1e-8:
            obj.scale[i] *= target_dim[i] / current

    bpy.context.view_layer.update()

    mw = obj.matrix_world.copy()
    mw.translation = target
    obj.matrix_world = mw
    bpy.context.view_layer.update()


# ---------------------------------------------------------------------------
# Scene-level helpers
# ---------------------------------------------------------------------------


def _geometry_all_null(entry: dict[str, Any]) -> bool:
    return (
        entry.get("position") is None
        and entry.get("dimensions") is None
        and entry.get("rotation_yaw") is None
    )


def _create_placeholder_sphere(entry: dict[str, Any]) -> bpy.types.Object | None:
    """Create a red unit-sphere to represent a hallucinated object.

    Default radius is 0.5 (1 m diameter). If *entry* contains valid
    ``dimensions``, ``_apply_pose`` will scale it accordingly.
    Entries with all-null geometry are skipped (no position to place at).
    """
    pid = str(entry.get("id") or "placeholder")
    if _geometry_all_null(entry):
        print(f"[render] placeholder {pid!r}: geometry all null, skipping", file=sys.stderr)
        return None

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0.0, 0.0, 0.0))
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        return None
    obj.name = pid

    # Red material for easy visual identification
    mat = bpy.data.materials.get("placeholder_red")
    if mat is None:
        mat = bpy.data.materials.new("placeholder_red")
    mat.diffuse_color = (1.0, 0.0, 0.0, 1.0)
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    col = bpy.data.collections.get("unique_assets")
    if col is not None:
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        if obj.name not in col.objects:
            col.objects.link(obj)

    bpy.context.view_layer.update()
    _apply_pose(obj, entry)
    return obj


def _apply_entry(obj: bpy.types.Object, entry: dict[str, Any]) -> None:
    """Apply a render-json entry to an existing scene object."""
    if _geometry_all_null(entry):
        obj.hide_viewport = True
        obj.hide_render = True
        return
    obj.hide_viewport = False
    obj.hide_render = False
    _apply_pose(obj, entry)


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------


def apply_render_json_and_export(
    *,
    blend_src: Path,
    render_json: Path,
    model_id: str,
    protocol: str,
    output_png: Path,
) -> None:
    if protocol not in SUPPORTED_PROTOCOLS:
        print(
            f"[render] Unsupported protocol {protocol!r}. "
            f"Supported: {SUPPORTED_PROTOCOLS}. "
            f"The 'camera' coordinate protocol is not yet implemented.",
            file=sys.stderr,
        )
        sys.exit(1)

    render_json = render_json.resolve()
    with open(render_json, encoding="utf-8") as f:
        pseudo: dict[str, Any] = json.load(f)
    print(f"[render] loaded {render_json}", file=sys.stderr)

    blend_src = blend_src.resolve()
    if not blend_src.is_file():
        print(f"[render] source .blend not found: {blend_src}", file=sys.stderr)
        sys.exit(1)

    bpy.ops.wm.open_mainfile(filepath=str(blend_src))

    objects_payload: list[dict[str, Any]] = (pseudo.get("scene") or {}).get("objects") or []

    for entry in objects_payload:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("id")
        if oid is None:
            continue
        oid = str(oid)
        cat = (entry.get("category") or "").strip().lower()

        ob = bpy.data.objects.get(oid)
        if ob is None:
            if cat == PLACEHOLDER_CATEGORY:
                _create_placeholder_sphere(entry)
                print(f"[render] created placeholder sphere: {oid!r}", file=sys.stderr)
            else:
                print(f"[render] WARNING: object {oid!r} not in scene and not a placeholder, skipping", file=sys.stderr)
            continue

        _apply_entry(ob, entry)

    # Save scene copy (source .blend is untouched)
    out_dir = render_json.parent
    blend_out = out_dir / f"{model_id}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_out))
    print(f"[render] scene saved (source unchanged): {blend_out}", file=sys.stderr)

    # Render
    scene = bpy.context.scene
    if scene.camera is None:
        cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
        if not cams:
            print("[render] no camera in scene", file=sys.stderr)
            sys.exit(1)
        scene.camera = cams[0]

    output_png = output_png.resolve()
    output_png.parent.mkdir(parents=True, exist_ok=True)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(output_png)
    bpy.ops.render.render(write_still=True)
    print(f"[render] PNG -> {output_png}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _argv_after_dd() -> list[str]:
    if "--" not in sys.argv:
        print("[render] missing arguments after '--'", file=sys.stderr)
        sys.exit(2)
    return sys.argv[sys.argv.index("--") + 1:]


def main() -> None:
    args = _argv_after_dd()
    if len(args) != 5:
        print("usage: render.py -- blend_src render_json model_id protocol output_png", file=sys.stderr)
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