# `working_space.py` 办公与教学空间

约束与管线：`infinigen/infinigen_examples/constraints/working_space.py`。  
**全局约定**（架构、共享约束、灯光、相机、可走地面 Z、**求解参数**）：见 [GLOBAL_DESIGN.md](./GLOBAL_DESIGN.md)。

---

# 设计原则

本节收录**架构约定、房型定义、尺寸档位、管线与资产表**等与设计相关的说明（「应当是什么样」）。

**约定**：与 `core.py` 一致时通常设 `solve_small_enabled=False`，不解算桌面小物件。

## 一、模块与 GIN

```
constraints/working_space.py
configs_indoor/working_space.gin
```

导出：`floor_plan`、`room_constraints`、`furniture_constraints`。  
`single.py` 通过 `constraints_module` 加载。

**两层约定（全 `working_space` 房型）**：**第一层**一律为 `procedural_layout` 网格/脚本；**第二层**（若存在）为 **约束 + 主求解器**（`solve_large` 等）。当前**仅开放办公**有第二层；教室、图书馆暂时只有第一层。

**地毯**：`working_space` 场景里**不出现地毯约束**——合并 `_shared_constraints` 后，**开放办公 / 会议室**对 `rugs` / `rugs_max_per_room` / `floor_covering` 等做 **pop**（不从 `used_as` 删 `RugFactory`，以免与住宅 `living_space` 的完整 `home_asset_usage()` 混用概念时产生误解）。教室/图书馆不合并共享家具图。详见 **「关键改动」**。

- **教室 / 图书馆**：`furniture_constraints` 返回**空** `cl.Problem`；不合并 `_shared_constraints` 的家具图；`single.py` **跳过** `solve_large`（与 `_shared_constraints` 无关）。顶灯为程序化 **AREA**。
- **开放办公**：第一层（桌椅）程序化；第二层合并 **`_shared_constraints` + `_open_office_constraints`**（靠墙 **`Semantics.Storage`**：与会议室相同，**`SimpleBookcaseFactory` / `CellShelfFactory` / `LargeShelfFactory` / `SingleCabinetFactory` 均在用法表**；另加大盆栽、落地灯、**`ArmChairFactory` 扶手椅**，数量见 **§3.1**）。`single.py` 调度顺序为：`procedural_layout` → `refresh_state_trimesh`（开放办公）→（若 **`procedural_score_refinement_steps > 0`**）**`procedural_score_refinement`** → **`solve_large`**（第二层）；顶灯仍为程序化 **AREA**（`_lighting_constraints(..., mesh_ceiling=False)`，不要求 mesh 吸顶灯）。

**Meeting-room**：约束与家具仍全部由 **`working_space`**（合并 `_shared_constraints` 等）描述；**顶光**与 `living_space` 一致：**`procedural_layout` 注入 Blender AREA（energy=9）后 `return False`**，不占用 `ProceduralLayoutFixed`，**`solve_large` 照常执行**（见 [GLOBAL_DESIGN.md §6.1](./GLOBAL_DESIGN.md)）。合并后再 **pop** 共享地毯项（与 open-office 相同）。**三档 `solve_steps_*`**：开放办公与会议室会跑 `solve_large`；教室/图书馆仍不因家具图执行该阶段（[GLOBAL_DESIGN.md §8](./GLOBAL_DESIGN.md)）。

### 1.1 `procedural_score_refinement`（SA 抛光）

**代码顺序（权威）**：`infinigen_examples/single.py` 在 **`procedural_layout` 返回 `True`** 且 **`procedural_score_refinement_steps > 0`** 时，于 **`solve_large` 之前**运行本阶段（开放办公在 `refresh_state_trimesh` 之后立刻进入；**第二层墙柜 / 盆栽 / 灯 / 扶手椅尚未添加**）。在 greedy 域 **`on_floor_freestanding`** 上剔除带 **`ProceduralLayoutFixed`** 的物体后，对剩余实例做一次**短平移/旋转 SA**，再以 `reclamp_after_procedural_refine` 等收回内缩可走区域，减轻与固定工位网格的穿插。`configs_indoor/working_space.gin` 默认 **`procedural_score_refinement_steps = 320`**（置 **0** 则跳过整段）。

| 层 | 语义标记 | 谁 | 典型来源 |
|----|-----------|-----|----------|
| **第一层** | `ProceduralLayoutFixed` | 教室/开放办公桌椅、讲台、图书馆 `library_block_*` | 仅程序化 |
| **第二层** | 无该标 | **开放办公**：仅由 **`solve_large`** 加入的墙柜、盆栽、落地灯、**扶手椅**（`ArmChairFactory`）等 | 主求解器 **`solve_large`**（**在 SA 之后**） |

