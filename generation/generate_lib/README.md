# `generate_lib/`：单房合成 / 渲染后端

供 **[`../generate.py`](../generate.py)** 子进程调用；**不属于** `preprocess_lib`。对已打开的 `scene.blend` 写 **`GT.json`** 时用本目录的 **`extract_gt.py`**。批处理重写 **`objects_in_view.json`**（可见性 / 长宽比 / 截断）用仓库 **`../inview.py`**；若要在 Blender 内单次写出并附带**家具重叠摘要**字段，可调用 **`../preprocess_lib/extract_inview.py`** 的 **`write_info_json_v2`**（与 `inview.py` 顶层字段可能不完全一致，见 **`../README.md`** 与 `docs/PREPROCESS_DESIGN.md`）。

## 职责

| 文件 | 作用 |
|------|------|
| **`core.py`** | 解析 CLI → 调 `infinigen_examples.single`（coarse/render 等）；Object Index 副产物、写五件套（`GT.json` 由当前解释器内 `bpy` 写出）。档位 / 房型逻辑与 gin 覆盖集中在此。 |
| **`extract_gt.py`** | 在已打开的 `scene.blend` 里解析房间盒与资产列表，写 `GT.json`；亦可用带 `bpy` 的解释器 **`python extract_gt.py -- <scene_dir>`** 作为 CLI。 |
| **`build_metadata.py`** | 从 `scene.blend` + Infinigen `Objects*.json` 拼 `metadata.json`；相机来自当前解释器内 `bpy`（`open_mainfile`）。 |
| **`extract_metadata.py`** | 在带 `bpy` 的解释器内运行，写出 render / environment 片段 JSON（与 `extract_gt.py` 并列）。 |
## 依赖

- Infinigen 工作副本：`configs.data_paths.INFINIGEN_SUBMODULE_ROOT`（默认相对路径为仓库内子模块根目录）。
- 房型与路径：同上，见仓库根 `configs/constant.py`、`configs/data_paths.py`。
- **`extract_gt.py`** 使用 [`../generation_common/`](../generation_common/)（`scene_geometry`、`asset_rules`），与预处理共用。

## 注意事项

- **矩形单房间**（`shapely.box` / `living_space`·`working_space` 约束）；不规则多房公寓不在此链路，见 [`../../docs/LIMITATION.md`](../../docs/LIMITATION.md)。
- **`generate.py`** 固定 coarse+render、去噪开启；只跑部分阶段或自定义 `--task`/I/O 时需直接跑 **`core.py`**（说明见 **[`../../[INTERNAL]QUICKSTART/generate.md`](../../[INTERNAL]QUICKSTART/generate.md)**）。

## 运行说明

命令示例、环境与 `core.py` 参数见 **[`../../[INTERNAL]QUICKSTART/generate.md`](../../[INTERNAL]QUICKSTART/generate.md)**；本文不重复操作步骤。
