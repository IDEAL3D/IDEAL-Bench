"""
World / merged / root-local AABB utilities, floor/ceiling predicates, raycast, room height.

Runs with ``bpy``. Used by ``asset_rules``, ``generate_lib.extract_gt``, ``overlap_check``.
"""

from __future__ import annotations

from typing import Any, Tuple


def bpy_importable() -> bool:
    try:
        import bpy  # noqa: F401

        return True
    except Exception:
        return False


def is_floor(obj) -> bool:
    return getattr(obj, "type", None) == "MESH" and obj.name.endswith(".floor")


def is_ceiling(obj) -> bool:
    return getattr(obj, "type", None) == "MESH" and obj.name.endswith(".ceiling")


def world_aabb(obj):
    import bpy
    import mathutils

    dg = bpy.context.evaluated_depsgraph_get()
    oe = obj.evaluated_get(dg)
    me = oe.to_mesh()
    if not me or not me.vertices:
        oe.to_mesh_clear()
        loc = oe.matrix_world.translation.copy()
        return loc, loc.copy()

    mw = oe.matrix_world
    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    for v in me.vertices:
        w = mw @ v.co
        for i in range(3):
            if w[i] < lo[i]:
                lo[i] = w[i]
            if w[i] > hi[i]:
                hi[i] = w[i]
    oe.to_mesh_clear()
    return lo, hi


def combined_aabb(objs):
    import mathutils

    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    valid = False
    for o in objs:
        if o.type != "MESH":
            continue
        try:
            a, b = world_aabb(o)
        except Exception:
            continue
        valid = True
        for i in range(3):
            if a[i] < lo[i]:
                lo[i] = a[i]
            if b[i] > hi[i]:
                hi[i] = b[i]
    if not valid:
        z = objs[0].matrix_world.to_translation() if objs else mathutils.Vector()
        return z.copy(), z.copy()
    return lo, hi


def combined_aabb_root_local(root, mesh_objs) -> Tuple[Any, Any]:
    """
    Transform each subtree MESH's **evaluated** vertices into **root local space**, then merge axis-aligned min/max.

    Components of ``dimensions = hi - lo`` align with **root-local X/Y/Z**, matching root yaw and avoiding
    world-axis AABB **padding** on tilted furniture. If ``matrix_world`` is singular, falls back to ``combined_aabb`` (world space).
    """
    import bpy
    import mathutils

    try:
        inv = root.matrix_world.inverted()
    except Exception:
        return combined_aabb(mesh_objs)

    dg = bpy.context.evaluated_depsgraph_get()
    INF = float("inf")
    lo = mathutils.Vector((INF, INF, INF))
    hi = mathutils.Vector((-INF, -INF, -INF))
    valid = False
    for o in mesh_objs:
        if o.type != "MESH":
            continue
        try:
            oe = o.evaluated_get(dg)
            me = oe.to_mesh()
            if not me or not me.vertices:
                oe.to_mesh_clear()
                continue
            mw = oe.matrix_world
            for v in me.vertices:
                pw = mw @ v.co
                pl = inv @ pw
                valid = True
                for i in range(3):
                    if pl[i] < lo[i]:
                        lo[i] = pl[i]
                    if pl[i] > hi[i]:
                        hi[i] = pl[i]
            oe.to_mesh_clear()
        except Exception:
            continue
    if not valid:
        z = mathutils.Vector((0.0, 0.0, 0.0))
        return z.copy(), z.copy()
    return lo, hi


def raycast_floor(origin, max_depth: float = 100.0, eps: float = 1e-3):
    import bpy
    import mathutils

    dg = bpy.context.evaluated_depsgraph_get()
    d = mathutils.Vector((0, 0, -1))
    cur = origin.copy()
    rem = max_depth
    while rem > 0:
        hit, loc, *_, obj, _ = bpy.context.scene.ray_cast(dg, cur, d, distance=rem)
        if not hit:
            return None, None
        if is_floor(obj):
            return obj, loc
        t = cur.z - loc.z
        if t <= 0:
            t = eps
        rem -= t
        cur = loc + d * eps
    return None, None


def room_height(room_id: str, floor_z_top: float) -> float:
    import bpy

    for o in bpy.data.objects:
        if is_ceiling(o) and o.name.startswith(room_id + "."):
            cmin, _ = world_aabb(o)
            return round(cmin.z - floor_z_top, 4)
    return 2.7
