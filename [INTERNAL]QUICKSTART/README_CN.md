# IDEAL（中文说明，内部）

> 英文主文档见仓库根目录 [`README.md`](../README.md)。

本文档说明本仓库的目标、目录约定与基本使用方法，面向需要复现或扩展评测链路的内部成员。

## 概述

本仓库提供面向 **室内场景理解** 的 **数据集与评测基准** 相关工程实现。在统一几何与 JSON 协议下，用于评估多模态大模型（例如 GPT、Gemini、Qwen）从 **单张室内图像** 推断室内布局并输出结构化场景描述的能力；路线图包括 **短视频** 等多帧输入。评测支持在 Blender 中根据模型预测进行 **场景重建** 与 **对照渲染**，并从 **几何一致性**、**感知质量** 等维度汇总结果。

典型流水线为：**基于定制 Infinigen 的场景合成** → **预处理（真值、可见物体信息等）** → **视觉语言模型推理** → **客观几何指标** → **基于预测的几何重建与渲染** → **评判型模型等主观指标**。代码上分为 **数据生成与预处理**（`generation/`，依赖 Git 子模块内 Infinigen）与 **评测相关组件**（`inference/`、`reconstruction/`、`benchmarks/` 等）。

## 运行环境

- **操作系统**：建议在 **Linux x86_64** 上开发与复现；`bpy` 及向 PyPI 发布的推理、重建、合成相关依赖在 Linux 上安装路径相对完整。若使用 macOS 或 Windows，须自行确认各包是否提供适用平台的构建及版本要求。
- **硬件**：**场景合成**通常需要 **NVIDIA GPU**；显存占用与 Cycles 采样数、渲染分辨率等参数相关。

---

## 依赖与安装

以下命令均在仓库根目录 **`IDEAL/`** 下执行。

### Python 包（按用途分层）

| 用途 | 命令 | 说明 |
|------|------|------|
| **生成 + 推理 + 重建（默认）** | 先执行 `git submodule update --init`，再执行 `pip install -r requirements.txt` | 根目录 [`requirements.txt`](../requirements.txt) 通过 `-r infinigen-ideal/requirements.txt` 引入 Infinigen 全量依赖，并增加 `google-genai`、`openai`。运行 `generation/` **须**使用本组依赖；仅安装 `numpy`、`bpy`、`mathutils` 不足以运行合成管线。 |
| **仅推理与重建（不进行本地合成）** | `pip install -r requirements-minimal.txt` | [`requirements-minimal.txt`](../requirements-minimal.txt) 提供 `numpy`、`bpy`、`mathutils` 与云端 API 客户端的较小集合；**不包含** `gin-config`、`trimesh`、`scipy` 等，**不能**用于运行 `generation/`。 |

### Infinigen 锁定的依赖文件

[`infinigen-ideal/requirements.txt`](../infinigen-ideal/requirements.txt) 与上游管线对齐，常见条目包括：`bpy`、`gin-config`、`numpy`、`opencv-python`、`trimesh`、`imageio`、`scipy`、`matplotlib`、`scikit-image`、`Pillow`、`OpenEXR`、`usd-core` 等。其中可能包含以 **`git+ssh`** 形式声明的可编辑依赖（如 `infinigen_internal`、`robosuite`）；若无访问权限，需在团队 fork 或镜像上调整后重新安装。

### 场景合成与预处理（入口与关系）

| 组件 | 路径 | 说明 |
|------|------|------|
| 合成入口 | `generation/generate.py` | 调用 `generate_lib/`（例如 `core.py`）。 |
| 预处理入口 | `generation/preprocess.py` | 调用 `preprocess_lib/`，基于已有 `scene.blend` 产出真值、`objects_in_view` 等。 |

上述两条管线在代码结构上相互独立，无嵌套调用关系。Infinigen 子进程在子模块目录 **`infinigen-ideal/`** 内通过 `python -m infinigen_examples.single` 等形式启动。

### 合成产物的默认输出位置

路径常量见 [`configs/data_paths.py`](../configs/data_paths.py)：`data/scenes_raw`（本地生成，即 `SCENES_RAW_ROOT`）、`data/scenes_processed`、`scenes_*_hf`（HF 镜像），以及 **`infinigen-ideal/`** 子模块路径；预处理正式管线仍使用 sibling **`INFINIGEN/`** 下的 **`SCENES_PROCESSED`** / **`SCENES2TEST`** 等。

