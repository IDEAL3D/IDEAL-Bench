# 全局室内场景设计

本文档约定**跨所有空间类型**的架构、**坐标系与房间原点**、面积定义、共享约束、灯光、相机、求解参数与材质调整。  
**具体房型的尺寸档位、家具数量与布局细节**见对应分册：

| 文档 | 约束模块 / 内容 |
|------|------------------|
| [LIVING_SPACE_RESIDENTIAL_DESIGN.md](./LIVING_SPACE_RESIDENTIAL_DESIGN.md) | `living_space.py`：住宅六类（bedroom、homestudio、living-room、kitchen、bathroom、dining-room） |
| [WORKING_SPACE_DESIGN.md](./WORKING_SPACE_DESIGN.md) | `working_space.py`：open-office、meeting-room、classroom、library |

**说明（generation 入口范围）**：`generate.py` / `generate_lib.core` **仅支持 10 种** `room-type`（上表所列），**只使用** `living_space` 与 `working_space` 两套约束模块；**不包含**商业空间等其它房型入口。

---

## 1. 代码与配置架构

```
constraints/
├── living_space.py       # 住宅 + 共享约束 / 顶灯（solver）
├── working_space.py      # 办公与教学
├── semantics.py
├── home.py
└── util.py

configs_indoor/
├── living_space.gin
├── working_space.gin
└── base_indoors.gin      # 相机、渲染、天空光等全局基底
```

每个 `*_space.py` 导出：`floor_plan`、`room_constraints`、`furniture_constraints`。  
`single.py` 通过 gin 的 `constraints_module` 加载对应模块。

---

## 2. 坐标系与房间原点（Box 场景）

- **Blender 世界原点永远是 (0,0,0)**。
- **本管线 box 场景**：地板平面在 XY 上的几何中心为 **(0,0)**；与俯视图「房间关于原点对称」一致。
- **Z**：可走面/地板顶面高度由墙体与地板挤出（solidify）等 mesh 决定，**不一定**恰好是 `z=0`；物体 `position.z` 仍以 Blender 世界坐标为准。若评估规范要求「地面 z=常数」，需用地板 AABB 或显式约定统一。

`GT.json` 中 `position`、贴地高度与 `dimensions` 的预处理约定见 [PREPROCESS_DESIGN.md](./PREPROCESS_DESIGN.md)。

---

## 3. 空间类型与约束模块映射

`generation/generate_lib/core.py` 中 `ROOM_CONFIG_MAP` 定义房间类型与约束文件的对应关系：

| 房间类型 `room-type` | 约束模块 |
|----------------------|----------|
| bedroom, homestudio, living-room, kitchen, bathroom, dining-room | `living_space` |
| open-office, meeting-room, classroom, library | `working_space` |

**程序化布局（大件不由 `furniture_constraints` 描述）**：`Semantics.Classroom`、`Semantics.OpenOffice` 在 `furniture_constraints` 中返回空 `cl.Problem`，由 `procedural_layout()` 在 `solve_rooms` 之后写入占位；顶灯为程序化 **AREA**（非 `living_space` 的 `CeilingLightFactory` + 顶灯 solver）。**三档家具求解步数仍见 §8**（与全房型一致）。  
**图书馆**书架网格为程序化，部分阅读家具等仍可走 solver（见 `WORKING_SPACE_DESIGN.md`）。

---

## 4. 面积 `room_area`（家具与顶灯分档）

用于 `furniture_constraints` 与 `_lighting_constraints` 的 **`room_area`**：

\[
\texttt{room\_area} = \frac{\texttt{min\_w}+\texttt{max\_w}}{2} \times \frac{\texttt{min\_d}+\texttt{max\_d}}{2}
\]

其中 `(min_w, max_w, min_d, max_d)` 通常来自 gin 的 **`size_override`**（由 `core.py` 按房型与档位写入）；未覆盖时使用各模块 `ROOM_SIZE_PRESETS` 中该房型的默认范围。  
`floor_plan` 内可能对宽高做随机与网格取整，个别场景会在阈值附近波动。

**各房型的 `(min_w, max_w, min_d, max_d)` 档位表**见分册（与源码 `ROOM_SIZE_TIERS` 保持一致）。

