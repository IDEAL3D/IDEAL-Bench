# 预处理设计（Box 单房）：`GT.json` 与 `objects_in_view.json`

**当前代码布局（已无统一预处理 CLI）**：在 Blender 内由 **`generation/generate_lib/extract_gt.py`** 写 **`GT.json`**。**`objects_in_view.json`** 有两种常见产生方式：（1）**推荐两阶段**：**`generation/filter_scenes.py`**（阶段 1，场景级：镜内语义 + 重叠）后接 **`generation/filter_inview.py`**（阶段 2，物体级：可见性 / 长宽比 / **`preprocess_lib.truncation_filter`** 截断），由 `filter_inview.py` 重写该 JSON，并在 `scenes-root` 写 **`inview_batch.jsonl`**；（2）在 Blender 内调用 **`preprocess_lib/extract_inview.write_info_json_v2`**，合并镜内列表与**家具重叠摘要**字段——与（1）写出顶层字段**可能不完全一致**（`filter_inview.py` 当前不写 `furniture_overlap_*`）。阶段 1 的镜内判定与 **`preprocess_lib/build_inview.build_instance_seg_view_payload`** 一致；阶段 2 的 F1/F2 使用 **`build_inview_with_filters`**。重叠检测子进程为 **`python -m preprocess_lib.check_overlap`**（阶段 1）。GT 字段形状见 `single_scene_schema.json`（若存在）。

**不写 GLB 导出管线**。`GT.json` / `objects_in_view.json` 顶层仅保留 **`scene_id`**（与场景目录名一致）及数据本体；**无** `schema_version`、`coordinate_frame` / `coordinate_summary`、`geometric_rules_ref`。字段语义与历史版本（曾用世界轴 `dimensions`、曾写冗长 `coordinate_frame`）见 **`geometric_rules_floor.md`** 与下文；当前 **`objects[].dimensions` 为根局部合并 AABB**。

**遗留文件名**：历史管线曾用 **`info.json`** 承载镜内信息；当前 canonical 为 **`objects_in_view.json`**（`preprocess_lib/defaults.OBJECTS_IN_VIEW_JSON`）。

**Blender 坐标系、矩阵含义、墙厚与地板挤出、资产原点习惯、在 Blender 中查看父子**等引擎侧内容见 Infinigen 仓库根 [**INFINIGEN_RULES.md**](../../INFINIGEN/INFINIGEN_RULES.md) **§七**。

---

## 历史：`preprocess_last.log` 与批处理 CLI

旧版 **`generation/preprocess.py`** + **`preprocess_lib/cli.py`** 会在 **`INFINIGEN/SCENES2TEST/preprocess_last.log`** 等路径**追加**批处理摘要；该入口与相关 **`--final`** / manifest 批处理逻辑已移除。筛选与重叠统计请使用 **`filter_scenes.py`** 写出的 CSV / JSONL（见 `preprocess_lib/defaults` 中的文件名常量）。

---

## 快速判断：场景里有没有「可报告的」家具重叠？

写出 **`objects_in_view.json`** 时，`extract_inview.write_info_json_v2` 会调用 `check_overlap.compute_furniture_overlap`（失败或未执行语义阶段时见下）。文件顶层提供摘要字段：

- **`furniture_overlap_pair_count`**：达到报告阈值的家具对数量（见下文 `min_reported_volume_m3`）。**`0`** 可能表示真无达到阈值的相交，也可能表示可参与检测的资产少于 2 个等（实现会仍标为已计算）。
- **`furniture_overlap_volume_m3_total`**（若写入）：各对估计相交体积之和。

**说明**：磁盘上的 JSON **不**包含 `check_overlap` 返回 dict 里的嵌套 `furniture_overlap` 块（无单独 `computed` 字段）。若不调用 **`write_info_json_v2`**（或未跑重叠步骤），则无从读上述字段。实现细节仅在 Blender 侧；算法表见下节。

---

## `objects_in_view.json`：镜内物体从何而来？

与旧实现（依赖 Blender `pass_index` 等）不同，**镜内列表**由磁盘上渲染产物 + 标注共同决定：