- **`generate.py`** 与 **`generate_lib/core.py`** 默认父目录均为 **`data/scenes_raw`**（见 `data_paths` 中 **`SCENES_RAW_ROOT`**）；**`generate.py`** 另维护 **`data/scenes_raw/manifests/`**（**`generation_batches.jsonl`**、各房型 **`*_scenes.csv`**，旧名 **`batches.jsonl`** 会在首次访问时迁为 **`generation_batches.jsonl`**）。需在其它根目录生成时，对 **`core.py`** 使用 **`--scenes-root`** / **`--output_folder`**。

### 进一步阅读

- 命令行参数与模块职责：`generation/README.md`、`generation/generate_lib/README.md`。
- 内部快速笔记（随仓库跟踪，**不对外发布**，见下文「含 `[INTERNAL]` 的目录」）：`[INTERNAL]QUICKSTART/`（如 `generate.md`、`preprocess.md`）与本文件 `README_CN.md`。
- `metadata.json` 字段定义：[`docs/METADATA_SCHEMA.md`](../docs/METADATA_SCHEMA.md)。

### `bpy` 版本

建议与所使用的 `.blend` 文件主版本一致（锁文件示例为 **4.2.x**）。推理、重建与合成宜在同一 **`bpy` 次版本**下运行，以减少兼容性问题。

### 各组件与依赖层级

| 组件 | 依赖说明 |
|------|----------|
| `inference/infer.py` | 上述两种安装方式均包含 `google-genai`、`openai`。 |
| `reconstruction/` | 需要 `bpy` 与 `mathutils`（全量依赖文件中已包含 `mathutils`；精简安装中单独列出）。渲染子进程固定为当前解释器 + `bpy_scripts/render.py`，不查找系统 `blender` 可执行文件。 |
| `generation/`（含 `generate.py`、`preprocess.py` 及 `generate_lib/`、`preprocess_lib/`） | 仅适用于 **全量** `requirements.txt`；依赖 Infinigen 侧 Python 与科学计算相关包。 |

---

## 重建阶段对 `bpy` 的调用方式

重建通过当前 Python 环境中的 **`bpy`**（`pip`/conda 安装的模块）加载 `.blend` 并渲染，与 InfiniGen 侧常见用法一致。**不**再提供独立 `blender` 可执行文件作为备选后端。

---

## 仓库职责

| 内容 | 说明 |
|------|------|
| 冻结的场景数据 | `data/scenes/`：每场景一个子目录；常见内容包括 `.blend`、渲染图、`scene_GT.json`、资产等（具体以数据集发布说明或本地同步结果为准）。 |
| 清单与划分 | `data/manifests/`：可存放入库后的场景列表、train/val/test 等划分（由发布或迁移脚本维护；与 `data/scenes_raw/manifests` 相互独立）。 |
| 推理 | `inference/`：`infer.py` 调用云端 VLM；`inview2sp.py` 将场景 `categories_in_view` 注入 `prompts/system_prompt_*.md` 中的 `{{THIS_ROOM_CONTAINS}}`（与 user 侧 `assets_list` 同源）；`prompts/` 为 Markdown 模板；预测默认写入仓库根 `result/`。 |
| 重建 | `reconstruction/`：根据模型输出的 JSON，在对应 `.blend` 中调整资产并按约定相机渲染。 |
| 评测 | `benchmarks/`：当前仅有占位目录 `evaluation/`、`protocols/`、`leaderboard/`（`.gitkeep`），供后续指标脚本、协议文稿与榜单使用；模型输出与指标快照默认与仓库根 `result/` 对齐。 |
| 配置与规范 | `configs/schema/`：JSON Schema（用于校验）；`configs/experiments/`：实验配置。 |
| 文档与约定 | `docs/`：几何与管线说明（附录性质）；`schema_design.json` 为人类可读的协议草图，**非** JSON Schema 校验器定义。 |
| API 与本地配置 | `[INTERNAL]api/check_api.py`：连通性检测；密钥等放在 `[INTERNAL]api/secrets/`（该目录下除 `.gitkeep` 外由 `.gitignore` 排除，见下文）。 |
| 内部维护脚本 | `[INTERNAL]tools/`：像素统计、`gpu_selecting` 等内部工具（非主评测链路；见下文「含 `[INTERNAL]` 的目录」）。 |

