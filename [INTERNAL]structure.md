# IDEAL 项目结构说明（按文件功能整理）

本文档按“目录 -> 文件 -> 功能”整理 IDEAL 仓库当前版本（不展开子模块 `infinigen-ideal/` 内部源码）。

## 1. 根目录

- `README.md`
  - 项目总览与端到端流程说明：场景生成、预处理、推理、重建、评测。
  - 提供快速启动命令和数据组织约定。

- `requirements.txt`
  - 全量依赖（包括对子模块 `infinigen-ideal/requirements.txt` 的引用）。
  - 适用于“本地场景生成 + 推理 + 重建”的完整流程。

- `requirements-minimal.txt`
  - 精简依赖（偏推理/重建与 API 调用）。
  - 适用于不在本机跑 Infinigen 场景生成的场景。

- `.gitignore`
  - 忽略大体量数据与中间结果（如 `data/` 下场景数据），保留占位文件。

- `.gitmodules`
  - 子模块定义（`infinigen-ideal/`）。

## 2. [INTERNAL]QUICKSTART（内部快速上手）

- `[INTERNAL]QUICKSTART/README_CN.md`
  - 中文上手文档：环境配置、安装方式、生成/预处理/推理/重建入口。

- `[INTERNAL]QUICKSTART/generate.md`
  - 场景生成实操指南：单卡/多卡、批量策略、参数示例。

- `[INTERNAL]QUICKSTART/preprocess.md`
  - 预处理指南：输入输出结构、批处理方式与注意事项。

- `[INTERNAL]QUICKSTART/Untitled`
  - 也是一份快速上手内容（含环境、生成与预处理片段），可视作草稿版补充文档。

## 3. [INTERNAL]api（API 调试与密钥配置）

- `[INTERNAL]api/check_api.py`
  - API 连通性测试脚本。
  - 从 CSV 读取 provider/model/key/base_url，支持交互式请求测试（Gemini/OpenAI）。

- `[INTERNAL]api/secrets/api_keys.csv`
  - API 配置表（敏感信息容器），用于 `check_api.py` 与推理脚本读取。

- `[INTERNAL]api/secrets/.gitkeep`
  - 目录占位。

## 4. [INTERNAL]tools（内部工具）

- `[INTERNAL]tools/check_instance_pixel.py`
  - 统计实例分割图中对象像素量，用于可见性阈值分析（如最小可见像素）。

- `[INTERNAL]tools/gpu_selecting.py`
  - GPU 选择辅助脚本：支持解析 `CUDA_VISIBLE_DEVICES`，或按 `nvidia-smi` 负载自动选卡。

## 5. configs（全局配置）

- `configs/constant.py`
  - 房间类型、资产类别映射和规范化函数。
  - 统一类别命名，避免训练/推理/评估阶段类别不一致。

- `configs/data_paths.py`
  - 项目路径常量集中定义（仓库根、数据根、原始场景、处理后场景等）。

- `configs/gpu_select.py`
  - GPU 设备选择逻辑库（CLI 参数 / 环境变量 / 自动选择）。

- `configs/schema/single_scene_v1.json`
  - 模型输出 JSON Schema（单场景协议）。
  - 约束 `scene.room` 与 `scene.objects` 字段结构及类型。

- `configs/schema/.gitkeep`
  - 目录占位。

- `configs/experiments/.gitkeep`
  - 实验配置目录占位。

## 6. docs（设计规范与协议文档）

- `docs/GLOBAL_DESIGN.md`
  - 全局设计规范：坐标系、尺寸分档、房间约束总体原则。

- `docs/LIMITATION.md`
  - 已知限制说明：合成数据偏差、建模边界、协议适用范围。

- `docs/METADATA_SCHEMA.md`
  - `metadata.json` 规范说明：字段语义、实例映射、编码约定。

- `docs/PREPROCESS_DESIGN.md`
  - 预处理设计：GT 提取、镜内对象提取、重叠检测和产物格式。

- `docs/WORKING_SPACE_DESIGN.md`
  - 办公/教学等工作空间类别的生成约束与布局规则。

- `docs/LIVING_SPACE_RESIDENTIAL_DESIGN.md`
  - 住宅类房间（卧室/客厅/厨房等）的规则、尺寸和家具约束。

- `docs/geometric_rules_floor.md`
  - floor 协议下的几何定义：坐标系、尺寸、旋转等核心规则。

- `docs/schema_design.json`
  - 协议草案示意（人读友好版），用于说明结构；正式验证以 `configs/schema/single_scene_v1.json` 为准。

## 7. generation（场景生成与预处理入口）

- `generation/README.md`
  - generation 模块使用说明与子模块职责说明。

- `generation/generate.py`
  - 生成主入口：批量调度单房间合成，维护 manifest/统计信息。

- `generation/preprocess.py`
  - 预处理主入口：按参数调用 `preprocess_lib`，支持单场景与批处理。

### 7.1 generation/generate_lib