**教室 / 图书馆**：无 `solve_large` 第二层；SA 阶段可动物体集合常为空，一般近似无位移，行为安全。

**实现要点**：若物体未经 `dof.try_apply_relation_constraints`，需 `ensure_procedural_refinement_dof`；深穿插时 refine 会临时 `relaxed_pose_validity_for_refinement`，再由 `reclamp_after_procedural_refine` 按 **generator 类型**与固定键名 `desk_*`/`chair_*` 等把物体收回内缩地面矩形（与 `collision_refinement.py` 一致）。

约束组织仍在 `working_space.py` + `collision_refinement.py`（方案 1，不与 living 抽全局共享模块）。

---

## 二、`core.py` 面积档位总览

下列 `(min_w, max_w, min_d, max_d)` 单位：**米**，与 `generation/generate_lib/core.py` → `ROOM_SIZE_TIERS` 一致。未列房型见该文件或 `_DEFAULT_SIZE_TIERS`。

### 2.1 开放办公室 `open-office`

| 档位 | `(min_w, max_w, min_d, max_d)` | 典型面积量级 |
|------|-------------------------------|----------------|
| **small** | 3.0–4.0 × 3.0–4.0 | ~9–16 m² |
| **medium** | 4.0–6.0 × 4.0–6.0 | ~16–36 m² |
| **large** | 6.0–8.0 × 6.0–8.0 | ~36–64 m² |

Gin 未覆盖时的总边界：`ROOM_SIZE_PRESETS[OpenOffice]` 常为 **(3.0, 8.0, 3.0, 8.0)**（与脚本 large 上限一致）。

### 2.2 会议室 `meeting-room`

| 档位 | `(min_w, max_w, min_d, max_d)` | 典型面积量级 |
|------|-------------------------------|----------------|
| **small** | 3.5–5.0 × 3.0–4.5 | ~10–22 m² |
| **medium** | 5.0–7.0 × 4.0–5.5 | ~20–38 m² |
| **large** | 7.0–10.0 × 5.5–7.0 | ~38–70 m² |

文档叙述用近似尺寸 **(4.0, 7.0, 3.5, 5.5) m** 时，与 **medium** 档中心区域一致。

### 2.3 教室 `classroom`

| 档位 | `(min_w, max_w, min_d, max_d)` | 说明 |
|------|-------------------------------|------|
| **small** | 6.0–6.5 × 8.0–8.5 | 约 3 组 × 6–7 行 |
| **medium** | 6.0–7.0 × 8.5–9.5 | 约 3 组 × 7–8 行（常见标准） |
| **large** | 6.5–7.5 × 9.0–10.0 | 约 3–4 组 × 7–8 行 |

### 2.4 图书馆 `library`

共 **三档**（`core.py` → `ROOM_SIZE_TIERS["library"]`）。未在命令行指定 `--room-size` 时，`core.py` 在每个场景 **small / medium / large 间各 1/3** 随机；`generate.py` 可对 library 使用 `--room-size auto` 达到同样效果。

| 档位 | 性质 | `(min_w, max_w, min_d, max_d)`（m） | 典型面积量级 | `library_block` 数（目标） | 单个 `library_block` 竖向格层数 |
|------|------|-------------------------------------|----------------|---------------------------|--------------------------------|
| **small** | 小型阅览室 | 7.5–10.0 × 6.0–9.5 | 约 **45–95 m²** | **6–10** | **4–5**（`shelf_cell_height` 层数） |
| **medium** | 普通图书馆 | 10.0–15.0 × 9.5–16.0 | 约 **95–240 m²** | **≥12**（抽样 **12–20**） | **5–8** |
| **large** | 大型图书馆 | 16.0–30.0 × 18.0–36.0 | 约 **288–1080 m²** | **≥16**（抽样 **16–28**） | **5–8** |

说明：

