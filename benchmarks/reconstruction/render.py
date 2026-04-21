#!/usr/bin/env python3
"""
render.py - Blender render step for the IDEAL reconstruction pipeline.

Must be executed with a Python interpreter that can ``import bpy`` (the
``bpy`` pip package in a conda env, or Blender's bundled Python).

Reads the ``{model_id}_render.json`` written by compose.py, applies the
instructions to a copy of the original scene, saves ``{model_id}.blend``,
and renders ``{model_id}.png``. The source ``scene.blend`` is never
modified.

Schema consumed (``{model_id}_render.json``)
--------------------------------------------
::

  {
    "scene_id":         "...",
    "model_id":         "...",
    "room_type":        "...",
    "is_grid_scene":    bool,
    "camera_transform": {"delta_deg": int | null},

    "matched": [
      {"gt_id": "...", "category": "bed",
       "final_location":   [x, y, z],   # Blender obj.location directly
       "final_rotation_z": float_deg,   # Blender rotation_euler.z in degrees
       "final_dimensions": [x, y, z]},  # Blender obj.dimensions directly
      ...
    ],

    "hide": [{"gt_id": "...", "category": "..."}, ...],

    "hallucination": [
      {"pred_category": "sofa",
       "pred_position": [x, y, z],
       "pred_dim_mean": float_metres},
      ...
    ]
  }

Responsibilities (this module does NOT compute geometry)
--------------------------------------------------------
compose.py already performed the full GT-inverse (pose_inverse.invert_pose
+ wall-mounted Z policy). All render.py needs to do is:

  * matched       -> obj.location / rotation_euler.z / dimensions = final_*
  * hide          -> obj.hide_render = obj.hide_viewport = True
  * hallucination -> create a translucent red UV sphere at pred_position
                     with radius min(pred_dim_mean / 2, HALLUC_MAX_RADIUS)

Objects in the scene that appear in neither list are left untouched. This
is intentional: non-target objects in grid scenes (e.g. blackboard) and
any object outside the in-view whitelist should keep their blend-native
transforms.

CLI
---
Positional arguments after ``--`` (matches reconstruct._build_render_cmd)::

    blend_src       Path to the original scene.blend
    render_json     Path to {model_id}_render.json
    model_id        Stem used for output filenames
    protocol        Coordinate protocol; must be "floor" (camera NYI)
    output_png      Path for the rendered PNG
    objects_in_view_json  Path to objects_in_view.json (tolerated but
                          unused: the schema above already tells us which
                          objects to touch)
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

SUPPORTED_PROTOCOLS = ("floor",)

# Hallucination spheres: cap the visual size so a model that hallucinates
# a "3 m wide sofa" does not produce a wall-sized red blob that occludes
# the rest of the scene. Transparency makes them visible without hiding
# everything behind them.
HALLUC_MAX_RADIUS = 0.3   # metres
HALLUC_ALPHA = 0.5        # 0 = transparent, 1 = opaque
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
# Hallucination: translucent red UV spheres
# ---------------------------------------------------------------------------


def _get_or_make_hallucination_material() -> bpy.types.Material:
    """Return (creating if needed) the shared translucent-red material.

    We use Principled BSDF with Alpha < 1 so the sphere renders
    translucent in Cycles/Eevee without depending on viewport shading
    tricks.
    """
    mat = bpy.data.materials.get(HALLUC_MATERIAL_NAME)
    if mat is not None:
        return mat

    mat = bpy.data.materials.new(HALLUC_MATERIAL_NAME)
    mat.use_nodes = True
    mat.blend_method = "BLEND"   # Eevee alpha blending; harmless in Cycles
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        # Fall back gracefully if the default node got replaced.
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
    """Spawn one translucent red sphere for a hallucinated prediction.

    Position comes from ``entry["pred_position"]`` (world-frame, already
    through camera_transform in compose). Radius is the capped half of
    ``pred_dim_mean``.
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
    # Accept either 5 args (new) or 6 args (legacy, with trailing
    # objects_in_view.json that we ignore). This lets reconstruct.py keep
    # its existing command builder without breaking old callers.
    if len(args) == 5:
        blend_src, rj, model_id, protocol, out_png = args
    elif len(args) == 6:
        blend_src, rj, model_id, protocol, out_png, _in_view = args
    else:
        print(
            "usage: render.py -- blend_src render_json model_id protocol output_png "
            "[objects_in_view_json]",
            file=sys.stderr,
        )
        sys.exit(2)

    apply_render_json_and_export(
        blend_src=Path(blend_src),
        render_json=Path(rj),
        model_id=model_id,
        protocol=protocol,
        output_png=Path(out_png),
    )


if __name__ == "__main__":
    main()