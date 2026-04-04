# 生成与预处理（`generation/`）

两条**并列**流程，互不嵌套：

| 入口 | 库目录 | 说明 |
|------|--------|------|
| **`generate.py`** | **`generate_lib/`** | Infinigen 单房间合成与渲染（`core.py` 等） |
| **`preprocess.py`** | **`preprocess_lib/`** | 对已有 `scene.blend` 做 GT / info 等预处理 |

- **`tools/fix_existing_scene.py`**（仓库根 [`../tools/fix_existing_scene.py`](../tools/fix_existing_scene.py)）：一次性为旧 `SCENES_RAW` 补 `instance_segmentation` / `metadata`（内部调用 `generate_lib.core`）；数据修完后可删，无其它模块依赖。

路径常量（`INFINIGEN_IDL3D_ROOT`、`SCENES_*`）见仓库根 **`../configs/infinigen_paths.py`**。房型 canonical 列表：**`../configs/constant.py`**（`ROOM_TYPES`）；新生成目录名为 `{canonical}_s{seed}`，旧批次旧名并存、无兼容脚本。

- 场景 **`metadata.json`** 字段含义（英文约定，不写进 JSON 正文）：**[`../docs/METADATA_SCHEMA.md`](../docs/METADATA_SCHEMA.md)**
- 详细脚本说明：**[`generate_lib/README.md`](generate_lib/README.md)**（生成侧；内含与 `preprocess` 的衔接说明）。
