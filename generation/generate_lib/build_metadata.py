#!/usr/bin/env python3
"""
Build the ``metadata.json`` payload dict from ``scene.blend`` and Infinigen ``Objects*.json``.

Camera / render / environment fields come from in-process ``bpy`` (``open_mainfile`` + scene read).
Metadata is read in-process via ``bpy`` only. On failure, an error is raised.
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

    ``camera_location`` and ``camera_rotation_euler`` stay full precision; all other
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


def _quantize_render_skip_camera_pose(render: dict) -> dict:
    """Round all floats under ``render`` except ``camera_location`` / ``camera_rotation_euler``."""
    skip = frozenset({"camera_location", "camera_rotation_euler"})
    out: dict[str, Any] = {}
    for k, v in render.items():
        if k in skip:
            out[k] = v
        elif isinstance(v, float):
            out[k] = _round2(v)
        elif isinstance(v, list):
            if k == "resolution":
                out[k] = [int(x) for x in v]
            else:
                out[k] = [_quantize_json_floats(x) for x in v]
        else:
            out[k] = v
    return out


RENDER_ROTATION_UNIT = "radians"
RENDER_ROTATION_ORDER = "XYZ"


def _attach_render_rotation_fields(render: dict) -> dict:
    out = dict(render)
    out["rotation_unit"] = RENDER_ROTATION_UNIT
    out["rotation_order"] = RENDER_ROTATION_ORDER
    return out


def _attach_camera_derived_fields(render: dict) -> dict:
    loc = render.get("camera_location")
    if not loc or len(loc) < 3:
        return dict(render)
    out = dict(render)
    x, y, z = float(loc[0]), float(loc[1]), float(loc[2])
    out["camera_height"] = z
    out["camera_distance_to_center"] = math.sqrt(x * x + y * y)
    return out


def _finalize_render_dict(render: dict) -> dict:
    r = _attach_camera_derived_fields(_attach_render_rotation_fields(render))
    return _quantize_render_skip_camera_pose(r)


def build_instance_map_block(object_index_map: dict[str, str]) -> dict:
    """``instance_map`` fragment for one scene; field semantics in ``docs/METADATA_SCHEMA.md``."""
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


def _filter_furniture_only(object_index_map: dict[str, str]) -> dict[str, str]:
    """Keep entries whose label contains ``factory`` (furniture); drop walls/floor/camera etc."""
    return {k: v for k, v in object_index_map.items() if "factory" in v.lower()}


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


def _render_environment_fragment_from_current_bpy(room_type: str) -> dict | None:
    """Assume ``.blend`` is already loaded; build ``render`` + ``environment``."""
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
        euler = rot_quat.to_euler(RENDER_ROTATION_ORDER)
        render_base = {
            "resolution": [
                int(scene.render.resolution_x),
                int(scene.render.resolution_y),
            ],
            "camera_location": [float(loc.x), float(loc.y), float(loc.z)],
            "camera_rotation_euler": [float(euler.x), float(euler.y), float(euler.z)],
            "focal_length_mm": float(cam.data.lens),
            "sensor_width": float(cam.data.sensor_width),
        }
        env = _quantize_json_floats(_collect_environment_bpy(room_type))
        return {
            "render": _finalize_render_dict(render_base),
            "environment": env,
        }
    except Exception as e:
        logger.warning("bpy failed to read scene metadata from loaded file: %s", e)
        return None


def resolve_scene_metadata(blend_path: Path, room_type: str) -> dict:
    """Read render + environment from ``blend_path`` using in-process ``bpy`` only.

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
    fragment = _render_environment_fragment_from_current_bpy(room_type)
    if fragment is None:
        raise RuntimeError(
            f"Could not read render/environment metadata from blend (no active camera or read error): "
            f"{blend_path}"
        )
    logger.info("metadata (render+environment): in-process bpy")
    return fragment


def build_metadata(
    scene_id: str,
    canonical_room_type: str,
    blend_path: Path,
    objects_json_path: Path,
) -> dict:
    """Assemble scene metadata: identifiers, render/camera, instance map, and environment."""
    data = json.loads(Path(objects_json_path).read_text(encoding="utf-8"))
    index_map = _filter_furniture_only(_object_index_map_from_objects_json(data))
    fragment = resolve_scene_metadata(Path(blend_path), canonical_room_type)
    return {
        "scene_id": scene_id,
        "render": fragment["render"],
        "instance_map": build_instance_map_block(index_map),
        "environment": fragment["environment"],
    }


def build_metadata_from_loaded_bpy_scene(scene_id: str, canonical_room_type: str) -> dict:
    """Build metadata dict from the **already loaded** Blender file (``bpy`` session).

    Uses ``pass_index`` on objects (same convention as Infinigen ``Objects*.json``) and current scene camera /
    environment. Does not call ``open_mainfile``.
    """
    fragment = _render_environment_fragment_from_current_bpy(canonical_room_type)
    if fragment is None:
        raise RuntimeError("build_metadata_from_loaded_bpy_scene: no camera or bpy read failed")
    data = _objects_stub_from_current_bpy()
    index_map = _filter_furniture_only(_object_index_map_from_objects_json(data))
    return {
        "scene_id": scene_id,
        "render": fragment["render"],
        "instance_map": build_instance_map_block(index_map),
        "environment": fragment["environment"],
    }


def build_metadata_from_blend_path(scene_id: str, canonical_room_type: str, blend_path: Path) -> dict:
    """Like ``build_metadata_from_loaded_bpy_scene`` but opens ``blend_path`` once (requires in-process ``bpy``)."""
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "build_metadata_from_blend_path requires a Python that can import bpy "
            "(use the same conda env / interpreter you use for generation with bpy)."
        ) from e
    bpy.ops.wm.open_mainfile(filepath=str(Path(blend_path).resolve()))
    return build_metadata_from_loaded_bpy_scene(scene_id, canonical_room_type)