1. **`metadata.json`**：须存在且含 **`instance_map.object_index_map`**（`pass_index` 字符串 → 实例名）；缺失、无法解析或为空映射会抛错（`MissingInViewInfoError`）。缺 **`GT.json`** 或 **`instance_segmentation.png`** 时亦抛 `MissingInViewInfoError`（或底层读图仍可能 `FileNotFoundError` / `RuntimeError`）。
2. **`instance_segmentation.png`**：按 **整型 / uint16** 读取（`imageio` / `cv2.IMREAD_UNCHANGED` 等），逐 id 统计像素数；**`≥ MIN_VISIBLE_PIXELS`**（`configs/constant.py`）的 id 视为在分割中「可见」。
3. 用 **`object_index_map`** 将 id 换为 metadata 实例名，再与 **`GT.json`** 中物体 **`id`** 对齐；只对齐成功的条目进入 **`objects`**（并做类别 canonical，与 GT 一致）。
4. **不**使用 `bpy.object.pass_index` 枚举镜内物体。

`preprocess_lib/extract_inview.write_info_json_v2(blend_dir)` 仅接收场景目录路径；**不再**传入相机对象（镜内不依赖相机句柄）。

---

## `objects_in_view.json` 顶层（当前管线）

首字段为 **`scene_id`**（与 `GT.json` 相同）。随后为：

| 字段 | 含义 |
|------|------|
| **`categories_in_view` / `categories_in_view_count`** | 镜内物体类别列表及计数。 |
| **`objects_in_view_count` / `objects`** | 镜内物体条目数；**`objects`** 为 **`GT.json`** 子集的深拷贝（字段形状与 GT 一致）。 |
| **`furniture_overlap_pair_count`** | 家具重叠检测摘要：报告对数。 |
| **`furniture_overlap_volume_m3_total`** | 可选；总估计相交体积（m³）。 |

**不**写顶层 **`image`**、**`intrinsic` / `extrinsic` / `blender`** 相机块（与旧 `extract_camview` 全量载荷不同）。**不**写入 `schema_version`、`coordinate_frame`、`distortion`、`camera_setup`（全数据一致，见 **`IDEAL/docs/geometric_rules_floor.md`**）。若需外参与 OpenCV 约定，仍以 Infinigen / 生成侧为准，本文不展开。

---

## `objects_in_view.json` 中的家具重叠（算法语义）

由 `extract_inview` 将 `compute_furniture_overlap` 的**摘要数字**写入顶层；重叠的**完整**结构（含 **`pairs`**、`method` 等）存在于函数返回的 dict 中，**未**完整序列化到 `objects_in_view.json`。返回 dict 各字段含义如下（与实现 `check_overlap.py` 一致；**`method`** 等处以代码为准）。

| 字段 | 含义 |
|------|------|
| **`computed`** | 正常返回时为 **`true`**（资产过少等情况仍可能 `pair_count == 0`）。 |
| **`method`** | 粗筛 + Blender Boolean + 体素/顶点距离回退；具体字符串见实现。 |
| **`voxel_resolution`** | 体素为均匀立方体、**每对家具自适应**步长；单对体素数有上限，避免极慢。 |
| **`min_reported_volume_m3`** | 小于该体积（默认 `1e-6` m³）的相交**不写入** `pairs`。 |
| **`asset_count`** | 参与检测的**顶层家具**数量（与 `GT.json` 的 `unique_assets` 收集规则一致，并排除镜面墙画等类别）。 |
| **`pair_count`** | 达到报告阈值的**家具对**数量（写入文件时对应 **`furniture_overlap_pair_count`**）。 |
| **`overlap_volume_m3_total`** | 各对 `overlap_volume_m3` 之和（写入文件时对应 **`furniture_overlap_volume_m3_total`**）。 |
| **`pairs`** | 列表项含 `a` / `b`（物体 `id`）、`category_a` / `category_b`、`overlap_volume_m3`、`voxel_count`、`detection` 等（**仅内存/日志**）。 |
| **`skipped_category_pairs`** | 算法中**不参与穿模判定**的类别对说明（如床 + 床垫视为一体）。 |