**矩形单房间合成与预处理**：由 `generation/generate.py`（经 `generate_lib/core.py`）实现合成；由 `generation/preprocess.py`（`preprocess_lib/`）从已有 `scene.blend` 产出真值与 `objects_in_view` 等，详见 `generation/README.md` 与 `generate_lib/README.md`。二者均依赖子模块 **`infinigen-ideal/`**。合成结果默认落盘位置见上文「合成产物的默认输出位置」。纳入评测前，请将场景 **复制或同步** 至 `data/scenes/`、`data/scenes2test/` 等约定路径。

---

## 目录结构

下列树形结构 **与当前 `git ls-files` 一致**（仅已跟踪路径；不含子模块 `infinigen-ideal/` 内部的成千上万文件）。层级统一展开到 **第四级**（例如 `generation/generate_lib/core.py`），便于对照模块职责。[`.gitignore`](../.gitignore)：**`data/*`**、**`result/*`**、**`[INTERNAL]api/secrets/*`** 被忽略，但分别保留 **`data/.gitkeep`**、**`result/.gitkeep`**、**`[INTERNAL]api/secrets/.gitkeep`**；另忽略常见 Python 缓存。未提交的本地文件**不会**出现在此表中。

### 含 `[INTERNAL]` 的目录（不对外发布）

路径名中包含 **`[INTERNAL]`** 的文件夹（例如 `[INTERNAL]QUICKSTART/`、`[INTERNAL]tools/`、`[INTERNAL]api/`）**仅供团队内部工作使用**，在对外公开发布的代码或数据包中**应剔除**，不属于正式发行内容。

```
IDEAL/
├── README.md                     # 英文主文档（对外）
├── requirements.txt              # -r infinigen-ideal + 推理客户端
├── requirements-minimal.txt      # 仅推理与重建
├── .gitignore
├── .gitmodules
├── data/
│   └── .gitkeep                  # 占位；其余 data/* 由脚本生成且被 ignore
├── result/
│   └── .gitkeep                  # 占位；推理/重建输出默认写 result/*，被 ignore
├── docs/                         # 几何、预处理与元数据约定（附录；均为 docs/*.md / schema_design.json）
│   ├── geometric_rules_floor.md
│   ├── GLOBAL_DESIGN.md
│   ├── LIMITATION.md
│   ├── LIVING_SPACE_RESIDENTIAL_DESIGN.md
│   ├── METADATA_SCHEMA.md
│   ├── PREPROCESS_DESIGN.md
│   ├── WORKING_SPACE_DESIGN.md
│   └── schema_design.json
├── configs/
│   ├── constant.py               # ROOM_TYPES、类别别名等
│   ├── data_paths.py             # data/* 路径、Infinigen 子模块路径
│   ├── gpu_select.py             # CUDA_VISIBLE_DEVICES / IDEAL_GPU 解析（生成与预处理共用）
│   ├── experiments/.gitkeep
│   └── schema/
│       ├── .gitkeep
│       └── single_scene_v1.json  # JSON Schema：单场景模型输出
├── generation/
│   ├── README.md
│   ├── generate.py               # 批量合成入口 → 子进程 generate_lib.core；写 manifests
│   ├── preprocess.py               # 预处理 CLI → preprocess_lib.cli
│   ├── generate_lib/
│   │   ├── core.py               # Infinigen 子进程、coarse/render、五件套收尾
│   │   ├── build_metadata.py     # 拼 metadata.json
│   │   ├── extract_metadata.py   # Blender 内导出 render/environment 片段
│   │   ├── extract_gt.py         # 写 GT.json（房间度量等）
│   │   └── __init__.py
│   ├── generation_common/        # 生成与预处理共用
│   │   ├── scene_geometry.py     # AABB、地板/天花、射线、房高
│   │   └── asset_rules.py        # 集合排除、collect_assets、相机/房间回退、scene_id
│   └── preprocess_lib/
│       ├── cli.py                # argparse 与 --final / 批处理等模式分发
│       ├── core.py               # 批处理子进程循环、manifest、RAW→SCENES2TEST 同步
│       ├── pipeline.py           # 单场景 bpy：GT.json、objects_in_view、npz 清理
│       ├── defaults.py           # BPY 解释器路径等默认常量
│       ├── extract_info.py       # objects_in_view.json；合并家具重叠摘要
│       ├── extract_camview.py    # 实例分割 + metadata 对齐 → 镜内列表
│       ├── check_overlap.py      # 家具两两重叠估计（由 extract_inview 调用）
│       └── __init__.py
├── inference/
│   ├── infer.py                  # VLM CLI；预测写入 result/{protocol}/{scene_id}/
│   ├── inview2sp.py              # 将 categories_in_view 注入 system 模板 {{THIS_ROOM_CONTAINS}}
│   ├── models/.gitkeep
│   └── prompts/
│       ├── .gitkeep
│       ├── system_prompt_floor.md
│       └── system_prompt_camera.md
├── reconstruction/
│   ├── reconstruct.py            # 重建批处理 / 单任务入口
│   ├── compose.py                # 预测写回并与 GT.json 合并 → *_render.json
│   ├── match.py                  # 按类别的预测与场景物体队列匹配
│   ├── runtime.py                # 扫描预测文件、路径解析等运行时辅助
│   ├── __init__.py
│   └── bpy_scripts/
│       ├── .gitkeep
│       └── render.py             # bpy：读 *_render.json，另存 .blend 并渲染 .png
├── benchmarks/
│   ├── evaluation/.gitkeep       # 预留：指标脚本
│   ├── protocols/.gitkeep        # 预留：评测协议文稿
│   └── leaderboard/.gitkeep      # 预留：榜单/汇总
├── infinigen-ideal/              # Git 子模块（完整 Infinigen 树，见 .gitmodules）
├── [INTERNAL]QUICKSTART/         # 内部使用的说明文档，上线前删掉
│   ├── README_CN.md              
│   ├── generate.md
│   └── preprocess.md
├── [INTERNAL]api/                # 内部使用的api相关文件，上线前删掉
│   ├── check_api.py              # API 连通性检测 CLI
│   └── secrets/
│       └── .gitkeep              # 占位；api_keys.csv 等本地文件被 ignore
└── [INTERNAL]tools/              # 内部使用的工具，上线前删掉
    ├── check_instance_pixel.py   # 实例图像素统计（可见性阈值调参）
    └── gpu_selecting.py          # 打印或 export CUDA_VISIBLE_DEVICES
```

