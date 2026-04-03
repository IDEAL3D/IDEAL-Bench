# generation — 项目脚本总览

本目录是室内场景数据集制作的**入口层**，所有生成、预处理、加载操作均从此处发起。
底层调用本仓库内的 Infinigen 克隆（**`../infinigen-idl3d/`**，勿使用 `INFINIGEN/infinigen`）。

**单房间生成**：使用统一入口 `generate.py`（仅矩形 box 房间；不规则管线已弃用，见 `../docs/LIMITATION.md`）。**完整公寓**：使用 `generate_all.py`。

---

## 1. 场景生成脚本

单房间生成推荐使用**统一入口** `generate.py`；完整公寓使用 `generate_all.py`。默认单房/试跑输出在 **同级 `INFINIGEN/SCENES_SKETCH/`**（临时生成、调参、改场景）；**正式未预处理数据集**用 `generate.py --scenes-raw` 写入 **`INFINIGEN/SCENES_RAW/`**，并在其 `manifests/` 下维护 **`generation_batches.jsonl`** 与各房型 **`*_scenes.csv`**（路径定义见 `infinigen_paths.py`）。

### generate.py — 统一生成与渲染入口（推荐）

```
用途：   调用 generate_box.py 生成矩形单房间，统一参数与默认值
适用：   日常生成单房间场景；无需直接调用 generate_box.py
```

**调用关系：**
```
generate.py → generate_box.py → infinigen_examples.single
```

**统一参数（常用）：**
- `--room-type`：住宅 6 类 + 办公/教学 4 类，共 **10 种**（见脚本内 `ROOM_TYPES_UNIFIED`：bedroom、homestudio、living-room、kitchen、bathroom、dining-room、open-office、meeting-room、classroom、library）
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
python generate.py --room-type bedroom --room-size-mix small:3,medium:3,large:3
```

**输出目录：** `SCENES_SKETCH/{roomtype}_{size}_s{seed}/`（如 `livingroom_medium_s42`）。生成清理后保留 `scene.blend` 与 `Image_*.png`；相机标定与视锥语义由 `preprocess.py` 写出 `info.json`。

---

### generate_box.py — 矩形单房间（generate.py 的后端）

```
用途：   生成单个矩形房间（可控尺寸）+ 家具
适用房型：住宅 6 类 + working_space 4 类（与 `generate.py` 一致，共 10 种）
房间形状：由 shapely.box() 生成，始终是矩形，中心在原点
```

**调用链：**
```
generate_box.py
  └─ infinigen_examples.single           (Blender 子进程)
       ├─ configs:  single_room.gin      (指定 PredefinedFloorPlanSolver)
       └─ constraints: single_room.py    (single_room_floor_plan → shapely.box)
```

**关键参数：** 与 generate.py 一致；另有 `--render-samples`、`--denoise`/`--no-denoise`、`--gpu`（默认 2）。

**输出目录：** `SCENES_SKETCH/{roomtype}_{size}_s{seed}/`

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

## 2. 预处理脚本

### preprocess.py — Box 单房预处理

对 `scene.blend` 所在目录生成 `GT.json`（`unique_assets` 下单件资产）与 `info.json`（由 `extract_info` 组织；镜头内资产与相机块来自 `extract_camview`），并删除该目录下遗留的 `*.npz`。**默认 / 试跑**：不加 `--final` 时，在 `--scenes-root` 下就地预处理（默认 `../SCENES2TEST`），**不**写 `SCENES_PROCESSED/manifests/`（与 `generate.py` 默认写 `SCENES_SKETCH` 对应）。**正式**：`--final`（或 `--formal`）+ `--room-type`：仅在 `SCENES_RAW` **根目录平铺**的 `<房型>_s*` 下发现待预处理场景；`SCENES_RAW/<房型>/` 下已入库的场景**不参与**。要求每个目录有 `scene.blend` 与至少一个 `.png`（文件名不限，如 `Image_0_0_0001_0.png`）；预处理后仍会统一为 `image.png`。`*_scenes.csv` 与 `--raw-batch-id` 只做与**根目录已发现场景**的**交集**。从 `SCENES_RAW` 拷贝到 `SCENES_PROCESSED` 并更新 `manifests/`。

```
preprocess.py（薄入口）
  └─ preprocess_lib/
       ├─ cli / orchestrator / pipeline
       ├─ extract_gt.py       GT.json + ``resolve_room_metrics_box``（房间 W/D/H）
       ├─ extract_info.py     组装并写出 ``info.json``
       ├─ extract_camview.py  视锥内资产、相机块（原 ``utils`` / ``identify_room`` 已并入）
       └─ overlap_check.py    家具体素重叠（``furniture_overlap``）
```

**运行方式：**
```bash
cd /scratch2/f0080vw/IDL3D/generation

# 批量处理 ../SCENES2TEST/*/scene.blend
python preprocess.py

# 单个场景
python preprocess.py --blend /path/to/scene_folder/scene.blend

# 正式：从 RAW 清单拷贝并预处理（示例）
python preprocess.py --final --room-type bedroom
python preprocess.py --final --room-type bedroom --raw-batch-id bedroom_batch_001
```

### preprocess_lib/ 子目录

| 文件 | 功能 |
|------|------|
| `extract_info.py` | 合并各子模块结果，写出 `info.json` |
| `extract_camview.py` | 集合排除、视锥、镜头内资产与相机块；含原 `utils` / `identify_room` |
| `overlap_check.py` | 顶层家具两两重叠（`furniture_overlap`） |
| `extract_gt.py` | 房间尺寸推算 + `GT.json` |

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
├── single.py                          generate_box.py 的后端
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
│  generate.py             │     │  generate_all.py │
│  → generate_box.py       │     │  (完整公寓)       │
└────────────┬─────────────┘     └────────┬─────────┘
             │                            │
             └──────────────┬─────────────┘
                            ▼
                         SCENES_SKETCH/{scene_name}/
                         ├── scene.blend
                         └── Image_*.png
                                    │
                                    ▼
                         preprocess.py
                                    │
                                    ▼
                         同上目录追加 / 清理：
                         ├── GT.json
                         ├── info.json
                         └── （删除 *.npz）
```
