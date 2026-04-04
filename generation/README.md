# `generation/`：生成与预处理

两条管线**并列**，代码互不嵌套：

| 入口 | 库 | 作用 |
|------|-----|------|
| [`generate.py`](generate.py) | [`generate_lib/`](generate_lib/) | Infinigen **矩形单房间**：合成 + 渲染，收尾四文件 |
| [`preprocess.py`](preprocess.py) | [`preprocess_lib/`](preprocess_lib/) | 对已有 `scene.blend` 产出 GT / `objects_in_view` 等 |

**依赖与约定**

- 路径：`INFINIGEN_IDL3D_ROOT`、`SCENES_*` 见 [`../configs/infinigen_paths.py`](../configs/infinigen_paths.py)；房型 canonical 见 [`../configs/constant.py`](../configs/constant.py)（`ROOM_TYPES`）。新场景目录名一般为 `{canonical}_s{seed}`。
- **如何运行**（参数、tmux、`--scenes-raw`、manifests、`core.py` 细节）：[**`../QUICKSTART/generate.md`**](../QUICKSTART/generate.md)。
- **生成侧模块关系**（`core` / `metadata_builder` 等）：[`generate_lib/README.md`](generate_lib/README.md)。
- `metadata.json` 字段约定：[`../docs/METADATA_SCHEMA.md`](../docs/METADATA_SCHEMA.md)。
- 单房 box 局限：[`../docs/LIMITATION.md`](../docs/LIMITATION.md)。

**其它**

- 仓库根的 [`../tools/fix_existing_scene.py`](../tools/fix_existing_scene.py)：为旧 RAW 补 `instance_segmentation` / `metadata`（调 `generate_lib.core`），非主链路。