- **`library_block` 数**：`_place_library_grid` 按 **地面面积** 推断档位（阈值约 **100 / 280 m²**），再取上表目标并受「0.9 m 架间下最多能摆几块」上限裁剪；在满足 **架间 / 墙边 / 门侧** 约束前提下，对 `n_wide` **枚举择优**，使 **`n_cols×n_rows` 最大**（尽量多放双面块）。
- **竖向格层数**：`LibraryLargeShelfFactory` 通过 `library_shelf_layers` 调用 `_sample_library_shelf_params`，固定 **开架竖向分格数**（非房间网格的「排数」）。
- **偏暗**：程序化顶灯原 **盏数少、功率低** 时，大房间体积大、深色系木架吸光，易整体偏暗；**不是**主要靠书架挡住「缝在天花里的灯」。现已在 `_place_library_grid` 中 **按面积提高 AREA 盏数、能量与尺寸**；若仍暗，可再调 gin 曝光或材质。
- **超大空间一侧空地 >8 m**：常见原因包括门侧 **`BACK_CLEAR`** 过大（原可抽到 **8 m**）、以及 **`n_wide` 过大** 导致单块在进深向极宽、**`n_rows=1`** 仅一条书带。现已 **`BACK_CLEAR` 上限随进深比例收紧**，并以 **最大块数** 择优 `n_wide`。

`working_space.ROOM_SIZE_PRESETS[Library]` 为 gin 未覆盖时的总边界 **(7.5, 30, 6, 36)**。

---

## 三、空间分节

### 3.1 开放办公室 (open-office) — **第一层程序化 + 第二层求解器**

**Semantics**：`Semantics.OpenOffice`

**第一层（程序化）**：`SimpleDeskFactory` + `SwivelChairFactory` 网格，`ProceduralLayoutFixed`；`_place_open_office_grid` 仅写桌椅 + **AREA 顶灯**。

**工位套数（一桌一椅）**：按房间**实际**地板面积 `room_w×room_d`（m²）——**&lt;16**：**1–3**；**16–36**（含 16、不含 36）：**4–8**；**≥36**：**8–12**（该档内用 Beta 抽样略偏上端）；再与几何算出的网格容量 **`grid_cap`** 取 **`min`**（极小户型可能达不到区间下限并打 log warning）。

**第二层（约束 + `solve_large`）**：`_open_office_constraints` 规定 **`storage_freestanding`**（靠墙 **`Semantics.Storage`**，与 **`home_asset_usage`** 一致：**四类**书架/柜工厂均可出现）及 **`LargePlantContainerFactory`**、**`FloorLampFactory`**、**`ArmChairFactory`** 的数量区间（与面积档一致，见下表）；与 `living_space._shared_constraints` 合并时**已剔除地毯**（含门洞净空、家具非穿透等）。物体由 greedy/SA **添加**，`state.objs` 键名为求解器分配，**不再**固定 `office_shelf_*` 前缀。

**扶手椅（`ArmChairFactory`）**（仅开放办公；**不**改 `living_space`）：

- **语义**：走 `home_asset_usage()` 既有 **`Semantics.LoungeSeating`**；仅在 **`furniture_constraints(..., OpenOffice)`** 时把 **`ArmChairFactory`** 加入 **`Semantics.UniformInstance`**，同一场景内样式一致。
- **数量硬约束**（与工位网格 **第一层** 使用的面积分界一致：**&lt;16 / 16–36 / ≥36 m²**）：

| 面积档（`shared["room_area"]`） | 扶手椅件数（闭区间） |
|-------------------------------|----------------------|
| **&lt; 16 m²**（对应 `core` **small** 档边界均值） | **0**（必须 0） |
| **16 ≤ area &lt; 36 m²**（**medium**） | **0–1** |
| **≥ 36 m²**（**large**） | **0–2** |

- **`room_area` 含义**：与第二层书架 / 盆栽 / 落地灯相同，为 **`size_override` 四元组 `(min_w,max_w,min_d,max_d)` 的边界均值** 所算面积 \(\frac{w_\mathrm{min}+w_\mathrm{max}}{2}\times\frac{d_\mathrm{min}+d_\mathrm{max}}{2}\)，**不是** `floor_plan` 一次采样得到的真实 `w×d`。因此档位与 `core` 传入的 small/medium/large 边界一致。
- **软目标**：当上限 **&gt; 0** 时，`open_office_armchairs` 鼓励多摆扶手椅，并使其与**地面工位**（`SimpleDeskFactory`）保持一定间距，减轻堵在工位旁。

**管线**（与 `single.py` 一致）：

```
solve_rooms
→ procedural_layout（仅桌椅 + 顶灯）→ refresh trimesh
→ procedural_score_refinement（若 procedural_score_refinement_steps > 0）
→ solve_large（on_floor_and_wall + on_floor_freestanding）
→ （开放办公 / 教室 / 图书馆跳过 solve_medium / solve_small）
→ populate_assets → 门窗踢脚墙地顶 → …
```

