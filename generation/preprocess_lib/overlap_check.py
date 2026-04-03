"""
``unique_assets`` 顶层家具两两重叠估计（与 ``info.json`` 的 ``furniture_overlap`` 对应）。

依赖 ``extract_camview`` 的资产枚举与 AABB；由 ``extract_info`` 调用 ``compute_furniture_overlap``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .extract_camview import collect_assets, combined_aabb, factory_to_category

# 家具两两重叠：体素网格（世界轴对齐）；单对最大体素数以控耗时
OVERLAP_MAX_VOXELS_PER_PAIR = 120_000
OVERLAP_MIN_VOXEL_M = 0.015
OVERLAP_MIN_VOLUME_M3 = 1e-6
# 体素法在「开放格架 / 薄壳 / 非闭合 mesh」上奇偶射线会失效；用顶点–对侧表面距离回退
OVERLAP_PROXIMITY_THRESHOLD_M = 0.045
OVERLAP_PROXIMITY_MAX_VERTS = 600
# 仅由回退检出的对：报告体积用占位值（满足 min_reported 阈值即可）
OVERLAP_PROXIMITY_REPORT_VOLUME_M3 = 2e-5
# 床 + 床垫视为一体，不计入穿模对（与 GT 顶层资产类别一致）
OVERLAP_SKIP_CATEGORY_PAIRS = frozenset({frozenset({"bed", "mattress"})})
# 不参与两两重叠检测的类别（与 GT 顶层 ``factory_to_category`` 名一致）
# 地毯/墙饰/镜子 + 围护与洞口（即便个别场景下仍挂在 unique_assets 顶层也跳过）
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


# ── unique_assets 顶层家具：AABB → Boolean / 体素 / 顶点距离 ────────────────


def _collect_mesh_bvhs_world(root) -> List:
    """每个 MESH 子对象一棵世界空间 BVHTree（与 ``collect_assets`` 根一致）。"""
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
    """闭网格奇偶射线法；``co`` 为世界坐标。"""
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
    """点到多棵 BVHTree 所代表 mesh 表面的最短距离。"""
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
    """根物体子树内所有 mesh 顶点（世界坐标），过多时按步长均匀抽样（可复现）。"""
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
    """任一侧抽样顶点与另一侧 mesh 表面距离 < threshold 则视为几何穿透/贴合（体素法补充）。"""
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
    """两轴对齐盒的相交子盒；不相交则 ``None``。"""
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
    """将根物体子树内所有 MESH 合并为**世界坐标**下的单块 mesh 数据（物体原点处）。"""
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
    临时物体 + Boolean(INTERSECT, EXACT) 求交体积；失败返回 ``None``。
    交为空（桌椅 AABB 重叠但 mesh 不交）返回 ``0.0``。

    **不修改、不删除场景资产**：``root_a``/``root_b`` 仅用于 ``_merge_asset_world_mesh_data``
    读取几何并 **新建** mesh；布尔只作用于 ``_ov_*`` 临时物体。``finally`` 里删除的永远是
    这些临时 object/mesh，与原装家具 datablock 无关。
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
        print(f"[overlap_check] boolean overlap skipped: {ex}")
        return None
    finally:
        # 勿在 modifier / remove 之后持有 Mesh RNA 引用再访问 .name（会 ReferenceError）
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
    重叠检测结束后拆掉隐藏集合 ``_FurnitureOverlapTemp`` 及其中残留物。

    仅清理本模块创建的临时数据；**不**遍历或删除 ``unique_assets`` 下的原装物体。
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
    # bpy_prop_collection.__contains__ 只接受集合名字符串，不能写 ``col in root.children``
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
    """在 ``[lo,hi]`` 上均匀立方体素，数同时在两资产 mesh 内的体素；体积 = 计数 × s³。"""
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
    # 控制体素总数
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
    ``unique_assets`` 顶层资产：先过滤类别，再 **O(n²)** 世界 AABB 两两相交；
    仅对相交对做 **Boolean INTERSECT** 体积；布尔失败时再体素 + 顶点距离回退。
    布尔体积 < 阈值视为无穿模（如桌椅 AABB 叠但 mesh 不交）。

    全程不删除原装资产；结束后调用 ``_purge_furniture_overlap_temp_collection`` 清掉临时集合。
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
        f"[overlap_check] furniture_overlap: {len(pairs_out)} pair(s), "
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