### 4.1 墙厚与净空高度（`core.py` → `RoomConstants.global_params`）

单房间经 `generation/generate_lib/core.py` 调用 `infinigen_examples.single` 时，除写入 **`size_override`**（平面 footprint）外，还会按**当前场景的档位名 `size_name`** 注入墙体参数（与约束模块是 `living_space` 还是 `working_space` **无关**，凡走单房间 `core` 即生效）：

| 注入项 | 源码常量 | 说明 |
|--------|-----------|------|
| **墙厚** | `FIXED_WALL_THICKNESS_M`（默认 **0.25 m**） | 非 `None` 时写入 `wall_thickness` |
| **净空高** | `WALL_HEIGHT_UNIFORM_BY_TIER` | 写入 `wall_height = ("uniform", lo, hi)`（单位 **m**） |

**`WALL_HEIGHT_UNIFORM_BY_TIER` 与档位名对应（与商业/办公大空间共用同一表）：**

| 档位名 `size_name` | `wall_height` 均匀采样区间 `[lo, hi]` (m) |
|--------------------|------------------------------------------|
| **small** | **2.8–3.2** |
| **medium** | **3.0–4.0** |
| **large** / **xlarge** / **studio** | **4.0–6.0**（高层高、大空间观感；**homestudio** 固定走 **studio** 档） |

**住宅（`living_space`）各 `room-type` 对应的档位名**（`resolve_living_space_tier`，与输出目录名无关）：

- **bedroom**、**living-room**：按 seed **50% `small` / 50% `medium`** → 净空约 **2.8–3.2** 或 **3.0–4.0 m**。
- **homestudio**：固定 **`studio`** → 净空 **4.0–6.0 m**（刻意偏高、偏「studio / 大平层」）。
- **kitchen**、**bathroom**、**dining-room**：固定 **`medium`** → 净空 **3.0–4.0 m**。

非住宅房型由 `--room-size` / `--room-size-mix` 等决定 `size_name`，再查上表；**未出现在表中的档位**不注入 `wall_height`，沿用 gin 默认（源码注释中为约 **2.8–3.2 m** 量级）。

---

## 5. 共享约束（solver 路径，`living_space` 合并项）

对 **合并了 `_shared_constraints` / `_lighting_constraints`** 的房间（非教室 / 非 open-office 程序化路径等），下列规则来自 `living_space.py`。**洗手间**不适用项已注明。

| 项目 | 规则 |
|------|------|
| **落地储物（靠墙，`Semantics.Storage`）** | **非浴室**：每房间 **1–7**；**浴室**：不参与此条（见住宅分册洗手间节） |
| **Storage 包含的工厂** | `SimpleBookcaseFactory`、`CellShelfFactory`、`LargeShelfFactory`、`SingleCabinetFactory`（不含 `KitchenCabinetFactory`） |
| **门洞净空 `portal_clearance`** | 任意家具与任意门：表面距离 **> 0.3 m** |
| **门前可达性** | `portal_accessibility` 软目标（weight=10） |
| **墙面装饰** | 挂画 **0–3**；装饰镜 **0–1**；距地、距洞口等见代码 |
| **地毯** | 单房间最多 **1** 张（`rugs_max_per_room`）；多块时最小间距 **≥ 1 m** |
| **植物** | 大盆栽 **0–1**；储物顶面小盆栽 **0–5** |
| **转椅（全局）** | 每房间 **0–1**；且限定 `front_against` 桌面朝向 |
| **每张桌最多 1 椅** | `desks.all(lambda d: deskchair.related_to(d).count().in_range(0, 1))` |
| **书桌显示器** | 每张书桌 **0–1** |
| **床头柜与台灯** | 床头柜上台灯 **0–2**；与储物/书桌协同见代码 |
| **家具间距惩罚** | `furniture_spacing`：`hinge(0.08, 0.4).minimize(weight=50)` |

### 5.1 顶灯数量（`_lighting_constraints`，按 `room_area`）

| `room_area` | 吸顶灯数量 |
|-------------|------------|
| **< 10 m²** | **2–4** |
| **< 25 m²** | **3–6** |
| **≥ 25 m²** | **4–8** |

