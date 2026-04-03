# `generation/generate_lib/` — 合成 / 渲染后端

本目录**仅**服务 [`../generate.py`](../generate.py) 一条线（如 `core.py` 等）。一次性修补旧 RAW 的脚本在上一级 [`../fix_existing_scene.py`](../fix_existing_scene.py)，非管线核心。  
**预处理**是并列的另一条线：入口 [`../preprocess.py`](../preprocess.py），库 [`../preprocess_lib/`](../preprocess_lib/)，**不在本目录内**。  
路径常量见 [`../../configs/infinigen_paths.py`](../../configs/infinigen_paths.py)；Infinigen 克隆在 **`../../infinigen-idl3d/`**。

**单房间生成**：使用 **`../generate.py`**（仅矩形 box；不规则管线已弃用，见 `../../docs/LIMITATION.md`）。**完整公寓**：`generate_all.py`（若存在于工作区）。

---

## 1. 场景生成脚本

单房间生成推荐使用**统一入口** `../generate.py`；完整公寓使用 `generate_all.py`。默认单房/试跑输出在 **同级 `INFINIGEN/SCENES_SKETCH/`**（临时生成、调参、改场景）；**正式未预处理数据集**用 `../generate.py --scenes-raw` 写入 **`INFINIGEN/SCENES_RAW/`**，并在其 `manifests/` 下维护 **`generation_batches.jsonl`** 与各房型 **`*_scenes.csv`**（路径定义见 [`../../configs/infinigen_paths.py`](../../configs/infinigen_paths.py)）。

### generate.py — 统一生成与渲染入口（推荐）

```
用途：   调用 core.py 生成矩形单房间，统一参数与默认值
适用：   日常生成单房间场景；无需直接调用 core.py
```

**调用关系：**
```
../generate.py → core.py → infinigen_examples.single
```

**统一参数（常用）：**
- `--room-type`：共 **10** 种 canonical（`configs/constant.py` 的 `ROOM_TYPES`：如 `bedroom`、`homestudio`、`livingroom`、`diningroom`、`office`、`meetingroom` 等）；对内映射到 Infinigen 串，不改 Infinigen 仓库
- `--render`：`1280`（默认，1280×720）或 `640`（640×360）
- `--num-scenes`：生成数量，默认 1
- `--room-size`：small / medium / large / xlarge，默认 medium
- `--room-size-mix`：如 `small:3,medium:3,large:3`（单房型批量）
- `--render-samples` / `--sample`：渲染采样数，默认 128（如 `--sample 256`）
- `--denoise` / `--no-denoise`：默认开启去噪
- `--light-multiplier`：灯光倍率，默认 5.0
- `--gpu`：默认由 get_render_gpu() 指定

**推荐运行方式（先 cd 到脚本目录）：**
```bash
cd /scratch2/f0080vw/IDL3D/generation

python generate.py
python generate.py --room-type kitchen --num-scenes 5
python generate.py --room-type library --room-size-mix small:3,medium:3,large:3
```

**输出目录（新批次）：** `SCENES_SKETCH/{canonical}_s{seed}/`（如 `livingroom_s42`）；旧目录（含历史 `livingroom_s*` 无下划线旧批次等）并存、无自动迁移。

---

### core.py — 矩形单房间（generate.py 的后端）

```
用途：   生成单个矩形房间（可控尺寸）+ 家具
适用房型：住宅 6 类 + working_space 4 类（与 `generate.py` 一致，共 10 种）
房间形状：由 shapely.box() 生成，始终是矩形，中心在原点
```

**调用链：**
```
core.py
  └─ infinigen_examples.single           (Blender 子进程)
       ├─ configs:  single_room.gin      (指定 PredefinedFloorPlanSolver)
       └─ constraints: single_room.py    (single_room_floor_plan → shapely.box)
```

**关键参数：** 与 generate.py 一致；另有 `--render-samples`、`--denoise`/`--no-denoise`、`--gpu`（默认 2）。

**输出目录：** `SCENES_SKETCH/{canonical}_s{seed}/`

---

### generate_all.py — 完整公寓多房间

```
用途：   生成完整公寓户型 + 多房间家具
适用场景：需要完整户型布局、多相机多房间渲染
房间形状：公寓分割算法产生，所有房间均可见
```

**调用链：**
```
generate_all.py
  └─ infinigen_examples.generate_indoors       (Blender 子进程)
       ├─ configs:  fast_solve.gin
       ├─ coarse 阶段 → scene.blend
       └─ render 阶段 → Image_*.png + camview_*.npz（按相机编号逐个渲染）
```

**关键参数：**
- `--furniture-mode`：none / camera_room / max_rooms / all_rooms
- `--detail`：full / no_small / layout_only
- `--room-type`：仅 camera_room 模式下生效
- `--overhead`：俯视相机（仅 none 模式下生效）

**输出目录：** `SCENES_SKETCH/{scene_name}/`（含 coarse/ 和 frames/ 子目录）

---

## 2. 预处理（独立管线，不在本目录）

入口：**[`../preprocess.py`](../preprocess.py)**，库：**[`../preprocess_lib/`](../preprocess_lib/)**。设计与字段见 [`../../docs/PREPROCESS_DESIGN.md`](../../docs/PREPROCESS_DESIGN.md)。

同级的 **`../promote.py`**、**`../backfill_preprocess_batches_metrics.py`** 也属于预处理/数据集流转，与 `generate_lib/` 无包含关系。

---

## 3. 数据加载 & 参考文档

| 文件 | 说明 |
|------|------|
| `GT_loader.py` | 在 Blender 脚本环境中加载 scene_GT.json，提取相机参数、房间尺寸等 |
| `single_scene_schema.json` | `GT.json` 的字段说明（示例 schema） |
| `../docs/LIMITATION.md` | 数据集局限（仅 box、单相机、无畸变、发布文件集等） |
| `../docs/PREPROCESS_DESIGN.md` | 预处理设计：`GT.json` / `info.json` / `furniture_overlap`、字段语义与几何提取（引擎侧见 Infinigen 仓库根 `INFINIGEN_RULES.md` §七） |
| `GEOMETRIC RULES.md` | 通用几何规则文档 |
| `ASSETS.csv` | 资产清单 |

---

## 4. Infinigen 底层关键文件（供参考）

生成脚本最终调用的 Infinigen 内部模块：

```
infinigen-idl3d/infinigen_examples/
├── single.py                          core.py 的后端
├── generate_indoors.py                generate_all.py 的后端（公寓多房间）
├── configs_indoor/
│   ├── single_room.gin                矩形单房间配置（PredefinedFloorPlanSolver）
│   ├── singleroom.gin                 官方单房间配置（solve_max_rooms=1）
│   ├── fast_solve.gin                 快速求解（减少迭代）
│   ├── overhead.gin                   俯视相机 + 隐藏其他房间
│   └── base_indoors.gin               所有室内场景的基础配置
├── constraints/
│   ├── single_room.py                 矩形单房间的 floor plan + 家具约束
│   ├── home.py                        多房间公寓的家具约束
│   └── semantics.py                   资产类型 → 用途映射
└── util/
    └── generate_indoors_util.py       hide_other_rooms()、restrict_solving() 等工具函数
```

---

## 5. 典型工作流

```
┌──────────────────────────┐     ┌──────────────────┐
│  ../generate.py          │     │  generate_all.py │
│  → generate_lib/…        │     │  (完整公寓)       │
└────────────┬─────────────┘     └────────┬─────────┘
             │                            │
             └──────────────┬─────────────┘
                            ▼
                         SCENES_SKETCH/{scene_name}/
                         ├── scene.blend
                         └── Image_*.png
                                    │
                                    ▼
                    ../preprocess.py（独立，与 generate_lib 并列）
                                    │
                                    ▼
                         同上目录追加 / 清理：
                         ├── GT.json
                         ├── info.json
                         └── （删除 *.npz）
```