算法主流程：**世界系 AABB 两两相交**（粗筛；与 `GT.json` 的**根局部** `dimensions` 定义不同）→ 候选对 **Blender Boolean（INTERSECT）** → 失败则 **体素** 与 **顶点–表面距离** 回退。详见 `check_overlap` 模块注释。

---

# `GT.json` 几何提取逻辑

## 字段一览（语义速查，与 Blender 对象对齐）

| 字段 | 含义 |
|------|------|
| `scene_id` | 场景目录名（如 `kitchen_medium_s1759680675`），与 `objects_in_view.json` 中同名字段一致。 |
| `scene.room.size` | `[W, D, H]`（米）：`unique_assets:room_floor` 内地板网格合并世界 AABB 的 XY 跨度 + 由 `.ceiling` 推算的高度。 |
| `scene.room.type` | 场景目录名按下划线分段后的**首段**、小写，且须在 **`configs.constant.ROOM_TYPES`** 中；否则打 **`[WARN]`** 并写 **`"unknown"`**（**不做** `openoffice`→`office` 等别名映射）。 |
| `scene.room.asset_count` | `objects` 数组长度。 |
| `objects[].id` | 根物体名（如 `…Factory(…).spawn_asset(…)`）。 |
| `objects[].position` | 根物体 **`matrix_world.to_translation()`**（米），即**物体原点**的世界坐标，**不是**几何中心或贴地点。（矩阵含义见 INFINIGEN_RULES §7.2） |
| `objects[].dimensions` | 根物体下**所有 MESH 子树**在**资产根局部坐标系**中合并的轴对齐包围盒边长 `[dx, dy, dz]`（米），沿**根局部 X/Y/Z**。与绕 Z 斜放时世界轴外接盒不同；约定见 **`IDEAL/docs/geometric_rules_floor.md`**。 |
| `objects[].rotation_yaw` | 语义前向相对 +Y 的整数度（`extract_gt.semantic_yaw_degrees`），**不是**简单的 `matrix_world` 欧拉 Z。无 Front 时默认用根局部 **+X** 回退；**`blackboard`** 用根局部 **+Y**（仅 GT）。**`side_table`** 在水平近似正方形时固定为 `0`。**灯具**（`lamp` / `*_lamp`）与类别名中含 **`_plant_`** 的（如 `large_plant_container`）为 **`null`**（无比对意义）。 |

下文 §1 起为**实现细节**（与上表一致）。

---

## 1. 写入哪些物体？

- 集合 **`unique_assets`** 中、**父级为 `None` 的顶层物体**。
- 排除位于下列 **collection** 内的一切物体名（墙、地、顶、外墙、楼梯、门窗、门套、踢脚线等），以及名称以 `door_base_elements` 为前缀的 collection。定义见 `generation_common/asset_rules.py` 中的 `ASSET_GT_EXCLUDE_COLLECTIONS` 等。
- 每个物体一条记录；**`id` = 该根物体在 Blender 中的唯一名称**（如 `OfficeChairFactory(…).spawn_asset(…)`）。

---

## 2. `scene.room`

| 字段 | 计算方式 |
|------|----------|
| `type` | 目录基名按 `_` 首段、`lower()`；若首段在 **`ROOM_TYPES`** 中则采用，否则 **`unknown`** 并 **`[WARN] unexpected room_type prefix …`**。空目录名则为 **`unknown`**（不打 WARN）。 |
| `size` | `[W, D, H]`（米）。优先用 `unique_assets:room_floor` 内所有 `*.floor` **MESH** 的**合并世界 AABB**：`W/D` 为 XY 方向跨度，`H` 由同房间 `.ceiling` 与地板顶面 Z 通过 `room_height` 推算（见 `generate_lib/extract_gt.resolve_room_metrics_box`）。若不可用则回退射线或最大地板网格。 |
| `asset_count` | 等于本文件中 `objects` 数组长度。 |

---

## 3. 每件物体：`position` / `dimensions` / `rotation_yaw`

### `position`（米，保留两位小数）