**当前网格**：固定为 **rows** 模式（有序行列填充）；`DESK_GAP` / `ROW_GAP` / `FRONT_CLEAR` / `BACK_CLEAR` 等见 `_place_open_office_grid` 源码。

**顶灯**：与教室相同面积分档（2 / 4 / 6 盏 `AREA`，9 W，天花下 0.15 m）。

---

### 3.2 会议室 (meeting-room)

**Semantics**：`Semantics.MeetingRoom`  
**尺寸**：见 **§2.2** 档位表；叙述近似 **(4.0, 7.0, 3.5, 5.5) m** 对应 medium 附近。  
**管线**：**约束 + 求解器**（与 `living_space` 共享 `_shared_constraints` 等），**无** `procedural_layout` 网格。

**会议桌**：`tables.ConferenceTableFactory`（`infinigen/assets/objects/tables/conference_table.py`）×1，**仅**在 `working_space.furniture_constraints` 对 `MeetingRoom` 替换 `TableDiningFactory`，**不**写入全局 `home_asset_usage()`。

- **几何**：复用 `TableDiningFactory` 节点；**长宽比 1.6–2.5**；平面尺寸 **随 `size_override` 边界均值面积缩放**（`conference_table.py` → `_dimensions_from_footprint_m2`，与 `shared["room_area"]` 同源）。未注入 footprint 时回退为 **独立均匀采样**（原 **1.2–5 m** 每边逻辑）。
- **椅子**：`SwivelChairFactory`，与桌 `front_against`；数量按 **`shared["room_area"]`（档位边界均值，见 §3.1 open-office 脚注）** 分段：`area < 22` → **3–6**；`22 ≤ area < 45` → **5–9**；`area ≥ 45` → **7–12**。
- **靠墙储物 / 大盆栽**：原约束允许 **0 件**，求解器常全取 0，场景普遍偏空。现对 **area ≥ 22** 强制 **靠墙 `Semantics.Storage` 至少 1、至多 2**，**大盆栽恰 1**；小档（`area < 22`）仍为 **0–2 / 0–1**。
- **其它**：**不**再要求电视柜；**`TVStandFactory`** 仍留在 `used_as`（住宅 `living_space` 需要）。会议 **focus** score 为椅子朝向 **会议桌**；中/大档另有 **`meeting_decor_fill`** 鼓励摆满储物与盆栽（在硬约束下仍偏向 2 个柜）。

---

### 3.3 教室 (classroom) — 程序化网格

**Semantics**：`Semantics.Classroom`

#### 实现概要

- **`procedural_layout` → `_place_classroom_grid`**：课桌椅与讲台程序化网格摆放；`desk_*` / `chair_*` / `classroom_podium` 带 **`ProceduralLayoutFixed`**（黑板为 `WallDecoration`，不参与地面 SA）。
- **`furniture_constraints(classroom)`**：空 `cl.Problem`（大件不由该问题描述）。因 `procedural_layout` 返回 **`True`**，`infinigen_examples/single.py` **不运行** `solve_large`，且 **`solve_medium` / `solve_small` 整段被跳过**（`if not procedural_applied` 分支）；与 [GLOBAL_DESIGN.md §8](./GLOBAL_DESIGN.md) 中「程序化房型」一行一致。

#### 双模式（约 60% / 40%）

| 模式 | 学生桌椅 | 讲台 |
|------|----------|------|
| K–12 | `StudentDeskFactory` + `StudentChairFactory` | **`SimpleDeskFactory`**（老师桌） |
| 大学 | `SimpleDeskBaseFactory` + `SwivelChairFactory` | 高脚 **`SimpleDeskBaseFactory`** 讲台 |

#### 前区与黑板

- **黑板**：`BlackboardFactory`，高约 **0.9–1.2 m**，贴在 **maxy** 墙居中；底高约 **0.82–1.0 m**。可用墙面宽约 **`room_w − 0.35` m**；宽度在 **约 50%～100%** 该可用跨度内均匀随机，且绝对宽度不超过 **5 m**（窄教室仍不低于约 **1.8 m**）。
- **间距（与 `_place_classroom_grid` 中 `FRONT_CLEAR` 一致）**：`BB_WALL_INSET`（0.20 m，与墙边储物 `_WALL_STORAGE_STANDOFF` 同）+ **板厚** `BOARD_T`（0.035 m）+ **黑板前沿→讲台前沿** `gap_bb_podium` **U(1.0, 1.5) m** + **讲台平面进深** `PODIUM_PLAN_D`（K–12 **0.82**；大学 **0.56**）+ **讲台前沿→第一排课桌前沿** `gap_podium_first` **U(0.8, 1.5) m**。

