# `generate_lib/`：单房合成 / 渲染后端

供 **[`../generate.py`](../generate.py)** 子进程调用；**不属于** `preprocess_lib`。预处理入口在 [`../preprocess.py`](../preprocess.py)。

## 职责

| 文件 | 作用 |
|------|------|
| **`core.py`** | 解析 CLI → 调 `infinigen_examples.single`（coarse/render 等）；可选 GT 第二趟、写四件套（`scene.blend`、`color.png`、uint16 `instance_segmentation.png`、`metadata.json`）。档位 / 房型逻辑与 gin 覆盖集中在此。 |
| **`metadata_builder.py`** | 从 `scene.blend` + Infinigen `Objects*.json` 拼 `metadata.json`；相机来自本机 `bpy` 或 **`blender_dump_metadata.py`** 子进程。 |
| **`blender_dump_metadata.py`** | 仅在 Blender 内运行，写出相机片段 JSON。 |

## 依赖

- Infinigen 工作副本：`../../infinigen-idl3d/`（与 `configs.infinigen_paths` 一致）。
- 房型与路径：同上，见仓库根 `configs/constant.py`、`configs/infinigen_paths.py`。

## 注意事项

- **矩形单房间**（`shapely.box` / `living_space`·`working_space` 约束）；不规则多房公寓不在此链路，见 [`../../docs/LIMITATION.md`](../../docs/LIMITATION.md)。
- **`generate.py`** 固定 coarse+render、去噪开启；只跑部分阶段或自定义 `--task`/I/O 时需直接跑 **`core.py`**（说明见 **[`../../QUICKSTART/generate.md`](../../QUICKSTART/generate.md)**）。
- 一次性修补旧数据：[`../../tools/fix_existing_scene.py`](../../tools/fix_existing_scene.py)（内部用 `core._finalize_scene_outputs`），非核心依赖。

## 运行说明

命令示例、环境与 `core.py` 参数见 **[`../../QUICKSTART/generate.md`](../../QUICKSTART/generate.md)**；本文不重复操作步骤。
