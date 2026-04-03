# IDL3D

本仓库是 **室内空间理解** 方向的 **数据集与评测（Datasets & Benchmarks）** 工程：在固定几何与 JSON 协议下，评测多模态大模型（如 GPT、Gemini、Qwen 等）从**单张室内图像**（以及规划中的**短视频**）中理解布局并输出结构化场景描述的能力；并支持在 Blender 中依据模型输出做**场景重建**与**渲染对比**，从**客观几何指标**与**主观图像指标**两方面打分。

更完整的产品与流程说明见 [`项目资料/D&B项目.md`](项目资料/D&B项目.md)。

**运行环境**：建议在 **Linux**（常见为 x86_64）上进行开发与复现。推理、重建及 PyPI 上的 **`bpy`** 等依赖在 Linux 上最易一键安装；若使用 macOS / Windows，需自行核对各包是否提供对应轮子及版本。

**Python 依赖**：仓库根目录 [`requirements.txt`](requirements.txt)（含 `numpy` / **`bpy`** 及推理客户端等）。在 **`IDL3D/`** 下执行 `pip install -r requirements.txt`。重建默认 **`--runner bpy`**：用当前解释器的 **`bpy` 包** 加载 `.blend` 并渲染（与 InfiniGen 一致），**不要求**安装带 UI 的 Blender 或把 `blender` 加入 PATH。若希望改用官方命令行，可加 **`--runner blender`**（并安装 Blender 或设置 `BLENDER_EXECUTABLE`）。`bpy` 版本建议与场景 `.blend` 主版本接近（如 4.2.x）。

---

## 仓库职责（当前阶段）

| 内容 | 说明 |
|------|------|
| **冻结的场景数据** | 收录在 `data/scenes/`（每场景一个子目录，含 `.blend`、渲染图、`scene_GT.json`、资产等，以你实际落盘为准）。 |
| **清单与划分** | `data/manifests/` 可存放入库后的场景列表、train/val/test 等 split（由搬运/发布脚本维护；与 InfiniGen 侧 `SCENES_RAW/manifests` 分离）。 |
| **推理** | `inference/`：调用各 VLM；**system prompt** 见 `inference/prompts/`（运行时加载）；输出路径约定见实验配置。 |
| **重建** | `reconstruction/`：根据模型生成的 JSON 在对应 `.blend` 中调整资产并由同一相机渲染。 |
| **评测** | `benchmarks/`：客观/主观指标与评测协议；各模型在该场景上的产出与指标快照见仓库根 `result/`（按场景聚合）。 |
| **配置与规范** | `configs/schema/`（**JSON Schema**，用于校验）、`configs/experiments/`（实验配置）。 |
| **文档与约定** | `docs/`：几何规则（论文附录级）、协议草图（`schema_design.json`，人类可读，非校验器）。 |
| **API 与本地配置** | `api/`：如 `check_api.py`、密钥 CSV 等；**整目录默认不提交**（见 `.gitignore` 与下文）。 |

**场景生成与预处理**：目前仍在独立工程（InfiniGen 及自有脚本）中完成；处理完成后将场景**手动拷贝**至本仓库的 `data/scenes/`。后续若迁移生成/预处理代码，可再单独规划子模块或新仓库，**不阻塞**本仓库以「冻结数据 + 评测与推理」为主线的开发。

---

## 目录结构

**Git**：根目录 [`.gitignore`](.gitignore) 忽略 `data/`、`result/`、`api/`（大数据与测试场景、运行产物、本地 API 密钥与辅助脚本）。克隆仓库后需在本地自备数据目录与 `api/` 配置；下文路径均为**工作区约定**。