#### 学生桌尺寸（K–12）

`StudentDeskFactory`：深约 **0.38–0.48 m**，宽约 **0.58–0.80 m**，高约 **0.56–0.68 m**。

#### 坐标与网格几何

- **平面**：`room_poly.bounds` → `room_w = maxx−minx`，`room_d = maxy−miny`。**前墙（黑板侧）为 maxy**，**第一排课桌行中心**从 `maxy` 向内：`y_start = maxy − FRONT_CLEAR − desk_d/2`，行向 **−y** 递增排数。
- **列数 / 行数**（初值见下表，可在循环中收紧过道）：
  - `row_pitch = desk_d + cy + CHAIR_GAP + ROW_GAP`
  - `usable_depth = room_d − FRONT_CLEAR − BACK_CLEAR`，`n_rows = max(1, ⌊usable_depth / row_pitch⌋)`
  - `col_pitch = desk_w + AISLE_W`，`usable_width = room_w − 2·SIDE_M`，`n_cols = max(1, ⌊(usable_width + AISLE_W) / col_pitch⌋)`（与源码 `int((usable_width + AISLE_W) / col_pitch)` 一致）
  - **总座数** `total_seats = n_cols × n_rows`

#### 距离与参数表（`_place_classroom_grid`）

| 符号 | 初值（m） | 含义 |
|------|-----------|------|
| `FRONT_CLEAR` | 合成 | **前区总进深**：从户型 **maxy** 边界到**第一排课桌行**之前占用的条带；`BB_WALL_INSET + BOARD_T + gap_bb_podium + PODIUM_PLAN_D + gap_podium_first`。 |
| `BB_WALL_INSET` | 0.20 | 前墙内表面相对边界的内缩（`_WALL_STORAGE_STANDOFF`）。 |
| `BOARD_T` | 0.035 | 黑板厚度占位。 |
| `BACK_CLEAR` | 1.10 | **后墙（miny 侧）留白**；迭代可降至 **≥0.48**。 |
| `AISLE_W` | 0.60 | **列间纵向过道**（沿 `x` 方向）；可降至 **≥0.26**。 |
| `SIDE_M` | 0.15 | **左右墙边距**；可降至 **≥0.06**。 |
| `CHAIR_GAP` | 0.10 | 椅与桌沿之间的空隙。 |
| `ROW_GAP` | 0.15 | **行与行**之间附加缝（不含桌+椅占位）。 |
| `MIN_SEATS` | 8 | 目标下限；循环收紧 `AISLE_W` / `BACK_CLEAR` / `SIDE_M` / `ROW_GAP` / `CHAIR_GAP` 直至 `total_seats ≥ 8` 或迭代用尽。 |

#### 网格与收紧

列距 **`desk_w + AISLE_W`**，行距 **`row_pitch`**（上式）；**上限**无固定封顶，由房间尺寸与上表共同决定。若仍 **&lt;8** 座则打 **warning**。椅子 **jx / jy / yaw** 随机 + OBB 碰撞；大学转椅 **`clearance=0.042`**。

#### `floor_plan`（教室）

- **两侧长窗**：`window_0` / `window_1` 在 **x=−w/2** 与 **x=+w/2**，沿 **y** 方向窗长 **span = d × U(0.6, 0.8)**；门在 **y=−d/2** 墙。

#### 灯光

程序化 `AREA` 网格（同 open-office）。

#### 相机（教室专用：放宽校验 + 可选评分与 top-K 随机）

`single.py` 室内相机在 **地板 `SupportSurface`** 上均匀随机取点，`look_at` 为 **房间地面 AABB 中心**；`compute_base_views` 对通过校验的候选用 **`keep_cam_pose_proposal` 标量打分**并排序。**内参 / 高度 / pitch** 由 gin（`base_indoors.gin` → `camera_pose_proposal`）决定。

**教室**（`room_type == "classroom"` → **`classroom_camera`**）：对 **`keep_cam_pose_proposal`** 传入更小的 **`min_terrain_distance`**、更宽的 **`terrain_coverage_range`**；并默认将 **`score_min_dist_weight` 设为低于全局默认 1.5**（削弱 `np.min(dists)` 相对 `np.std(dists)` 的权重，使排序更均匀）。在 **`configure_cameras`** 中，在已排序的 **`views` 前 K 名**（K 为 **3～6** 与 `len(views)` 的较小值）内**均匀随机**选一作为最终机位（其它房型仍取排序第一）。**`animate_trajectories`** 的校验与搜索共用同一 **`score_min_dist_weight`** 与放宽阈值；`classroom_random_from_top_k` 仅影响静态机位选取，不传入校验函数。

