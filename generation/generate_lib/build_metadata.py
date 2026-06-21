


    #!/usr/bin/env python3
"""
Build the ``metadata.json`` payload dict from ``scene.blend``, Infinigen ``Objects*.json``,
and ``color.png``.

Outputs four top-level blocks:

``render``
    Image-level fields: resolution + image stats (mean_saturation, hue_std, mean_value,
    mean_gradient, entropy) computed from ``color.png``.
``camera``
    Camera pose and optics: location, rotation, focal length, sensor width,
    plus derived height and distance_to_center.
``environment``
    Room type, lighting, surface materials, mirror presence.
``instance_map``
    Grayscale uint16 instance segmentation map metadata and complete object index map.

Camera / environment fields come from in-process ``bpy`` (``open_mainfile`` + scene read).
Image stats come from ``color.png`` via PIL/numpy (no bpy required).
On failure, an error is raised.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)


def _round2(x: float) -> float:
    """Quantize metadata floats to two decimals (``round``: IEEE 754 ties-to-even).

    ``location`` and ``rotation_euler`` stay full precision; all other
    floats emitted into ``metadata.json`` should pass through here.
    """
    return round(float(x), 2)


def _quantize_json_floats(obj: Any) -> Any:
    """Recursively round every float in dicts / lists (e.g. ``environment`` subtree)."""
    if isinstance(obj, float):
        return _round2(obj)
    if isinstance(obj, list):
        return [_quantize_json_floats(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _quantize_json_floats(v) for k, v in obj.items()}
    return obj


CAMERA_ROTATION_UNIT = "radians"
CAMERA_ROTATION_ORDER = "XYZ"


# ---------------------------------------------------------------------------
# Render block
# ---------------------------------------------------------------------------

def _build_render_block(resolution: list[int], color_png: Path | None) -> dict:
    """Assemble the ``render`` block: resolution + image stats (flat, no nesting).

    Image stats are merged directly alongside resolution.
    If ``color_png`` is None or missing, image stat fields are omitted with a warning.
    """
    block: dict[str, Any] = {"resolution": [int(r) for r in resolution]}
    if color_png is not None and color_png.exists():
        try:
            from extract_img import compute_image_stats  # local sibling module
            block.update(compute_image_stats(color_png))
        except Exception as e:
            logger.warning("Failed to compute image stats from %s: %s", color_png, e)
    else:
        logger.warning("color.png not found at %s; image stats omitted", color_png)
    return block


# ---------------------------------------------------------------------------
# Camera block
# ---------------------------------------------------------------------------

def _build_camera_block(
    location: list[float],
    rotation_euler: list[float],
    focal_length_mm: float,
    sensor_width: float,
) -> dict:
    """Assemble the ``camera`` block with derived height and distance_to_center."""
    x, y, z = float(location[0]), float(location[1]), float(location[2])
    return {
        "location": location,              # full precision
        "rotation_euler": rotation_euler,  # full precision
        "rotation_unit": CAMERA_ROTATION_UNIT,
        "rotation_order": CAMERA_ROTATION_ORDER,
        "focal_length_mm": _round2(focal_length_mm),
        "sensor_width": _round2(sensor_width),
        "height": _round2(z),
        "distance_to_center": _round2(math.sqrt(x * x + y * y)),
    }


# ---------------------------------------------------------------------------
# Instance map block
# ---------------------------------------------------------------------------

def build_instance_map_block(object_index_map: dict[str, str]) -> dict:
    """``instance_map`` fragment for one scene."""
    return {
        "format": "grayscale_uint16",
        "bits_per_channel": 16,
        "background_value": 0,
        "object_index_map": object_index_map,
    }


def _object_index_map_from_objects_json(data: dict) -> dict[str, str]:
    """Build ``object_index_map`` from Infinigen ``Objects*.json``: every entry with object_index > 0."""
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


# ---------------------------------------------------------------------------
# Environment block (bpy)
# ---------------------------------------------------------------------------

def _is_portal_light(light) -> bool:
    cyc = getattr(light, "cycles", None)
    if cyc is None:
        return False
    return bool(getattr(cyc, "is_portal", False))


def _collect_lighting_bpy() -> dict:
    import bpy  # type: ignore[import-not-found]

    light_count = 0
    has_window = False
    for light in bpy.data.lights:
        if _is_portal_light(light):
            has_window = True
        elif light.type == "AREA":
            light_count += 1
    return {"light_count": light_count, "has_window": has_window}


def _principled_surface_params_bpy(mat) -> dict | None:
    import bpy  # type: ignore[import-not-found]

    if not mat or not mat.use_nodes:
        return None
    nt = mat.node_tree
    if nt is None:
        return None
    principled = None
    for node in nt.nodes:
        if getattr(node, "bl_idname", "") == "ShaderNodeBsdfPrincipled":
            principled = node
            break
        if getattr(node, "type", "") == "BSDF_PRINCIPLED":
            principled = node
            break
    if principled is None:
        return None
    inp = principled.inputs
    try:
        bc = inp["Base Color"].default_value
    except (KeyError, IndexError):
        bc = inp[0].default_value
    base_color = [float(bc[i]) for i in range(min(4, len(bc)))]
    while len(base_color) < 4:
        base_color.append(1.0)

    def _fslot(names: tuple[str, ...], default: float = 0.0) -> float:
        for name in names:
            if name in inp:
                v = inp[name].default_value
                return float(v if isinstance(v, (int, float)) else v[0])
        return default

    roughness = _fslot(("Roughness",), 0.5)
    metallic = _fslot(("Metallic",), 0.0)
    specular = _fslot(
        ("Specular IOR Level", "Specular", "IOR Level"),
        0.5,
    )
    return {
        "base_color": base_color,
        "roughness": roughness,
        "metallic": metallic,
        "specular": specular,
    }


def _first_principled_for_name_substr_bpy(substr: str) -> dict | None:
    import bpy  # type: ignore[import-not-found]

    sub = substr.lower()
    for obj in bpy.data.objects:
        if sub not in obj.name.lower():
            continue
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            params = _principled_surface_params_bpy(mat)
            if params is not None:
                return params
    return None


def _collect_materials_bpy() -> dict:
    return {
        "floor": _first_principled_for_name_substr_bpy("floor"),
        "wall": _first_principled_for_name_substr_bpy("wall"),
    }


def _collect_has_mirror_bpy() -> bool:
    import bpy  # type: ignore[import-not-found]

    return any("mirror" in o.name.lower() for o in bpy.data.objects)


def _collect_environment_bpy(room_type: str) -> dict:
    return {
        "room_type": room_type,
        "lighting": _collect_lighting_bpy(),
        "materials": _collect_materials_bpy(),
        "has_mirror": _collect_has_mirror_bpy(),
    }


# ---------------------------------------------------------------------------
# bpy scene reading
# ---------------------------------------------------------------------------

def _objects_stub_from_current_bpy() -> dict:
    """Minimal Infinigen-shaped ``{obj_name: {"object_index": int}}`` from Blender ``pass_index``."""
    import bpy  # type: ignore[import-not-found]

    data: dict = {}
    for obj in bpy.data.objects:
        try:
            pi = int(obj.pass_index)
        except (TypeError, ValueError):
            continue
        if pi > 0:
            data[obj.name] = {"object_index": pi}
    return data


def _camera_environment_from_current_bpy(room_type: str) -> dict | None:
    """Assume ``.blend`` is already loaded; extract camera pose + environment."""
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        cam = bpy.context.scene.camera
        if cam is None:
            return None
        scene = bpy.context.scene
        loc, rot_quat, _sca = cam.matrix_world.decompose()
        euler = rot_quat.to_euler(CAMERA_ROTATION_ORDER)
        camera_raw = {
            "resolution": [
                int(scene.render.resolution_x),
                int(scene.render.resolution_y),
            ],
            "location": [float(loc.x), float(loc.y), float(loc.z)],
            "rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
            "focal_length_mm": float(cam.data.lens),
            "sensor_width": float(cam.data.sensor_width),
        }
        env = _quantize_json_floats(_collect_environment_bpy(room_type))
        return {"camera_raw": camera_raw, "environment": env}
    except Exception as e:
        logger.warning("bpy failed to read scene metadata from loaded file: %s", e)
        return None


def resolve_scene_metadata(blend_path: Path, room_type: str) -> dict:
    """Read camera + environment from ``blend_path`` using in-process ``bpy`` only.

    Raises ``RuntimeError`` if ``bpy`` is unavailable, the file cannot be opened, or the scene
    has no readable active camera / environment data.
    """
    blend_path = Path(blend_path).resolve()
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "resolve_scene_metadata requires a Python that can import bpy "
            "(use the same conda env / interpreter you use for generation with bpy)."
        ) from e
    try:
        bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    except Exception as e:
        raise RuntimeError(f"Could not open blend for scene metadata: {blend_path}") from e
    fragment = _camera_environment_from_current_bpy(room_type)
    if fragment is None:
        raise RuntimeError(
            f"Could not read camera/environment from blend (no active camera or read error): "
            f"{blend_path}"
        )
    logger.info("metadata (camera+environment): in-process bpy")
    return fragment


# ---------------------------------------------------------------------------
# Public assembly functions
# ---------------------------------------------------------------------------

def _assemble_metadata(
    scene_id: str,
    camera_raw: dict,
    environment: dict,
    index_map: dict,
    color_png: Path | None,
) -> dict:
    """Assemble the final metadata dict from pre-collected fragments.

    Room type lives only under ``environment["room_type"]`` (see ``_collect_environment_bpy``),
    not as a top-level key — matches ``generate`` output.
    """
    render_block = _build_render_block(camera_raw["resolution"], color_png)
    camera_block = _build_camera_block(
        location=camera_raw["location"],
        rotation_euler=camera_raw["rotation_euler"],
        focal_length_mm=camera_raw["focal_length_mm"],
        sensor_width=camera_raw["sensor_width"],
    )
    return {
        "scene_id": scene_id,
        "render": render_block,
        "camera": camera_block,
        "environment": environment,
        "instance_map": build_instance_map_block(index_map),
    }


def build_metadata(
    scene_id: str,
    canonical_room_type: str,
    blend_path: Path,
    objects_json_path: Path,
    color_png: Path | None = None,
) -> dict:
    """Assemble scene metadata: identifiers, render, camera, environment, instance map."""
    blend_path = Path(blend_path)
    if color_png is None:
        color_png = blend_path.parent / "color.png"
    data = json.loads(Path(objects_json_path).read_text(encoding="utf-8"))
    index_map = _object_index_map_from_objects_json(data)
    fragment = resolve_scene_metadata(blend_path, canonical_room_type)
    return _assemble_metadata(
        scene_id=scene_id,
        camera_raw=fragment["camera_raw"],
        environment=fragment["environment"],
        index_map=index_map,
        color_png=color_png,
    )


def build_metadata_from_loaded_bpy_scene(
    scene_id: str,
    canonical_room_type: str,
    color_png: Path | None = None,
) -> dict:
    """Build metadata dict from the **already loaded** Blender file (``bpy`` session).

    Uses ``pass_index`` on objects (same convention as Infinigen ``Objects*.json``) and current
    scene camera / environment. Does not call ``open_mainfile``.
    """
    fragment = _camera_environment_from_current_bpy(canonical_room_type)
    if fragment is None:
        raise RuntimeError("build_metadata_from_loaded_bpy_scene: no camera or bpy read failed")
    data = _objects_stub_from_current_bpy()
    index_map = _object_index_map_from_objects_json(data)
    return _assemble_metadata(
        scene_id=scene_id,
        camera_raw=fragment["camera_raw"],
        environment=fragment["environment"],
        index_map=index_map,
        color_png=color_png,
    )


def build_metadata_from_blend_path(
    scene_id: str,
    canonical_room_type: str,
    blend_path: Path,
    color_png: Path | None = None,
) -> dict:
    """Like ``build_metadata_from_loaded_bpy_scene`` but opens ``blend_path`` once (requires in-process ``bpy``)."""
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "build_metadata_from_blend_path requires a Python that can import bpy "
            "(use the same conda env / interpreter you use for generation with bpy)."
        ) from e
    blend_path = Path(blend_path).resolve()
    if color_png is None:
        color_png = blend_path.parent / "color.png"
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    return build_metadata_from_loaded_bpy_scene(scene_id, canonical_room_type, color_png)