（另含台灯在储物/书桌上的区间，见源码 `constraints["lamps"]`。）

---

## 6. 灯光与曝光（渲染）

### 6.1 程序化 AREA 顶光（与 `CeilingLightFactory` 求解器顶灯相对）

**本管线（`living_space` + `working_space`，不含已废弃的商业入口）** 中，凡**不**要求求解器摆 **mesh 吸顶灯**（`mesh_ceiling=False`）的房型，顶光均为 **Blender `AREA`**，天花下约 **0.15 m**，基础 **energy=9**（再经 `--light-multiplier` 倍率，见 §6.2）。

| 模块 | 程序化 AREA 顶光 | 备注 |
|------|------------------|------|
| **`living_space`**（六类住宅） | `procedural_layout` → `_place_living_area_ceiling_lights` | **energy=9**；`return False` 保证家具求解仍跑 |
| **`working_space`** · 教室 / 开放办公 | `_place_classroom_grid` / `_place_open_office_grid` 内嵌 | **energy=9**；数量随面积 **2 / 4 / 6** 盏等 |
| **`working_space`** · **会议室** | 同 living 的 `_place_living_area_ceiling_lights`（`meeting_ceiling_*` 命名） | **energy=9**；仍属 **`working_space.py`**，语义勿与住宅混淆 |
| **`working_space`** · **图书馆** | `_place_library_grid` 内 AREA | **energy 随面积上调**（大空间偏亮），**非**固定 9 |

§5.1 表格描述的是 **`mesh_ceiling=True`** 时约束图里对 **CeilingLightFactory** **数量**的区间；**住宅与上述 AREA 房型**不在约束图里求 mesh 顶灯。

### 6.2 灯光功率 CLI 倍率

三脚本统一默认 **`light_energy_multiplier = 5.0`**，可由 `--light-multiplier` 覆盖。

| 值 | 效果 | 适用 |
|----|------|------|
| 1.0 | 较暗 | 夜晚 / 仅靠窗光 |
| 3.0 | 偏暗柔和 | 傍晚 / 暖光 |
| 5.0 | 与原版 demo 接近 | 日常室内（默认） |
| 8.0 | 明亮 | 白天充足光照 |
| 10.0 | 很亮 | 商业样板间 / 过曝风格 |

### 6.3 渲染曝光与天空光（`base_indoors.gin`）

- **`configure_render_cycles.exposure = 3`** → film_exposure 约 **2³ = 8** 倍亮度。  
- **降噪**：`base_indoors.gin` 中默认为 `configure_render_cycles.denoise = False`；**推荐入口** `generation/generate_lib/core.py` 在拼 `single` 的 `--overrides` 时显式写入 **`configure_render_cycles.denoise=True`**，并设置 **`configure_render_cycles.num_samples`**（与 CLI 一致）。因此以 `generate.py` / `core.py` 跑出的数据默认**开启** Cycles 降噪，与仅读 gin 文件时的默认值不同。  
- **`adaptive_threshold = 0.005`** 仍来自 gin；每像素采样数由上述 **`num_samples`** 控制：`generate.py` 对应参数为 **`--sample`**（默认 **128**；内部字段名 `render_samples`）。  
- **输出分辨率**：`base.gin` 默认为 **1280×720**（`execute_tasks.generate_resolution` 与 `get_sensor_coords`）。`generate.py` 可用 **`--render 640`** 覆盖为 **640×360**（仍写入同一 gin 键）。  
- **天空光**：`nishita_lighting.strength = 0.25`；`nishita_lighting.sun_elevation = ("clip_gaussian", 40, 25, 6, 70)`。

---

## 7. 相机

单房间（`core.py`）与完整公寓（`generate_all.py`）共用下列逻辑；参数由 `base_indoors.gin` 与 `core/placement/camera.py` 控制。（`generate_irregular` 已弃用，见 Infinigen 侧 `DEPRECATED/` 与 `LIMITATION.md`。）

### 7.1 位置