**其它房型可改参数**：

- **`keep_cam_pose_proposal`**（`camera.py`）：`min_terrain_distance`、`terrain_coverage_range`、`min_placeholder_dist`、`score_min_dist_weight`（默认 **1.5**）。
- **`camera_pose_proposal`**（gin / `base_indoors.gin`）：`altitude`、`pitch`、`focal_length`。
- **`compute_base_views`**（gin）：`min_candidates_ratio`、`max_tries`。

---

### 3.4 图书馆 (library)

**Semantics**：`Semantics.Library`  
**尺寸**：见 **§2.4**；与 `ROOM_SIZE_PRESETS[Library]` 一致。  
**核心**：程序化网格 **`_place_library_grid`** 摆 `LibraryLargeShelfFactory`；每个 **`library_block_*`** 带 **`ProceduralLayoutFixed`**。当前无第二层落地家具；若在 gin 中 **`procedural_score_refinement_steps > 0`**，`single.py` 仍会在**本不执行的 `solve_large` 调用点之前**跑 SA；图书馆通常可动物体为空、近似 no-op。阅读桌等若未来接入 solver，文档需单独说明是否打固定标。

#### 背靠背重叠问题的根因与修复（要点）

`LargeShelf` 在几何节点树末尾对整段网格绕 **Z 旋转 −90°**，局部 **进深沿 +X、面宽沿 +Y**，开架朝 **+X**。旧版背靠背位移写在局部 **+Y** 对应世界方向，导致进深方向仍重合。  
**修复**：背靠背位移沿进深方向 `u_depth = (cos rz, sin rz)`；并排沿面宽 `u_wide = (-sin rz, cos rz)`。详见 `double_face_shelf.py` 与 `_place_library_grid` 注释。

#### 过道与书柜—墙关系

| 类型 | 宽度（m） | 含义 |
|------|-----------|------|
| **架间面对面净距**（`stack_aisle`，`AISLE_W`） | **0.9–1.5** | 双面书架之间读者面对面通行 |
| **窄过道** | **0.9–1.2** 或 **0** | 书柜与侧墙/远墙；`SIDE_CLEAR` 约 12% 贴墙，否则 0.9–1.2；`FRONT_CLEAR` 为远墙侧 |
| **门侧宽过道** | **2–8** | **仅**靠门墙（约 `miny`）门前 **`BACK_CLEAR`**；不是书架块之间的间距 |

**目标 `library_block` 数与竖向格层数**：见 **§2.4** 表。

**程序化书架**：

- **面宽** 1–5 m、`LibraryLargeShelfFactory`。
- **双面块**：`spawn_double_face_block`：`n_wide` 组背靠背沿面宽（世界 Y）拼接，再 `join` 成 `library_block_*`。
- **朝向**：`rz=0`，开架朝世界 **+X**。
- **footprint**：约 **`depth_d × (n_wide×面宽)`**；块间为架间净距；墙边窄过道或贴墙；门侧宽过道。

---

## 四、资产速查（按房型）

下表为从实现效果上快速总结
| 物品类型         | classroom                                           | open office                | meeting room                | library       |
| ------------ | --------------------------------------------------- | -------------------------- | --------------------------- | ------------- |
| 桌子           | StudentDeskFactory/<br>SimpleDeskFactory：最小8张且受空间限制 | SimpleDeskFactory：1-12     | ConferenceTableFactory：只有1个 | 无             |
| 椅子           | StudentChairFactory/<br>SwivelChairFactory：数量跟随桌子   | SwivelChairFactory：数量跟随桌子  | SwivelChairFactory：3–12（按 area 分段，见 §3.2）     | 无             |
| 扶手椅         | 无                                                   | ArmChairFactory：第二层，0 / 0–1 / 0–2（见 §3.1） | 无 | 无 |
| 储物架          | 无                                                   | Semantics.Storage：按面积1-8   | Storage：**小档 0–2，中/大档 1–2**      | Library Units |
| 植物           | 无                                                   | LargePlantContainerFactory | 大盆栽：**小档 0–1，中/大档 1**  | 无             |
| 落地灯          | 无                                                   | FloorLampFactory：0-3       | 无                           | 无             |
| 特殊           | BlackboardFactory：只有1个                              |                            | 无                           | 无             |
| 挂画           | 无                                                   | WallArtFactory：0-3         | WallArtFactory：0-1          | 无             |
| 地毯/台灯/镜子/电视柜 | 无                                                   | 无                          | 无                           | 无             |