```
IDL3D/
├── README.md                 # 本说明
├── docs/                     # 几何规则、数据形状说明（附录级文档；非 JSON Schema 校验器）
├── 项目资料/                 # 设计文档与笔记（如 D&B 项目说明）
├── data/                     # （默认不提交）冻结数据与测试场景
│   ├── scenes/               # 收录后的场景数据（每场景一目录）
│   ├── scenes2test/          # 推理用测试场景（每场景一目录，含 image.png 等）
│   └── manifests/            # 场景清单、划分、版本元数据等
├── configs/
│   ├── schema/               # 输出 / GT 的格式与版本约定
│   └── experiments/          # 实验与模型批量运行配置
├── result/                   # （默认不提交）按场景聚合：模型 JSON、重建图、可选 .blend、benchmark 结果等（见下）
├── inference/
│   ├── infer.py              # 云端 API 推理入口（示例）
│   ├── migrate_runs_to_result.py  # 旧 runs/ → result/（一次性）
│   ├── migrate_nested_result_to_flat_root.py  # 旧 inference/result/ 嵌套 → result/ 扁平（一次性）
│   ├── models/               # 各模型封装（代码，不含权重）
│   └── prompts/              # System prompt（floor / camera 等）
├── reconstruction/           # 依据预测 JSON 在 Blender 中重建与渲染
│   ├── reconstruct.py        # 入口：compose → {model}_render.json → render
│   ├── compose.py            # 写回几何、合并 GT、写出 _render.json（内部调 match）
│   ├── match.py              # 仅类别队列配对、占位与 nullify
│   ├── runtime.py            # result 路径布局、任务发现、bpy / Blender 可执行文件
│   └── blender_scripts/      # render.py（依赖 bpy）
├── benchmarks/
│   ├── metrics/
│   │   ├── objective/        # 几何等硬指标
│   │   └── subjective/       # 图像/主观相关指标
│   ├── protocols/            # 评测协议说明
│   └── leaderboard/          # 榜单生成等（可选）
└── api/                      # （默认不提交）本地 API 检测与密钥
    ├── check_api.py          # 本地检测 Gemini / OpenAI 等是否可用（可选）
    └── secrets/
        ├── README.md         # 放置说明（仅本地）
        └── api_keys.csv      # provider / model / api_key / base_url（勿提交）
```

### `result/`（仓库根：推理、重建与 benchmark 产物）

按 **protocol → 场景** 一层目录；**场景文件夹内不再分子目录**（无 `json/`、`image/`、`blend/`），`{model_id}.json`、`.png`、`_reconstructed.blend` 与后续 **benchmark** 导出的 JSON 等**平铺并列**，便于大规模跑批时不必为未生成 `.blend` 的场景留空子文件夹。**GT** 仍在 `data/scenes2test/{scene_id}/GT.json`。

建议命名（约定）：

- **推理**：`{model_id}.json`（与 `infer.py` 一致）
- **重建渲染图**：`{model_id}.png`
- **可选导出场景**：`{model_id}.blend`（重建管线另存，与预测同目录）
- **benchmark 指标**（自行实现评测脚本时）：例如 `{model_id}_benchmark.json` 或 `{model_id}_metrics.json`；`reconstruct.py` / `runtime.py` 扫描预测时**会忽略** `*_benchmark.json` / `*_bench.json`，避免与模型输出混淆。

- **`protocol`**：`floor` / `camera`，与 `inference/prompts/` 下 `system_prompt_{protocol}.md` 对应。
- **`scene_id`**：`data/scenes2test/` 下场景子目录名。

`inference/infer.py` 将预测写入 **`result/{protocol}/{scene_id}/{model_id}.json`**。

### 重建渲染

入口：**`reconstruction/reconstruct.py`**。顺序：**`compose.py`**（内部 **`match.py`** 做类别队列配对；再写回预测几何、与 **`GT.json`** 合并）→ 写出同目录 **`{model_id}_render.json`** → **`blender_scripts/render.py`** 读该 JSON，另存 **`{model_id}.blend`** 并渲染 **`{model_id}.png`**。源 **`scene.blend`** 默认 **`data/scenes2test/{scene_id}/scene.blend`**（不覆盖）。

**批量**（在 **`IDL3D/`** 下执行；会顺序处理每个「场景 × 模型」预测；stderr 会打印任务总数）：

- **仅 `floor`**：**`--protocol floor`**，或等价地 **`--task-dir result/floor`**
- **仅 `camera`**：**`--protocol camera`**，或 **`--task-dir result/camera`**
- **两个协议全部场景**：省略 **`--protocol`** 且不要传 **`--task-dir`** / **`--prediction`**

可选过滤：**`--model-id`**、**`--scene`**（可重复）缩小范围。