- **XY**：在房间地板 `SupportSurface` 上均匀撒点（GeoNodes，约 **500 / m²**），随机取一点作为水平位置。  
- **Z**：自采样点向下射线得离地高度，再抬升到目标高度：  
  `camera.camera_pose_proposal.altitude = ("clip_gaussian", 1.6, 0.2, 1.2, 2.0)`  
  即均值 **1.6 m**，截断在 **1.2–2.0 m**（成人站立视角）。

**筛选**：不嵌入墙/地形（BVH）；与 placeholder 最小距离；视野覆盖率约 **50%–100%**；最近场景几何不宜过近（`min_terrain_distance`，默认约 **0.7 m**）。

### 7.2 朝向

- **look_at**：地板 AABB 的 XY 中心，Z 取 **地板最低点 + 1.0 m**（约家具中部）。相机从 ~1.6 m 看向该点，视线略俯视（约 10°–20°）。  
- `direction = 相机 → look_at`，`rotation = direction.to_track_quat("Z", "Y")`，再对各轴加 **±5°** 随机噪声。  
- `pitch` / `yaw` / `roll` 的 gin 默认值仅在**无 look_at** 时生效；室内通常始终有 look_at。

### 7.3 焦距与传感器

- `focal_length = 15` mm（超广角，水平 FOV 很大）。  
- `adjust_camera_sensor`：`sensor_height = 18` mm；`sensor_width` 按分辨率宽高比。等效 35 mm 全画幅约 **20 mm**。

### 7.4 Camera Rig

`camera.spawn_camera_rigs.n_camera_rigs = 1`，单 rig 下单子相机 `camera_0_0`，无额外偏移。

### 7.5 景深

默认 **`use_dof = False`**；`dof_aperture_fstop = 2.8` 仅在 DOF 开启时生效。

### 7.6 视角选择流程（概要）

地板撒点 → 高度 1.2–2.0 m → 朝向房间中心（Z=1.0 m）+ 噪声 → 质量筛选 → 按 `score = std(dists) + 1.5×min(d)` 等选最优候选。

### 7.7 关键文件

| 文件 | 作用 |
|------|------|
| `infinigen_examples/configs_indoor/base_indoors.gin` | 相机与渲染基底 |
| `infinigen/core/placement/camera.py` | 采样、朝向、筛选 |
| `infinigen/core/placement/camera_trajectories.py` | 轨迹（动画） |
| `infinigen/core/placement/animation_policy.py` | `get_altitude()` |
| `infinigen/core/util/camera.py` | 内参 K、外参 RT |
| `infinigen/core/rendering/render.py` | DOF、渲染执行 |
| `infinigen/core/init.py` | `configure_render_cycles()` |

---

## 8. 约束求解参数（gin）与 `single.py` 实际阶段

Gin 中的 `compose_single_room.solve_steps_large` / `solve_steps_medium` / `solve_steps_small` 等仅在对应 **`solve_*` 阶段被调用**时生效。`infinigen_examples/single.py` 中的 `compose_single_room` 逻辑如下（与「是否跑程序化第一层」强相关）：

| 路径 | `procedural_layout` 返回值 | `solve_large`（墙面 + 地面大件 greedy，用 `solve_steps_large`） | `solve_medium` / `solve_small` |
|------|---------------------------|------------------------------------------------------------------|--------------------------------|
| **住宅六类 + homestudio** | `False`（仅注入 AREA 顶光） | **执行** | **`solve_medium` 执行**；`solve_small` 由 `solve_small_enabled` 决定（`core.py` 常注入 `False`） |
| **meeting-room** | `False`（仅注入 AREA 顶光） | **执行** | 同左 |
| **open-office** | `True`（工位网格） | **执行**（第二层：墙柜 / 盆栽 / 灯 / 扶手椅等） | **不执行**（`solve_medium` / `solve_small` 整段在 `if not procedural_applied` 内） |
| **classroom** / **library** | `True` | **不执行**（无第二层求解器家具） | **不执行**（同上） |

**`procedural_score_refinement`（程序化得分精炼 / SA）**：以源码 `infinigen_examples/single.py` 为准。当 **`procedural_layout` 返回 `True`** 且 gin 中 **`procedural_score_refinement_steps > 0`** 时，在 **`solve_large` 之前**执行该阶段（开放办公在 `collision_refinement.refresh_state_trimesh` 之后）；**开放办公的第二层家具仅在此之后**才由 `solve_large` 写入。`configs_indoor/working_space.gin` 默认 **`procedural_score_refinement_steps = 320`**（步数为 0 时整段跳过）。教室与图书馆在该 SA 阶段可动物体常为空，往往近似无位移。