下表与 **`working_space.furniture_constraints` + 程序化第一层** 对齐；数量均为**闭区间**或「与代码一致」的表述。若与旧版笔记不一致，以本表为准。

| 物品类型 | classroom | open office | meeting room | library |
|----------|-----------|-------------|--------------|---------|
| **桌子** | **程序化**：`StudentDeskFactory`（K–12）或 **`SimpleDeskBaseFactory`**（大学）；**K–12 讲台**另用 **`SimpleDeskFactory`**。目标 **≥8** 套学生位，实际上限由房间与前区几何决定（见 **§3.3**）。 | **`SimpleDeskFactory`**：第一层网格，套数按地板面积 **&lt;16 → 1–3**；**16–36 → 4–8**；**≥36 → 8–12**，再与 **`grid_cap`** 取 `min`（不是笼统 1–12）。 | **`ConferenceTableFactory` ×1**（替换 `TableDining`）。 | **现行 `single.py`**：`library` 在程序化后 **不跑 `solve_large`**，**无**求解器阅读桌。代码里 **`_library_constraints`** 仍写有 **`TableDiningFactory` 1–1 / 1–2**（中大面积），**若将来恢复 library 的 `solve_large` 才生效**。 |
| **椅子** | **程序化**：`StudentChairFactory` / **`SwivelChairFactory`**（大学）与学生桌 **1:1**。`_classroom_constraints` 里教师椅等**在空 Problem 下不跑**。 | **`SwivelChairFactory`** 与工位 **1:1**（同上网格套数）。 | **`SwivelChairFactory`** 与会议桌 **`front_against`**：**area &lt;22 → 3–6**；**22–45 → 5–9**；**≥45 → 7–12**（`room_area` 为档位边界均值）。 | 同上：**当前管线无** `solve_large` → **`ChairFactory` 2–6** 等约束**未执行**（仅代码预留）。 |
| **扶手椅** | 无 | **`ArmChairFactory`**：**第二层** `solve_large`；件数按 **`room_area` 预设** **0 / 0–1 / 0–2**（**16 / 36 m²** 分界，与 §3.1 表一致）；**`UniformInstance`**。 | 无 | 无 |
| **储物架** | 程序化**无**靠墙储物 solver 条。 | **`Semantics.Storage`**（四类）：`_open_office_constraints` 按 **`room_area` 预设** **1–2 / 3–6 / 4–8**（**16 / 36 m²** 分界）。 | **`Semantics.Storage`**：**area &lt;22 → 0–2**；**≥22 → 1–2** 靠墙。 | **程序化**：`LibraryLargeShelfFactory` → **`library_block_*`**。**`_library_constraints` 里 `all_shelves` 件数**在**不跑 `solve_large`** 时同样**不生效**。 |
| **植物** | 程序化**无**大盆栽。 | **`LargePlantContainerFactory`**：0–2 / 0–6 / 0–6（随面积档）。 | **大盆栽**：**area &lt;22 → 0–1**；**≥22 → 1**（恰一棵）。 | **`_library_constraints` 写 0–2**；**当前不跑 solver → 实际不添加**。 |
| **落地灯** | 无 | **`FloorLampFactory`**：**0–1 / 0–2 / 0–3**（随面积档）。 | 无（未在 meeting 专用约束要求）。 | **`_library_constraints` 写 0–1**；**当前不跑 solver → 不添加**。 |
| **特殊** | **`BlackboardFactory` ×1**（程序化，`WallDecoration`）。 | — | — | — |
| **挂画** | **`furniture_constraints` 为空** 且 **不跑 `solve_large`** → **无**共享 `wall_decorations` 求解。 | **`WallArtFactory`**：`living_space._shared_constraints` / `home.py` 的 **`wall_decorations`**，**每间 0–3**。 | 同上 **0–3** / 间。 | **不跑 `solve_large`** → **与教室类似，当前无** solver 挂画。 |
| **地毯 / 台灯 / 镜子 / 电视柜** | **`used_as` 不删**。**open/meeting**：**pop** 地毯 / 床头柜 / 台灯等共享项；**无** `equals(0)` 硬约束。镜子仍受共享 **`wall_decorations`**（含镜 **0–1**）。教室/图书馆无 `solve_large`。 | 同左 | 同左 | 同左 |

**Factory 索引（补充）**