### benchmarks/

当前 **无** 已跟踪的说明类 Markdown；仅有上述三个占位目录。在 `benchmarks/` 下新增的草稿或脚本需自行 `git add` 后才会进入版本库与上表。

### 运行产物目录（仓库根下 `result/`，默认被 `.gitignore` 排除）

目录层级为 **`protocol` → 场景子目录**。场景目录内 **不再** 划分 `json/`、`image/`、`blend/` 等子文件夹；`{model_id}.json`、`.png`、重建得到的 `.blend` 以及后续 benchmark 导出的 JSON 等 **在同一层级并列存放**，便于批处理。真值文件仍位于 `data/scenes2test/{scene_id}/GT.json`。

**文件命名约定（建议）**

| 类型 | 文件名模式 |
|------|------------|
| 推理输出 | `{model_id}.json`（与 `infer.py` 一致） |
| 重建渲染图 | `{model_id}.png` |
| 可选导出的场景 | `{model_id}.blend`（与预测 JSON 同目录） |
| 指标快照（自定义评测脚本时） | 例如 `{model_id}_benchmark.json` 或 `{model_id}_metrics.json` |

`reconstruct.py` 与 `runtime.py` 在扫描预测时会 **忽略** `*_benchmark.json`、`*_bench.json`，以避免与模型原始输出混淆。

- **`protocol`**：取值为 `floor` 或 `camera`，与 `inference/prompts/` 下 `system_prompt_{protocol}.md` 对应。
- **`scene_id`**：与 `data/scenes2test/` 下场景子目录名称一致。

`inference/infer.py` 将预测写入 **`result/{protocol}/{scene_id}/{model_id}.json`**。每次请求前由 `inview2sp.inject_into_system_prompt` 把该场景的 `categories_in_view` 填入 `prompts/system_prompt_*.md` 中的 **`{{THIS_ROOM_CONTAINS}}`**（user 消息中的 `assets_list` 与之一致）。

### 重建流程与命令

**入口**：`reconstruction/reconstruct.py`。

**阶段顺序**：`compose.py`（内部通过 `match.py` 完成类别队列匹配；将预测几何写回并与 `GT.json` 合并）→ 在同目录生成 **`{model_id}_render.json`** → `bpy_scripts/render.py` 读取该 JSON，另存 **`{model_id}.blend`** 并渲染 **`{model_id}.png`**。源场景 **`scene.blend`** 默认路径为 **`data/scenes2test/{scene_id}/scene.blend`**（不覆盖源文件）。

