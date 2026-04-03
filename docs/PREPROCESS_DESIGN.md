# 预处理设计（Box 单房）：`GT.json` 与 `info.json`

实现入口：`generation/preprocess.py` → `preprocess_lib/extract_gt.py`（GT）、`preprocess_lib/extract_info.py`（组装并写出 `info.json`，镜头块来自 `extract_camview`）；调度见 `preprocess_lib/cli`、`preprocess_lib/orchestrator`、`preprocess_lib/pipeline`。GT 字段形状见 `single_scene_schema.json`（若存在）。

**不写 GLB 导出管线**。`GT.json` / `info.json` 顶层仅保留 **`scene_id`**（与场景目录名一致）及数据本体；**无** `schema_version`、`coordinate_frame` / `coordinate_summary`、`geometric_rules_ref`。字段语义与历史版本（曾用世界轴 `dimensions`、曾写冗长 `coordinate_frame`）见 **`geometric_rules_floor.md`** 与下文；当前 **`objects[].dimensions` 为根局部合并 AABB**。

**Blender 坐标系、矩阵含义、墙厚与地板挤出、资产原点习惯、在 Blender 中查看父子**等引擎侧内容见 Infinigen 仓库根 [**INFINIGEN_RULES.md**](../../INFINIGEN/INFINIGEN_RULES.md) **§七**。

---

## 简要运行日志（`preprocess_last.log`）

- **默认路径**：`IDL3D/SCENES2TEST/preprocess_last.log`（**追加**写入）。
- **每跑一次**追加**一块**（由 `#` 分隔线包围）：单 `--blend` 为一块；整批 `--scenes-root` 由**父进程只写一块**（`Scenes total` 为目录数），子进程带 `--no-log` 避免每条场景再写一块 `Scenes total: 1`。
  - **预处理批次 id**：本次运行随机十六进制串；
  - **预处理时间**（UTC ISO）｜**总用时**（秒）；
  - **预处理场景数量**｜**成功**｜**失败**｜**跳过**（已存在 `GT.json` 未再处理）｜；
  - **发现资产有重叠的场景**：子项 `场景名（pair_count=…）`，无则 `无`；
  - **发现有其他异常的场景**：失败场景及原因（如 `exit=1`、blend 路径），无则 `无`。
- **参数**：`--log-file PATH`；`--no-log` 不写文件；`--force`（或别名 `--non-skip`）在已有 `GT.json` 时仍预处理。
- **退出码**：任一子场景预处理失败时进程 **非 0**（仅跳过的不算失败）。

---

## 快速判断：场景里有没有「可报告的」家具重叠？

- **`furniture_overlap.computed === true`**：本次预处理**确实跑了**体素重叠估计（未使用 `--no-overlap`）。若为 `false`，表示跳过或未写该项，**不能**用 `pair_count` 判断场景。
- **有没有明显重叠**：看 **`pair_count`**（或 **`overlap_volume_m3_total`**）。  
  - **`pair_count > 0`**：至少有一对家具达到 `min_reported_volume_m3` 以上的相交体积，详情在 **`pairs`**。  
  - **`pair_count === 0`**：在体素分辨率与最小体积阈值下，**没有**任何一对被记为穿模（可能是真无重叠，或重叠太小被阈值滤掉）。

因此：「快速扫一眼」时，在 **`computed === true`** 的前提下，用 **`pair_count`**（或总音量）判断是否报告了重叠；**仅 `computed` 不能说明有/无重叠**。

---

## `info.json` 顶层

首字段为 **`scene_id`**（与 `GT.json` 相同）。随后为 `categories_in_view`、`asset_count_in_view`（视锥内资产根数量，与 GT 同源排除建筑）、**`objects`**（视锥内资产对应 `GT.json` 条目的拷贝）、可选 **`furniture_overlap`**，再为 `intrinsic`、`extrinsic`、`blender`。**不**写顶层 **`image`** 块（与旧 `extract_camview` 载荷不同）。**不**写入 `schema_version`、`coordinate_frame`、`distortion`、`camera_setup`（全数据一致，见 **`IDL3D/docs/geometric_rules_floor.md`**）。外参为 **world → OpenCV 相机** 的 `world_to_camera_rt`（3×4），与 `infinigen.core.util.camera` 一致。

---

## `info.json` 中的 `furniture_overlap`

