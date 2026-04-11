"""
Pairwise overlap estimate for top-level furniture under ``unique_assets``; ``extract_inview`` merges the summary into ``objects_in_view.json`` (legacy pipeline used a standalone ``furniture_overlap.json`` or an ``info.json`` block).

Uses asset enumeration from ``generation_common.asset_rules`` and AABB from ``generation_common.scene_geometry``; invoked by ``extract_inview`` as ``compute_furniture_overlap``.

**Subprocess CLI** (Blender Python only), for ``filter_scenes.py``::

 cd IDEAL/generation && <bpy_python> -m preprocess_lib.check_overlap --filter-write-json <scene.blend> <out.json>

Writes ``{"pair_count": int, "computed": bool, "error": str|null}`` to ``out.json``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from generation_common.asset_rules import collect_assets, factory_to_category
from generation_common.scene_geometry import combined_aabb

# Pairwise overlap: world-axis-aligned voxel grid; cap voxels per pair for runtime
OVERLAP_MAX_VOXELS_PER_PAIR = 120_000
OVERLAP_MIN_VOXEL_M = 0.015
OVERLAP_MIN_VOLUME_M3 = 1e-6
# Voxel inside test fails on open racks / thin shells / non-watertight mesh; vertex-to-surface distance fallback
OVERLAP_PROXIMITY_THRESHOLD_M = 0.045
OVERLAP_PROXIMITY_MAX_VERTS = 600
# Pairs found only via fallback: report a placeholder volume (still above min_reported)
OVERLAP_PROXIMITY_REPORT_VOLUME_M3 = 2e-5
# Bed + mattress treated as one asset; skip penetrant pair (matches GT top-level categories)
OVERLAP_SKIP_CATEGORY_PAIRS = frozenset({frozenset({"bed", "mattress"})})
# Categories excluded from pairwise overlap (names match GT top-level ``factory_to_category``)
# Rugs / wall art / mirrors + envelope and openings (skip even if still under unique_assets top level)
OVERLAP_EXCLUDE_CATEGORIES = frozenset(
    {
        "rug",
        "wall_art",
        "mirror",
        "wall",
        "floor",
        "ceiling",
        "room_wall",
        "room_floor",
        "room_ceiling",
        "room_exterior",
        "door",
        "sim_door",
        "panel_door",
        "glass_panel_door",
        "lite_door",
        "louver_door",
        "base_door",
        "window",
    }
)


# Top-level unique_assets furniture: AABB → Boolean / voxels / vertex distance


def _collect_mesh_bvhs_world(root) -> List:
    """One world-space BVHTree per MESH descendant (consistent with ``collect_assets`` roots)."""
    import bpy
    from mathutils.bvhtree import BVHTree

    dg = bpy.context.evaluated_depsgraph_get()
    out: List = []
    for o in [root] + list(root.children_recursive):
        if o.type != "MESH" or not o.data:
            continue
        try:
            bvh = BVHTree.FromObject(o, dg)
            out.append(bvh)
        except Exception:
            continue
    return out


def _point_inside_bvh(bvh, co) -> bool:
    """Odd-even ray test for closed mesh; ``co`` is world position."""
    from mathutils import Vector

    direction = Vector((1.0, 0.0, 0.0))
    pos = Vector(co)
    crossings = 0
    for _ in range(1024):
        hit = bvh.ray_cast(pos, direction)
        if hit is None:
            break
        loc = hit[0] if isinstance(hit, (tuple, list)) else getattr(hit, "location", None)
        if loc is None:
            break
        crossings += 1
        pos = loc + direction * 2e-4
    return crossings % 2 == 1


def _point_inside_asset(bvhs, co) -> bool:
    return any(_point_inside_bvh(bvh, co) for bvh in bvhs)


def _min_dist_to_bvhs(co: Tuple[float, float, float], bvhs) -> float:
    """Shortest distance from point to mesh surfaces represented by BVHTrees."""
    from mathutils import Vector

    v = Vector(co)
    best = float("inf")
    for bvh in bvhs:
        hit = bvh.find_nearest(v)
        if hit is None:
            continue
        if isinstance(hit, tuple) and len(hit) >= 4:
            if hit[0] is None:
                continue
            d = float(hit[3])
        else:
            continue
        if d < best:
            best = d
    return best if best < float("inf") else 1e9


def _world_vertices_sampled(root, max_count: int) -> List[Tuple[float, float, float]]:
    """All mesh vertices under root in world space; if too many, stride-sample deterministically."""
    import bpy

    dg = bpy.context.evaluated_depsgraph_get()
    all_v: List[Tuple[float, float, float]] = []
    for o in [root] + list(root.children_recursive):
        if o.type != "MESH" or not o.data:
            continue
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        mw = o.matrix_world
        for vert in me.vertices:
            wco = mw @ vert.co
            all_v.append((float(wco.x), float(wco.y), float(wco.z)))
        ev.to_mesh_clear()
    if not all_v:
        return []
    if len(all_v) <= max_count:
        return all_v
    step = max(1, len(all_v) // max_count)
    return all_v[::step][:max_count]


def _ensure_entry_bvhs(e: Dict[str, Any]) -> List:
    if e.get("bvhs") is None:
        e["bvhs"] = _collect_mesh_bvhs_world(e["root"])
    return e["bvhs"]


def _pair_overlap_vertex_proximity(
    ea: Dict[str, Any], eb: Dict[str, Any], threshold_m: float
) -> bool:
    """True if sampled vertices on either side lie within ``threshold_m`` of the opposite mesh surface (voxel supplement)."""
    _ensure_entry_bvhs(ea)
    _ensure_entry_bvhs(eb)
    verts_a = _world_vertices_sampled(ea["root"], OVERLAP_PROXIMITY_MAX_VERTS)
    verts_b = _world_vertices_sampled(eb["root"], OVERLAP_PROXIMITY_MAX_VERTS)
    for co in verts_a:
        if _min_dist_to_bvhs(co, eb["bvhs"]) < threshold_m:
            return True
    for co in verts_b:
        if _min_dist_to_bvhs(co, ea["bvhs"]) < threshold_m:
            return True
    return False


def _aabb_intersection(
    lo_a, hi_a, lo_b, hi_b
) -> Optional[Tuple[Any, Any]]:
    """Intersection of two axis-aligned boxes; ``None`` if disjoint."""
    from mathutils import Vector

    lo = Vector(
        (
            max(lo_a.x, lo_b.x),
            max(lo_a.y, lo_b.y),
            max(lo_a.z, lo_b.z),
        )
    )
    hi = Vector(
        (
            min(hi_a.x, hi_b.x),
            min(hi_a.y, hi_b.y),
            min(hi_a.z, hi_b.z),
        )
    )
    if lo.x >= hi.x - 1e-9 or lo.y >= hi.y - 1e-9 or lo.z >= hi.z - 1e-9:
        return None
    return lo, hi


def _merge_asset_world_mesh_data(root, mesh_name: str):
    """Merge all subtree MESH geometry into one mesh datablock expressed in **world space** at object origin."""
    import bmesh
    import bpy

    dg = bpy.context.evaluated_depsgraph_get()
    bm = bmesh.new()
    for o in [root] + list(root.children_recursive):
        if o.type != "MESH" or not o.data:
            continue
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        bm_temp = bmesh.new()
        bm_temp.from_mesh(me)
        bmesh.ops.transform(bm_temp, matrix=o.matrix_world, verts=bm_temp.verts)
        vert_map = {}
        for v in bm_temp.verts:
            vert_map[v.index] = bm.verts.new(v.co.copy())
        for f in bm_temp.faces:
            try:
                bm.faces.new([vert_map[v.index] for v in f.verts])
            except ValueError:
                pass
        bm_temp.free()
        ev.to_mesh_clear()
    if not bm.faces:
        bm.free()
        return None
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-4)
    bm.normal_update()
    me_out = bpy.data.meshes.new(mesh_name)
    bm.to_mesh(me_out)
    me_out.update()
    bm.free()
    return me_out


def _boolean_intersection_volume_m3(root_a, root_b) -> Optional[float]:
    """
    Temporary objects + Boolean(INTERSECT, EXACT) intersection volume; returns ``None`` on failure.
    Empty intersection (e.g. chair/table AABB overlap but meshes disjoint) returns ``0.0``.

    **Does not modify scene assets**: ``root_a``/``root_b`` only feed ``_merge_asset_world_mesh_data`` to **allocate** mesh data;
    Boolean runs on ``_ov_*`` temp objects only. ``finally`` removes those temp objects/meshes, not original furniture datablocks.
    """
    import bmesh
    import bpy
    import uuid
    from mathutils import Matrix

    col_name = "_FurnitureOverlapTemp"
    col = bpy.data.collections.get(col_name)
    if col is None:
        col = bpy.data.collections.new(col_name)
        bpy.context.scene.collection.children.link(col)
    col.hide_viewport = True
    col.hide_render = True

    uid = uuid.uuid4().hex[:10]
    mesh_a_name = f"_ov_{uid}_a"
    mesh_b_name = f"_ov_{uid}_b"
    obj_a_name = f"_ov_{uid}_a"
    obj_b_name = f"_ov_{uid}_b"

    obj_a = obj_b = None
    me_a = me_b = None
    try:
        me_a = _merge_asset_world_mesh_data(root_a, mesh_a_name)
        me_b = _merge_asset_world_mesh_data(root_b, mesh_b_name)
        if me_a is None or me_b is None:
            return None
        if len(me_a.vertices) == 0 or len(me_b.vertices) == 0:
            return None

        obj_a = bpy.data.objects.new(obj_a_name, me_a)
        obj_b = bpy.data.objects.new(obj_b_name, me_b)
        obj_a.matrix_world = Matrix.Identity(4)
        obj_b.matrix_world = Matrix.Identity(4)
        col.objects.link(obj_a)
        col.objects.link(obj_b)

        mod = obj_a.modifiers.new(name="bool_ov", type="BOOLEAN")
        mod.operation = "INTERSECT"
        mod.object = obj_b
        if hasattr(mod, "solver"):
            mod.solver = "EXACT"

        bpy.ops.object.select_all(action="DESELECT")
        obj_a.select_set(True)
        bpy.context.view_layer.objects.active = obj_a
        bpy.ops.object.modifier_apply(modifier=mod.name)

        bm = bmesh.new()
        bm.from_mesh(obj_a.data)
        if len(bm.faces) == 0:
            bm.free()
            return 0.0
        vol = abs(float(bm.calc_volume(signed=True)))
        bm.free()
        if vol != vol:  # NaN
            return None
        return vol
    except Exception as ex:
        print(f"[check_overlap] boolean overlap skipped: {ex}")
        return None
    finally:
        # Do not hold Mesh RNA across modifier/remove then access .name (ReferenceError).
        for ob in (obj_a, obj_b):
            if ob is None:
                continue
            try:
                oname = ob.name
            except ReferenceError:
                continue
            if oname in bpy.data.objects:
                bpy.data.objects.remove(bpy.data.objects[oname], do_unlink=True)
        for mname in (mesh_a_name, mesh_b_name):
            m = bpy.data.meshes.get(mname)
            if m is not None and m.users == 0:
                bpy.data.meshes.remove(m)


def _purge_furniture_overlap_temp_collection() -> None:
    """
    After overlap detection, remove hidden collection ``_FurnitureOverlapTemp`` and any leftovers.

    Cleans only temporaries created here; **does not** traverse or delete original objects under ``unique_assets``.
    """
    import bpy

    name = "_FurnitureOverlapTemp"
    col = bpy.data.collections.get(name)
    if col is None:
        return
    root = bpy.context.scene.collection
    for ob in list(col.objects):
        me = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if me is not None and me.users == 0:
            try:
                bpy.data.meshes.remove(me)
            except ReferenceError:
                pass
    # bpy_prop_collection.__contains__ expects collection name strings, not ``col in root.children``
    if col.name in root.children:
        root.children.unlink(col)
    try:
        bpy.data.collections.remove(col)
    except ReferenceError:
        pass


def _overlap_volume_voxels(
    bvhs_a,
    bvhs_b,
    lo,
    hi,
    voxel_size: float,
) -> Tuple[float, int]:
    """Uniform cube voxels on ``[lo,hi]``; count samples inside both asset meshes; volume = count × cell volume."""
    import math

    from mathutils import Vector

    dx = hi.x - lo.x
    dy = hi.y - lo.y
    dz = hi.z - lo.z
    vol_box = dx * dy * dz
    if vol_box <= 0:
        return 0.0, 0

    s = float(voxel_size)
    s = max(s, OVERLAP_MIN_VOXEL_M)
    # Cap total voxel count
    est = int(math.ceil(dx / s) * math.ceil(dy / s) * math.ceil(dz / s))
    if est > OVERLAP_MAX_VOXELS_PER_PAIR and vol_box > 0:
        s = max(OVERLAP_MIN_VOXEL_M, (vol_box / OVERLAP_MAX_VOXELS_PER_PAIR) ** (1.0 / 3.0))

    nx = max(1, int(math.ceil(dx / s)))
    ny = max(1, int(math.ceil(dy / s)))
    nz = max(1, int(math.ceil(dz / s)))
    est2 = nx * ny * nz
    if est2 > OVERLAP_MAX_VOXELS_PER_PAIR:
        s = max(OVERLAP_MIN_VOXEL_M, (vol_box / OVERLAP_MAX_VOXELS_PER_PAIR) ** (1.0 / 3.0))
        nx = max(1, int(math.ceil(dx / s)))
        ny = max(1, int(math.ceil(dy / s)))
        nz = max(1, int(math.ceil(dz / s)))

    count = 0
    sx = dx / nx
    sy = dy / ny
    sz = dz / nz
    for ix in range(nx):
        x = lo.x + (ix + 0.5) * sx
        for iy in range(ny):
            y = lo.y + (iy + 0.5) * sy
            for iz in range(nz):
                z = lo.z + (iz + 0.5) * sz
                co = Vector((x, y, z))
                if _point_inside_asset(bvhs_a, co) and _point_inside_asset(bvhs_b, co):
                    count += 1

    cell_vol = sx * sy * sz
    return count * cell_vol, count


def _skip_overlap_pair(cat_a: str, cat_b: str) -> bool:
    return frozenset({cat_a, cat_b}) in OVERLAP_SKIP_CATEGORY_PAIRS


def compute_furniture_overlap() -> Dict[str, Any]:
    """
    Top-level ``unique_assets`` entries: filter categories, then **O(n²)** world AABB intersection;
    for intersecting pairs, **Boolean INTERSECT** volume; on Boolean failure, voxel grid + vertex-distance fallback.
    Boolean volume below threshold counts as no penetration (e.g. stacked AABBs but separated meshes).

    Never deletes original assets; ends with ``_purge_furniture_overlap_temp_collection`` for temporaries.
    """
    try:
        return _compute_furniture_overlap_impl()
    finally:
        _purge_furniture_overlap_temp_collection()


def _compute_furniture_overlap_impl() -> Dict[str, Any]:
    assets = collect_assets()
    entries: List[Dict[str, Any]] = []
    for root in sorted(assets, key=lambda o: o.name):
        cat = factory_to_category(root.name)
        if cat in OVERLAP_EXCLUDE_CATEGORIES:
            continue
        hier = [root] + list(root.children_recursive)
        meshes = [o for o in hier if o.type == "MESH"]
        if not meshes:
            continue
        lo, hi = combined_aabb(meshes)
        entries.append(
            {
                "id": root.name,
                "root": root,
                "category": cat,
                "lo": lo,
                "hi": hi,
                "bvhs": None,
            }
        )

    n = len(entries)
    if n < 2:
        return {
            "computed": True,
            "method": "aabb_pairs_then_mesh_boolean",
            "asset_count": n,
            "pair_count": 0,
            "overlap_volume_m3_total": 0.0,
            "pairs": [],
            "notes": "Fewer than 2 mesh assets after category filter; no pairs.",
        }

    pairs_out: List[Dict[str, Any]] = []
    total_vol = 0.0
    voxel_size_base = OVERLAP_MIN_VOXEL_M * 2.0

    for i in range(n):
        for j in range(i + 1, n):
            ea, eb = entries[i], entries[j]
            if _skip_overlap_pair(ea["category"], eb["category"]):
                continue
            box = _aabb_intersection(ea["lo"], ea["hi"], eb["lo"], eb["hi"])
            if box is None:
                continue
            lo, hi = box
            dx = hi.x - lo.x
            dy = hi.y - lo.y
            dz = hi.z - lo.z
            s = max(
                OVERLAP_MIN_VOXEL_M,
                voxel_size_base,
                (dx * dy * dz / OVERLAP_MAX_VOXELS_PER_PAIR) ** (1.0 / 3.0)
                if dx * dy * dz > 0
                else OVERLAP_MIN_VOXEL_M,
            )

            bool_vol = _boolean_intersection_volume_m3(ea["root"], eb["root"])
            vol: Optional[float] = None
            vox_n = 0
            detection = ""

            if bool_vol is not None:
                if bool_vol < OVERLAP_MIN_VOLUME_M3:
                    continue
                vol = float(bool_vol)
                detection = "mesh_boolean"
            else:
                _ensure_entry_bvhs(ea)
                _ensure_entry_bvhs(eb)
                vvol, vox_n = _overlap_volume_voxels(
                    ea["bvhs"], eb["bvhs"], lo, hi, s
                )
                detection = "voxel_grid"
                vol = vvol
                if vol < OVERLAP_MIN_VOLUME_M3:
                    if _pair_overlap_vertex_proximity(
                        ea, eb, OVERLAP_PROXIMITY_THRESHOLD_M
                    ):
                        vol = OVERLAP_PROXIMITY_REPORT_VOLUME_M3
                        vox_n = 0
                        detection = "vertex_surface_proximity"
                    else:
                        continue

            total_vol += vol
            pairs_out.append(
                {
                    "a": ea["id"],
                    "b": eb["id"],
                    "category_a": ea["category"],
                    "category_b": eb["category"],
                    "overlap_volume_m3": round(vol, 6),
                    "voxel_count": vox_n,
                    "detection": detection,
                }
            )

    pairs_out.sort(key=lambda p: -p["overlap_volume_m3"])

    print(
        f"[check_overlap] furniture_overlap: {len(pairs_out)} pair(s), "
        f"total_volume≈{total_vol:.6f} m³ (AABB → boolean; fallback voxel/proximity)"
    )

    excl = sorted(OVERLAP_EXCLUDE_CATEGORIES)
    return {
        "computed": True,
        "method": "aabb_pair_filter_mesh_boolean_exact_fallback_voxel_proximity",
        "aabb_rule": (
            "world-space combined mesh AABB per asset (broad-phase only; "
            "GT dimensions are root-local merged AABB — see PREPROCESS_DESIGN.md)"
        ),
        "excluded_categories": excl,
        "voxel_resolution": "adaptive per pair (uniform cubes; cap %d voxels)"
        % OVERLAP_MAX_VOXELS_PER_PAIR,
        "proximity_fallback_m": OVERLAP_PROXIMITY_THRESHOLD_M,
        "proximity_report_volume_m3": OVERLAP_PROXIMITY_REPORT_VOLUME_M3,
        "min_reported_volume_m3": OVERLAP_MIN_VOLUME_M3,
        "asset_count": n,
        "pair_count": len(pairs_out),
        "overlap_volume_m3_total": round(total_vol, 6),
        "pairs": pairs_out,
        "skipped_category_pairs": ["bed+mattress"],
        "pair_fields": (
            "detection: mesh_boolean | voxel_grid | vertex_surface_proximity; "
            "boolean failure uses voxel then proximity"
        ),
    }


def _filter_scenes_cli() -> int:
    """``python -m preprocess_lib.check_overlap --filter-write-json <blend> <out.json>`` (requires ``bpy``)."""
    import json
    import os
    import sys

    av = sys.argv
    if len(av) < 4 or av[1] != "--filter-write-json":
        print(
            "usage: python -m preprocess_lib.check_overlap --filter-write-json <scene.blend> <out.json>",
            file=sys.stderr,
        )
        return 2

    blend_path = os.path.abspath(os.path.expanduser(av[2]))
    out_path = os.path.abspath(os.path.expanduser(av[3]))
    payload: Dict[str, Any] = {"pair_count": None, "computed": False, "error": None}

    if not os.path.isfile(blend_path):
        payload["error"] = f"missing_blend:{blend_path}"
        _overlap_cli_write_json(out_path, payload)
        return 1

    try:
        import bpy
    except ImportError:
        payload["error"] = "no_bpy"
        _overlap_cli_write_json(out_path, payload)
        return 1

    try:
        bpy.ops.wm.open_mainfile(filepath=blend_path)
    except Exception as e:
        payload["error"] = f"open_blend:{type(e).__name__}:{e}"
        _overlap_cli_write_json(out_path, payload)
        return 1

    try:
        fo = compute_furniture_overlap()
        payload["computed"] = bool(fo.get("computed"))
        payload["pair_count"] = int(fo.get("pair_count", 0) or 0)
        pairs_raw = fo.get("pairs") if isinstance(fo.get("pairs"), list) else []
        overlap_pair_strings: List[str] = []
        for p in pairs_raw:
            if not isinstance(p, dict):
                continue
            a, b = p.get("a"), p.get("b")
            if a is not None and b is not None:
                overlap_pair_strings.append(f"{a}|{b}")
        payload["overlap_pair_strings"] = overlap_pair_strings
    except Exception as e:
        payload["error"] = f"overlap:{type(e).__name__}:{e}"
        _overlap_cli_write_json(out_path, payload)
        return 1

    _overlap_cli_write_json(out_path, payload)
    return 0


def _overlap_cli_write_json(path: str, payload: Dict[str, Any]) -> None:
    import json
    import os

    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


if __name__ == "__main__":
    raise SystemExit(_filter_scenes_cli())