- `generation/generate_lib/__init__.py`
  - 包说明（生成相关 helper 汇总入口）。

- `generation/generate_lib/README.md`
  - `core.py`、`metadata_builder.py`、`blender_dump_metadata.py` 的职责说明。

- `generation/generate_lib/core.py`
  - 单场景生成核心调度：拼装 Infinigen 命令、设置房型参数、控制输出目录。

- `generation/generate_lib/metadata_builder.py`
  - 汇总场景资产与相机等信息，生成标准化 `metadata.json`。

- `generation/generate_lib/blender_dump_metadata.py`
  - Blender 内执行脚本，导出相机和环境相关元数据。

### 7.2 generation/preprocess_lib

- `generation/preprocess_lib/__init__.py`
  - 包说明（预处理流水线组件集合）。

- `generation/preprocess_lib/cli.py`
  - 预处理 CLI 参数定义与模式路由。

- `generation/preprocess_lib/core.py`
  - 预处理总控：扫描任务、调用单场景子流程、收集结果。

- `generation/preprocess_lib/pipeline.py`
  - 单场景流水线：产出 `GT.json` 与 `objects_in_view.json`，并执行清理逻辑。

- `generation/preprocess_lib/defaults.py`
  - 预处理阶段常量（文件名、默认参数、键名等）。

- `generation/preprocess_lib/extract_gt.py`
  - 从场景中提取 GT（房间与对象几何信息）并写出 `GT.json`。

- `generation/preprocess_lib/extract_camview.py`
  - 计算相机视角内对象集合与可见属性。

- `generation/preprocess_lib/extract_info.py`
  - 组织镜内对象信息，写出 `objects_in_view.json`，并合并重叠摘要。

- `generation/preprocess_lib/overlap_check.py`
  - 家具重叠检测（体素/几何回退策略），输出 pairwise overlap 摘要。

## 8. inference（VLM 推理）

- `inference/infer.py`
  - 推理主入口：遍历场景、加载图片与可见对象列表、构造消息、调用 API、写入 `result/`。

- `inference/inview2sp.py`
  - 将 `objects_in_view` 中信息注入系统提示词（如“当前房间包含哪些类别”）。

### 8.1 inference/prompts

- `inference/prompts/system_prompt_floor.md`
  - floor 协议系统提示词模板。

- `inference/prompts/system_prompt_camera.md`
  - camera 协议系统提示词模板。

- `inference/prompts/.gitkeep`
  - 目录占位。

### 8.2 inference/models

- `inference/models/.gitkeep`
  - 目录占位（预留模型相关资源）。

## 9. reconstruction（重建与渲染）

- `reconstruction/__init__.py`
  - 包说明（重建流水线组件）。

- `reconstruction/reconstruct.py`
  - 重建总入口：发现推理结果，按任务执行 compose + 渲染流程。

- `reconstruction/compose.py`
  - 组装重建输入：将预测结果和场景资产槽位对齐，生成 render 需要的数据 JSON。

- `reconstruction/match.py`
  - 对象匹配策略：按类别与顺序将预测对象映射到可见对象槽位。

- `reconstruction/runtime.py`
  - 运行时路径/任务解析与环境辅助函数。

### 9.1 reconstruction/blender_scripts

- `reconstruction/blender_scripts/render.py`
  - Blender 渲染脚本：读取 compose 产物，回写对象姿态并导出渲染结果。

- `reconstruction/blender_scripts/.gitkeep`
  - 目录占位。

## 10. benchmarks（评测框架骨架）

- `benchmarks/evaluation/.gitkeep`
  - 评测指标代码预留目录。

- `benchmarks/leaderboard/.gitkeep`
  - 排行榜与汇总展示预留目录。

- `benchmarks/protocols/.gitkeep`
  - 协议级评测实现预留目录。

## 11. data / result（数据与结果目录）

- `data/.gitkeep`
  - 数据目录占位（实际场景数据通常不入库）。

- `result/.gitkeep`
  - 推理与重建结果目录占位。

## 12. 子模块

- `infinigen-ideal/`
  - Infinigen 子模块，用于场景程序化生成。
  - 本文档仅说明其在 IDEAL 中的角色，不展开子模块内部源码。

## 13. 典型调用链（帮助快速定位）

- 生成：`generation/generate.py` -> `generation/generate_lib/core.py` -> `generation/generate_lib/metadata_builder.py`
- 预处理：`generation/preprocess.py` -> `generation/preprocess_lib/core.py` -> `generation/preprocess_lib/pipeline.py` -> `extract_*`
- 推理：`inference/infer.py` + `inference/inview2sp.py` + `inference/prompts/*.md`
- 重建：`reconstruction/reconstruct.py` -> `reconstruction/compose.py` -> `reconstruction/blender_scripts/render.py`

---

如需下一步，我可以基于这个结构继续生成两份文档：
1) `开发者入口速查`（按任务给出最短命令和关键文件）
2) `新同学阅读顺序`（先看哪些文件，为什么）