由 `extract_info` 合并进 `info.json`；重叠数据来自 `preprocess_lib/overlap_check.compute_furniture_overlap`，含义如下。

| 字段 | 含义 |
|------|------|
| **`computed`** | 是否执行了重叠计算（与 `--no-overlap` 对应）。 |
| **`method`** | 当前为 `voxel_grid_world_aligned`：世界坐标轴对齐体素网格上估计相交体积。 |
| **`voxel_resolution`** | 体素为均匀立方体、**每对家具自适应**步长；单对体素数有上限（见实现内常量），避免极慢。 |
| **`min_reported_volume_m3`** | 小于该体积（默认 `1e-6` m³）的相交**不写入** `pairs`，用于抑制数值噪声。 |
| **`asset_count`** | 参与检测的**顶层家具**数量（与 `GT.json` 的 `unique_assets` 收集规则一致）。 |
| **`pair_count`** | 达到报告阈值的**家具对**数量。 |
| **`overlap_volume_m3_total`** | 上述各对 `overlap_volume_m3` 之和（估计值）。 |
| **`pairs`** | 列表项含 `a` / `b`（物体 `id`）、`category_a` / `category_b`、`overlap_volume_m3`、`voxel_count` 等。 |
| **`skipped_category_pairs`** | 算法中**不参与穿模判定**的类别对说明（如床 + 床垫视为一体，不计入穿模）。 |

说明：主流程为 **世界系 AABB 两两相交**（粗筛；与 `GT.json` 的**根局部** `dimensions` 定义不同）→ 仅候选对做 **Blender Boolean（INTERSECT，EXACT）** 求交体积；体积低于 `min_reported_volume_m3` **不报告**（桌椅 AABB 叠在一起但 mesh 不交时布尔体积≈0）。布尔失败时再 **体素 + 顶点–表面距离** 回退。类别在 `excluded_categories` 中的物体（如 `wall_art`、`mirror`）不参与两两检测。`pairs[].detection`：`mesh_boolean` | `voxel_grid` | `vertex_surface_proximity`（后两者为回退；proximity 的体积为占位值）。整体适合**筛查与排序**。

---

# `GT.json` 几何提取逻辑

## 字段一览（语义速查，与 Blender 对象对齐）

| 字段 | 含义 |
|------|------|
| `scene_id` | 场景目录名（如 `kitchen_medium_s1759680675`），与 `info.json` 中同名字段一致。 |
| `scene.room.size` | `[W, D, H]`（米）：`unique_assets:room_floor` 内地板网格合并世界 AABB 的 XY 跨度 + 由 `.ceiling` 推算的高度。 |
| `scene.room.type` | 场景目录名按下划线分段后的**首段**（如 `bedroom_small_…` → `bedroom`）。 |
| `scene.room.asset_count` | `objects` 数组长度。 |
| `objects[].id` | 根物体名（如 `…Factory(…).spawn_asset(…)`）。 |
| `objects[].position` | 根物体 **`matrix_world.to_translation()`**（米），即**物体原点**的世界坐标，**不是**几何中心或贴地点。（矩阵含义见 INFINIGEN_RULES §7.2） |
| `objects[].dimensions` | 根物体下**所有 MESH 子树**在**资产根局部坐标系**中合并的轴对齐包围盒边长 `[dx, dy, dz]`（米），沿**根局部 X/Y/Z**。与绕 Z 斜放时世界轴外接盒不同；约定见 **`IDL3D/docs/geometric_rules_floor.md`**。 |
| `objects[].rotation_yaw` | 语义前向相对 +Y 的整数度（`extract_gt.semantic_yaw_degrees`），**不是**简单的 `matrix_world` 欧拉 Z。无 Front 时默认用根局部 **+X** 回退；**`blackboard`** 用根局部 **+Y**（仅 GT）。**`side_table`** 在水平近似正方形时固定为 `0`。**灯具**（`lamp` / `*_lamp`）与类别名中含 **`_plant_`** 的（如 `large_plant_container`）为 **`null`**（无比对意义）。 |

下文 §1 起为**实现细节**（与上表一致）。

---

## 1. 写入哪些物体？