| Factory | 备注 |
|---------|------|
| `SimpleDeskFactory` | open-office 工位、教室 K–12 **讲台** |
| `SimpleDeskBaseFactory` | 教室 **大学** 桌椅与讲台 |
| `StudentDeskFactory` / `StudentChairFactory` | **教室 K–12** |
| `SwivelChairFactory` | open-office、会议室、教室大学 |
| `ArmChairFactory` | **仅 open-office 第二层**（休息用扶手椅；住宅 `living_space` 仍用全局 `LoungeSeating` 规则，与此独立） |
| `ConferenceTableFactory` | **仅 meeting-room**（替换 `TableDiningFactory`） |
| `BlackboardFactory` | **教室** |
| `SimpleBookcaseFactory` / `CellShelfFactory` / `LargeShelfFactory` / `SingleCabinetFactory` | **Storage**；open / meeting **完整四类**；**图书馆** solver 侧剔除 `SimpleBookcase` |
| `LibraryLargeShelfFactory` | **图书馆** 程序化块 |

---

## 五、floor_plan

- **教室**：双侧墙（**x=±w/2**）长条窗 + 单门（**y=−d/2**）；窗沿进深方向长度 **span = d × U(0.6, 0.8)**。
- **开放办公 / 会议室 / 图书馆**：窗长取 **该边墙长 × U(0.6, 0.8)**——**y=±d/2** 墙（窗沿 **x**）用 **w**；**x=±w/2** 墙（窗沿 **y**）用 **d**；在 **除门墙（y=−d/2）外的三面** 上 **随机 1～3 面** 开长条窗（`LineString`，再按 margin 对墙长 clamp）。
- **其它**（若存在未列房型回退）：默认单窗在 **y=+d/2** 墙。门均在 **y=−d/2**。

---

# 关键改动

本节收录**重大变更、与代码路径绑定的排错说明**（「为了跑通/提质改了什么、踩过哪些坑」）。与 **「设计原则」** 中的约定互参：原则描述**目标状态**，此处描述**实现与故障现象**。

## 办公场景：地毯 / 电视柜 / 床头柜 —— **pop**（无 `equals(0)` 硬约束）

- **`RugFactory`、 `TVStandFactory`** 等留在 `used_as` 中；**住宅 `living_space`** 走自己的 compose 入口，与 `working_space` 的 `initialize_from_dict` 无关。
- **开放办公 / 会议室**：合并 `_shared_constraints` 后 **pop** 地毯、床头柜、台灯等共享行；**不再**使用 `count().equals(0)` 禁止地毯 / 台灯 / 镜子 / 电视柜（避免 greedy coverage 与图复杂度；实际是否出现主要由剩余约束与 score 决定）。
- **镜子**：共享 **`wall_decorations`** 仍允许 **0–1** 装饰镜（与住宅同一上限）；若要不出现镜子，需以后再改墙面约束或用法表，而非当前硬清零。

## 开放办公：两层布局与碰撞

- **`single.py`**：`procedural_layout`（工位）之后对开放办公 **refresh trimesh**；若 **`procedural_score_refinement_steps > 0`**，接着跑 **`procedural_score_refinement`**（在 **`solve_large` 之前**）；再跑 **`solve_large`**，保证第二层墙柜 / 盆栽 / 落地灯 / **扶手椅**参与主求解。
- **`collision_refinement.py`**：`procedural_score_refinement` 对非 `ProceduralLayoutFixed` 的 `on_floor_freestanding` 物体做短 SA；`reclamp_after_procedural_refine` 按 **generator** 与键名规则收回内缩地面矩形（第二层墙柜等此时尚未加入）。办公场景通常无地毯约束（已 pop），refine 逻辑与地毯无关。

## 求解与资源占位

- **`configs_indoor/working_space.gin`**：`solve_steps_large` 可从默认值**下调**（例如 **420**，原为 750 量级），缩短单次开放办公主求解时间；可按质量/时间再调。
- **`populate.populate_state_placeholders`**：若占位物体名无法解析出 `inst_seed`（如 **Blender 名称被截断**），用 **哈希回退**生成确定性整数种子，避免 `TypeError: int() ... not 'NoneType'` 在 populate 阶段崩溃。

## `living_space._shared_constraints` 与地毯

- 若某自定义用法表**未**注册 `RugFactory`，`_shared_constraints` 会跳过地毯分支（`shared["rugs"]` 用空集），避免 `obj[RugFactory]` 报错。住宅默认用法表**包含**地毯，行为不变。