**批处理**（在仓库根 **`IDEAL/`** 下执行；按「场景 × 模型」顺序处理；任务总数会输出至标准错误流）：

| 范围 | 参数示例 |
|------|----------|
| 仅 `floor` 协议 | `--protocol floor`，或 `--task-dir result/floor` |
| 仅 `camera` 协议 | `--protocol camera`，或 `--task-dir result/camera` |
| 两协议全部场景 | 省略 `--protocol`，且不指定 `--task-dir` / `--prediction` |

可选缩小范围：`--model-id`、`--scene`（可重复）。

**单次任务**：`--prediction result/floor/<scene>/…/<model_id>.json`，或 `--task-dir result/floor/<scene>/`（多模型时需配合 `--model-id`）。

**仅列出任务（`--dry-run`）示例**：

```bash
cd /path/to/IDEAL
python reconstruction/reconstruct.py --protocol floor --dry-run
python reconstruction/reconstruct.py --task-dir result/camera --dry-run
```

相对路径首先相对于 **当前工作目录** 解析；若无法解析，则相对于 **仓库根 `IDEAL/`** 解析。

**渲染后端**：固定为当前解释器的 **`bpy`**（运行 `render.py` 子进程前会检查 `import bpy`）。渲染使用 **`scene.blend` 中当前活动相机** 及文件内渲染设置。

### 重建相关模块职责

- **`match.py`**：仅完成 **按类别的队列匹配**：基于 `info.objects` 建队，逐条与预测对应；冗余预测生成 `placeholder_*`；未占用的槽位几何置为 `null`。输出为预测与场景中物体的配对列表，**不写磁盘**。
- **`compose.py`**：对配对结果执行 `apply_object_overlay`（写回预测几何），再经 `merge_gt_with_info_layer` 与完整 **`GT.json`** 合并，写出 **`{model_id}_render.json`**。
- **`render.py`（`bpy`）**：依据 **`PREPROCESS_DESIGN.md`** 第 3 节将 JSON 写回场景对象（`protocol=floor` 时涉及 `position`、`dimensions`、`rotation_yaw` 等；空物体尺寸由子树网格世界包围盒估算）。**`protocol=camera`** 时不写回几何。

### API 密钥与本地配置（`[INTERNAL]api/`）

- **`[INTERNAL]api/check_api.py`** 已跟踪入库；**`[INTERNAL]api/secrets/*`**（如 `api_keys.csv`）由 `.gitignore` 排除，仅 **`[INTERNAL]api/secrets/.gitkeep`** 占位，避免密钥进远程仓库。
- 本地使用：在 `[INTERNAL]api/secrets/api_keys.csv` 中按列填写（与 `inference/infer.py`、`check_api.py` 一致：`provider`、`model`、`api_key`、`base_url`）；若存在 `[INTERNAL]api/secrets/README.md`，以其中说明为准。
- 可选：在 `[INTERNAL]api/` 下提供不含真实密钥的 `api_keys.example.csv`（列名相同）供复制为 `secrets/api_keys.csv`。

### 开源模型权重（示例：Qwen）

权重宜存放在 **仓库外部** 的专用目录（例如 `MODELS/`、`~/models`），通过环境变量或实验配置传入路径。请避免将大体积权重文件放入 `IDEAL/inference/models/`（该目录仅存放封装代码）。使用独立目录名有助于区分 **权重资产** 与 **推理源码**。

---

## 数据与代码边界

- **大体量资产**（大量 `.blend`、高分辨率图像等）长期建议配合 Hugging Face Dataset 或等效托管方式；本仓库以 **目录约定与清单（manifest）** 为主，便于与远端数据版本对齐。
- **几何约定**（坐标系、锚点、单位、旋转等）：以 `docs/geometric_rules_floor.md` 为准；整体约束见 `docs/GLOBAL_DESIGN.md` 等。
- **机读校验**：使用 `configs/schema/` 下的 JSON Schema（例如 `single_scene_v1.json`）：顶层仅含 `scene`；`scene.room` 含 `type`、`size`（房间 W、D、H）、`asset_count`；每个物体以 **`dimensions`**（而非 `size`）描述三维尺寸；**真值 `GT.json` 可包含 `scene_id`，模型推理输出不应包含 `scene_id`**。
- **`docs/schema_design.json`** 为协议与字段的人类可读草案，**不是** JSON Schema 规范文件。

---

## 许可证

待定；随数据集与代码发布策略确定。