因此：**并非**所有 10 种房型都会跑满三档 `solve_steps_*`。教室、图书馆、开放办公在 `procedural_layout` 之后**跳过** `solve_medium` 与 `solve_small`；教室与图书馆还**跳过** `solve_large`。开放办公是唯一「程序化后又跑 `solve_large`」的房型。细节见 [WORKING_SPACE_DESIGN.md](./WORKING_SPACE_DESIGN.md)。

gin 中与 IDEAL 生成侧常用覆盖一致的示例：

```gin
compose_single_room.solve_steps_large = 200
compose_single_room.solve_steps_medium = 150
compose_single_room.solve_steps_small = 50
compose_single_room.terrain_enabled = False
compose_single_room.solve_small_enabled = False
```

与 `core.py` 一致时常注入 **`solve_small_enabled=False`**（不解算桌面小物件）。**注意**：`base_indoors.gin` 里 **`compose_indoors.solve_steps_*`** 也可能存在默认值；以实际加载顺序与 `--overrides` 为准。

---

## 9. 材质与渲染友好性（全局）

适用于所有房间类型，便于下游推理：

| 调整项 | 说明 |
|--------|------|
| **移除家具透明材质** | 从 `furniture_leg`、`table_top`、`officechair_seat`、`shelf_board`、`curtain` 等移除 `GlassVolume` |
| **桌腿材质** | `dining_table.py`、`cocktail_table.py` 桌腿从 `tableware` 改为 `furniture_leg` |
| **降低反射率** | `marble.py`、`marble_voronoi.py`、`hammered_metal.py`、`metal_basic.py`、`appliance.py`、`grained_metal.py` 等 Roughness/Metallic 区间调整（见源码） |
| **保留玻璃** | 小物件（杯碗、花瓶、家电面板等）可保留 Glass/GlassVolume |

---

## 10. 可走地面高度（Z）

`BlueprintSolidifier` 将室内地坪顶面放在世界坐标约 **`z = wall_thickness/2`**（常见 **0.1–0.15 m**），而非 `z=0`。住宅由求解器 `on_floor` 自动贴合；**教室与 open-office 程序化布局**须通过 `_procedural_walkable_floor_z()` 从 `SupportSurface` 读取并传入 `_align_object_bottom_to_floor(..., floor_z=…)`，否则会「陷入地板」。详见 `WORKING_SPACE_DESIGN.md`。

---

## 11. 源码索引（全局）

| 内容 | 位置 |
|------|------|
| 房间类型与档位、`size_override`、墙厚/净空高 | `generation/generate_lib/core.py` → `ROOM_SIZE_TIERS`、`ROOM_CONFIG_MAP`、`WALL_HEIGHT_UNIFORM_BY_TIER`、`FIXED_WALL_THICKNESS_M` |
| 共享约束与顶灯 | `infinigen_examples/constraints/living_space.py` → `_shared_constraints`、`_lighting_constraints` |
| 语义标签 | `infinigen_examples/constraints/semantics.py` |
| 材质分配 | `infinigen/assets/composition/material_assignments.py` |

---

## 12. 仓库场景目录约定（`INFINIGEN/` 下）

| 目录 | 用途 |
|------|------|
| **`IDEAL/data/scenes_raw/`** | **`generate.py`** 与 **`generate_lib/core.py`** 的默认场景父目录（`SCENES_RAW_ROOT`；可用 `--scenes-root` 覆盖）。其下 `manifests/`：`generation_batches.jsonl`（批次元数据，含 `status`: `pending_preprocess` / `archived` / `deleted`）与 `{room_type}_scenes.csv`（列：`batch_scene_index`, `batch_id`, `scene_id`）；供预处理与下游数据集使用。 |
| **`SCENES_PROCESSED/`** | 正式预处理输出（`preprocess.py --final`：从 RAW 拷贝本批再跑预处理）；`manifests/` 见该目录下 `README.txt`。与上两者同级。 |