**单任务**：**`--prediction result/floor/bedroom_.../gpt-5.2-2025-12-11.json`**，或 **`--task-dir result/floor/bedroom_...`**（多模型时需 **`--model-id`**）。

**`--dry-run`** 示例：

```bash
cd /path/to/IDL3D
python reconstruction/reconstruct.py --protocol floor --dry-run
python reconstruction/reconstruct.py --task-dir result/camera --dry-run
```

相对路径会先按**当前工作目录**解析，否则按**仓库根 `IDL3D/`** 解析。

**重建如何调用 Blender**：默认 **`--runner bpy`**（**`IDL3D_RECON_RUNNER`** 可覆盖）。**渲染与相机**：使用 **`scene.blend` 内活动相机**及文件内 Render 设置。

**迁移**：历史 **`runs/`** → **`python inference/migrate_runs_to_result.py`**；若曾使用嵌套的 **`inference/result/.../json/`** → **`python inference/migrate_nested_result_to_flat_root.py`**。任务发现逻辑兼容旧路径解析，新输出始终写入 **`result/`** 扁平布局。

### 重建：匹配、组合与渲染

- **`match.py`**：仅 **类别队列配对**：`info.objects` 建队列，预测逐条 dequeue；多报则追加 `placeholder_*`；未占用槽位几何置 `null`。返回「预测 ↔ 底稿物体」配对列表，**不写文件**。
- **`compose.py`**：对配对执行 **`apply_object_overlay`**（预测几何写回），再 **`merge_gt_with_info_layer`** 与全文 **`GT.json`** 合并，写出 **`{model_id}_render.json`**。
- **`render.py`**（Blender）：按 **`PREPROCESS_DESIGN.md` §3** 将 JSON 写回场景物体（`protocol=floor`）：`position` / `dimensions` / `rotation_yaw`；Empty 零尺寸时用子树 MESH 世界 AABB 估算缩放。**`protocol=camera`** 下不写回几何。

### API 密钥与 `api/` 目录

- **当前 `.gitignore`**：忽略整个 `api/`，因此 **`check_api.py`、`secrets/api_keys.csv` 等都不会进入远程仓库**，密钥与工作流脚本仅保留在本地，适合「仓库公开、密钥不外泄」。
- **本地配置**：在 `api/secrets/api_keys.csv` 中按列填写（与 `inference/infer.py`、`api/check_api.py` 读取格式一致：`provider`、`model`、`api_key`、`base_url`）；详见 `api/secrets/README.md`（若存在）。
- **若希望把 `check_api.py` 提交到 GitHub、仅忽略密钥**：可将 `.gitignore` 改为只忽略 `api/secrets/*`，并在 `api/` 下提供可提交的 `api_keys.example.csv`（列名同上、无真实密钥）。

### 开源模型权重（如 Qwen）

权重放在**仓库外**的专用目录（例如服务器上的 `MODELS/` 或 `~/models`），通过环境变量或实验配置传入路径；**不要**把大文件放进 `IDL3D/inference/models/`（该目录仅放代码）。若目录命名为 `MODELS`，与「推理代码」语义区分清晰，完全可以。

---

## 数据与代码边界说明

- **大数据量资产**（大量 `.blend`、高清图等）长期建议配合 **Hugging Face Dataset** 或等价托管；本仓库以**目录约定 + manifest** 为主，便于与远端数据版本对齐。
- **几何规则**（坐标系、锚点、单位、旋转约定等）：权威说明见 `docs/geometric_rules_floor.md`；与 `项目资料/D&B项目.md` 应对齐。**机读校验**使用 `configs/schema/` 下的 JSON Schema（如 `single_scene_v1.json`）：顶层仅含 `scene`；`scene.room` 含 `type`、`size`（房间 W,D,H）、`asset_count`；每个物体用 **`dimensions`**（不用 `size`）表示三维尺寸；**地面真值 `GT.json` 可含 `scene_id`，模型推理输出不要包含 `scene_id`**。`docs/schema_design.json` 为协议/字段设计草图，**不是** JSON Schema 文档。

---

## 许可证

待定（随数据集与代码发布策略一并确定）。