- 根物体的 **`matrix_world.to_translation()`**。
- 即 **物体原点（Object Origin）在世界坐标中的位置**，与 Transform 面板 **Location（世界空间）** 在无父级时一致。
- **不是**几何中心、**不是**包围盒底面；Infinigen 各资产原点位置不统一。

### `dimensions`（米，保留两位小数，根局部）

对**该根物体及其整条子树**：

1. 取 **`[根] + children_recursive`** 中所有 **`type == "MESH"`** 的对象（含根自身若为 MESH）。
2. 对各 MESH 的 **evaluated** 顶点先变到**世界坐标**，再左乘 **`inv(root.matrix_world)`** 得到**根物体局部坐标**，在根局部 XYZ 上合并 **min/max**（`generation_common/scene_geometry.combined_aabb_root_local`）。
3. **`dimensions = [hi.x - lo.x, hi.y - lo.y, hi.z - lo.z]`** 为沿**根局部 X/Y/Z** 的跨度；绕竖直轴斜放时不再用世界轴外接盒膨胀。

**复合物体（如床 = 床架根 + 床垫子 MESH）**：并集仍在**根局部**下计算，与侧边栏「仅父物体网格」仍可能不一致，但轴向与根的 Transform 一致。

若无任何 MESH（极少见）：用根物体 **evaluated** 的 **`bound_box`** 在**物体局部**上的跨度。

### `rotation_yaw`（整数 0–359，或 JSON `null`）

- 由语义前向与 `semantic_yaw_degrees` 等逻辑得到（见 `extract_gt.py`）；非简单欧拉 Z 单一来源。
- **`null`**：`lamp` / `*_lamp`，或 `category` 中含 `_plant_` 片段（如 `large_plant_container`）；评测应跳过 yaw。

### `category`

- 由根物体名解析：`generation_common/asset_rules.factory_to_category`（如 `OfficeChairFactory…` → `office_chair`）。

---

## 4. 与 Blender 面板的常见误解

- **`dimensions`**：GT 为 **根 + 全部后代 MESH 在根局部系下的合并 AABB 边长**；侧边栏对父级常**不含子网格**，二者不可直接等同。嵌套子物体仍会计入 GT，一般不必再手工叠加子物体尺寸。根节点可能是 Empty 下挂 Mesh，也可能根即 MESH；以具体 `.blend` 为准（概览见 INFINIGEN_RULES §7.4）。
- **`position.z`**：`position` 是**物体原点**高度，不是脚底、不是包围盒底面；不同资产原点位置差异很大（见 INFINIGEN_RULES §7.3）。室内可走平面大致在 **z ≈ 0.10～0.11 m** 量级（与生成侧墙厚/挤出相关，见 INFINIGEN_RULES §7.6）。多数「落地」资产接近地板顶面高度，但因原点定义不同，**不必**等于地板 Z；床等原点常在床体中部附近，**会更高**。
- **若评测需要贴地高度**：应对网格取世界系 **`combined_aabb` 的 `lo.z`（世界包围盒底面）**，而不是只用 `position.z`。`GT.json` 的 `dimensions` 为根局部，**不**直接给出世界底面 Z。

---

## 5. 单位与精度

- 场景单位为**米**。
- 位置与 `dimensions` 分量一般 **四舍五入到 0.01 m**（两位小数）；`rotation_yaw` 为**整度**或 **`null`**（见上）。
- 存在 **Modifier** 时，顶点级 AABB 与仅看 `bound_box` 可能略有差异；`extract_gt` 对网格使用 **evaluated mesh** 算 AABB（见 `generate_lib/extract_gt.py` 与 `generation_common/scene_geometry.py`；算法见上文 §3）。

---

## 6. 废弃说明

- 旧版文档中的 **GLB 锚点烘焙、`scene_GT.json` 底面中心** 等已废弃；评测以当前 **`GT.json`** 与 **`objects_in_view.json`**、**本文** 为准。
- 曾用的 **`info.json`** 相机大段与独立 **`furniture_overlap.json`** 侧车为遗留布局；当前以 **`objects_in_view.json`** 为准，重叠摘要为顶层数字字段。