- 集合 **`unique_assets`** 中、**父级为 `None` 的顶层物体**。
- 排除位于下列 **collection** 内的一切物体名（墙、地、顶、外墙、楼梯、门窗、门套、踢脚线等），以及名称以 `door_base_elements` 为前缀的 collection。定义见 `preprocess_lib/extract_camview.py` 顶部的 `ASSET_GT_EXCLUDE_COLLECTIONS` 等。
- 每个物体一条记录；**`id` = 该根物体在 Blender 中的唯一名称**（如 `OfficeChairFactory(…).spawn_asset(…)`）。

---

## 2. `scene.room`

| 字段 | 计算方式 |
|------|----------|
| `type` | 场景目录名按 `_` 分割后的**首段**小写（如 `bedroom_small_s123` → `bedroom`）。 |
| `size` | `[W, D, H]`（米）。优先用 `unique_assets:room_floor` 内所有 `*.floor` **MESH** 的**合并世界 AABB**：`W/D` 为 XY 方向跨度，`H` 由同房间 `.ceiling` 与地板顶面 Z 通过 `room_height` 推算（见 `preprocess_lib/extract_gt.resolve_room_metrics_box`）。若不可用则回退射线或最大地板网格。 |
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
2. 对各 MESH 的 **evaluated** 顶点先变到**世界坐标**，再左乘 **`inv(root.matrix_world)`** 得到**根物体局部坐标**，在根局部 XYZ 上合并 **min/max**（`preprocess_lib/extract_camview.combined_aabb_root_local`）。
3. **`dimensions = [hi.x - lo.x, hi.y - lo.y, hi.z - lo.z]`** 为沿**根局部 X/Y/Z** 的跨度；绕竖直轴斜放时不再用世界轴外接盒膨胀。

**复合物体（如床 = 床架根 + 床垫子 MESH）**：并集仍在**根局部**下计算，与侧边栏「仅父物体网格」仍可能不一致，但轴向与根的 Transform 一致。

若无任何 MESH（极少见）：用根物体 **evaluated** 的 **`bound_box`** 在**物体局部**上的跨度。

### `rotation_yaw`（整数 0–359，或 JSON `null`）

- 由语义前向与 `semantic_yaw_degrees` 等逻辑得到（见 `extract_gt.py`）；非简单欧拉 Z 单一来源。
- **`null`**：`lamp` / `*_lamp`，或 `category` 中含 `_plant_` 片段（如 `large_plant_container`）；评测应跳过 yaw。

### `category`

- 由根物体名解析：`preprocess_lib/extract_camview.factory_to_category`（如 `OfficeChairFactory…` → `office_chair`）。

---

## 4. 与 Blender 面板的常见误解

- **`dimensions`**：GT 为 **根 + 全部后代 MESH 在根局部系下的合并 AABB 边长**；侧边栏对父级常**不含子网格**，二者不可直接等同。嵌套子物体仍会计入 GT，一般不必再手工叠加子物体尺寸。根节点可能是 Empty 下挂 Mesh，也可能根即 MESH；以具体 `.blend` 为准（概览见 INFINIGEN_RULES §7.4）。
- **`position.z`**：`position` 是**物体原点**高度，不是脚底、不是包围盒底面；不同资产原点位置差异很大（见 INFINIGEN_RULES §7.3）。室内可走平面大致在 **z ≈ 0.10～0.11 m** 量级（与生成侧墙厚/挤出相关，见 INFINIGEN_RULES §7.6）。多数「落地」资产接近地板顶面高度，但因原点定义不同，**不必**等于地板 Z；床等原点常在床体中部附近，**会更高**。
- **若评测需要贴地高度**：应对网格取世界系 **`combined_aabb` 的 `lo.z`（世界包围盒底面）**，而不是只用 `position.z`。`GT.json` 的 `dimensions` 为根局部，**不**直接给出世界底面 Z。

---

## 5. 单位与精度

- 场景单位为**米**。
- 位置与 `dimensions` 分量一般 **四舍五入到 0.01 m**（两位小数）；`rotation_yaw` 为**整度**或 **`null`**（见上）。
- 存在 **Modifier** 时，顶点级 AABB 与仅看 `bound_box` 可能略有差异；预处理对网格使用 **evaluated mesh** 算 AABB（见 `preprocess_lib/extract_camview.py`；算法见上文 §3）。

---

## 6. 废弃说明

- 旧版文档中的 **GLB 锚点烘焙、`scene_GT.json` 底面中心** 等已废弃；评测以当前 **`GT.json` + 本文** 为准。
