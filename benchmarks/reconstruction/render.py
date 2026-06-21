#!/usr/bin/env python3
"""
Blender step: read ``{model_id}_render.json`` from ``compose``, apply ``matched`` / ``hide`` /
``hallucination`` to a *copy* of the scene, save ``{model_id}.blend`` and ``.png``. Needs ``bpy``.

Geometry is already solved in ``compose``; this file only applies transforms, hides, and red sphere markers.
CLI args after ``--`` are positional (see ``reconstruct._build_render_cmd``): blend path, render JSON, model id,
PNG path, optional trailing ``objects_in_view.json`` (ignored).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import bpy

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Hallucination spheres: cap radius so a wildly wrong pred_dim_mean does not
# fill the frame. All markers use the same solid red opaque material.
HALLUC_MAX_RADIUS = 0.3   # metres
HALLUC_ALPHA = 1.0        # fully opaque (solid red ball)
HALLUC_COLOR_RGB = (1.0, 0.0, 0.0)   # Principled BSDF base color
HALLUC_MATERIAL_NAME = "ideal_hallucination_red"


# ---------------------------------------------------------------------------
# Matched: straight writeback
# ---------------------------------------------------------------------------


def _apply_matched(entry: dict[str, Any]) -> bool:
    """Write compose's ``final_*`` fields to the named Blender object.

    Returns True on success, False if the object is not found in the scene.
    """
    gt_id = str(entry.get("gt_id", ""))
    obj = bpy.data.objects.get(gt_id)
    if obj is None:
        print(
            f"[render] WARNING: matched entry {gt_id!r} not found in scene; "
            f"skipping",
            file=sys.stderr,
        )
        return False

    loc = entry.get("final_location") or [0.0, 0.0, 0.0]
    rot_z_deg = float(entry.get("final_rotation_z", 0.0))
    dim = entry.get("final_dimensions") or [1.0, 1.0, 1.0]

    # Ensure visible (may have been hidden by a previous pass).
    obj.hide_viewport = False
    obj.hide_render = False

    # Write in a stable order: rotation, then location, then dimensions.
    # bpy's obj.dimensions setter internally rescales based on the current
    # rotation-applied bounding box, so rotation must already be written.
    obj.rotation_mode = "XYZ"
    obj.rotation_euler[2] = math.radians(rot_z_deg)

    obj.location = (float(loc[0]), float(loc[1]), float(loc[2]))

    # Guard against degenerate dims (would zero obj.scale and strand the
    # object invisibly on rerun).
    dx = max(float(dim[0]), 1e-4)
    dy = max(float(dim[1]), 1e-4)
    dz = max(float(dim[2]), 1e-4)
    try:
        obj.dimensions = (dx, dy, dz)
    except (AttributeError, RuntimeError) as exc:
        # Some EMPTY / non-mesh objects don't expose a writable dimensions
        # property. compose shouldn't be producing matches for these, but
        # we keep the render alive if it happens.
        print(
            f"[render] WARNING: cannot set dimensions on {gt_id!r} "
            f"({type(obj).__name__}): {exc}",
            file=sys.stderr,
        )

    return True


# ---------------------------------------------------------------------------
# Hide: unmatched GT in-view objects
# ---------------------------------------------------------------------------


def _apply_hide(entry: dict[str, Any]) -> bool:
    gt_id = str(entry.get("gt_id", ""))
    obj = bpy.data.objects.get(gt_id)
    if obj is None:
        print(
            f"[render] WARNING: hide entry {gt_id!r} not found in scene; "
            f"skipping",
            file=sys.stderr,
        )
        return False
    obj.hide_viewport = True
    obj.hide_render = True
    return True


# ---------------------------------------------------------------------------
# Hallucination: solid opaque red UV spheres
# ---------------------------------------------------------------------------


def _get_or_make_hallucination_material() -> bpy.types.Material:
    """Return the shared solid red material; refresh color/alpha every time.

    If the .blend already contained ``ideal_hallucination_red`` (e.g. from an
    older run), we still overwrite Base Color / Alpha / blend mode so results
    stay consistent.
    """
    mat = bpy.data.materials.get(HALLUC_MATERIAL_NAME)
    if mat is None:
        mat = bpy.data.materials.new(HALLUC_MATERIAL_NAME)

    mat.use_nodes = True
    mat.blend_method = "OPAQUE"
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = nt.nodes.get("Material Output") or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # Base color and alpha. Input names are stable across Blender 3.x/4.x
    # for these two sockets; we guard with .get() just in case.
    base_color = bsdf.inputs.get("Base Color")
    if base_color is not None:
        r, g, b = HALLUC_COLOR_RGB
        base_color.default_value = (r, g, b, 1.0)
    alpha = bsdf.inputs.get("Alpha")
    if alpha is not None:
        alpha.default_value = HALLUC_ALPHA

    return mat


def _create_hallucination_sphere(entry: dict[str, Any], seq: int) -> bool:
    """Spawn one solid opaque red sphere for a hallucinated prediction.

    Position comes from ``entry["pred_position"]`` (world space after ``transform_pred_scene``
    in compose). Radius is derived from ``pred_dim_mean``.
    """
    pos = entry.get("pred_position")
    if not isinstance(pos, list) or len(pos) < 3:
        print(
            f"[render] WARNING: hallucination entry missing pred_position; "
            f"skipping: {entry!r}",
            file=sys.stderr,
        )
        return False

    raw_mean = float(entry.get("pred_dim_mean", 0.3))
    radius = min(raw_mean / 2.0, HALLUC_MAX_RADIUS)
    radius = max(radius, 0.02)   # keep it visible at least

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=radius,
        location=(float(pos[0]), float(pos[1]), float(pos[2])),
    )
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        print("[render] WARNING: failed to spawn hallucination sphere", file=sys.stderr)
        return False

    cat = str(entry.get("pred_category") or "hallucination")
    obj.name = f"ideal_hallucination_{seq:02d}_{cat}"

    mat = _get_or_make_hallucination_material()
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def apply_render_json_and_export(
    *,
    blend_src: Path,
    render_json: Path,
    model_id: str,
    output_png: Path,
) -> None:
    render_json = render_json.resolve()
    with open(render_json, encoding="utf-8") as f:
        payload: dict[str, Any] = json.load(f)
    print(f"[render] loaded {render_json}", file=sys.stderr)

    matched = payload.get("matched") or []
    hide = payload.get("hide") or []
    halluc = payload.get("hallucination") or []
    print(
        f"[render] payload: matched={len(matched)} "
        f"hide={len(hide)} hallucination={len(halluc)}",
        file=sys.stderr,
    )

    blend_src = blend_src.resolve()
    if not blend_src.is_file():
        print(f"[render] source .blend not found: {blend_src}", file=sys.stderr)
        sys.exit(1)
    bpy.ops.wm.open_mainfile(filepath=str(blend_src))

    # Apply in a stable order: matched first (may undo a previous hide),
    # then hide, then spawn hallucinations.
    n_matched = 0
    for entry in matched:
        if isinstance(entry, dict) and _apply_matched(entry):
            n_matched += 1

    n_hidden = 0
    for entry in hide:
        if isinstance(entry, dict) and _apply_hide(entry):
            n_hidden += 1

    n_halluc = 0
    for seq, entry in enumerate(halluc, start=1):
        if isinstance(entry, dict) and _create_hallucination_sphere(entry, seq):
            n_halluc += 1

    bpy.context.view_layer.update()

    print(
        f"[render] summary: matched_written={n_matched} "
        f"hidden={n_hidden} hallucination_spheres={n_halluc}",
        file=sys.stderr,
    )

    # Save scene copy (source .blend is untouched).
    out_dir = render_json.parent
    blend_out = out_dir / f"{model_id}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_out))
    print(f"[render] scene saved (source unchanged): {blend_out}", file=sys.stderr)

    # Render.
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
    # 4 args: blend, render_json, model_id, output_png (current).
    # 5 args: legacy with ignored middle slot (former ``floor``/``camera`` label).
    # 6 args: legacy with trailing objects_in_view path (ignored).
    if len(args) == 4:
        blend_src, rj, model_id, out_png = args
    elif len(args) == 5:
        blend_src, rj, model_id, _legacy_proto, out_png = args
    elif len(args) == 6:
        blend_src, rj, model_id, _legacy_proto, out_png, _in_view = args
    else:
        print(
            "usage: render.py -- blend_src render_json model_id output_png "
            "[legacy_protocol_ignored [objects_in_view_json]]",
            file=sys.stderr,
        )
        sys.exit(2)

    apply_render_json_and_export(
        blend_src=Path(blend_src),
        render_json=Path(rj),
        model_id=model_id,
        output_png=Path(out_png),
    )


if __name__ == "__main__":
    main()