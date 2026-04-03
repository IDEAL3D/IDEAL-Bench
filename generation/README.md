# 生成与预处理（`generation/`）

两条**并列**流程，互不嵌套：

| 入口 | 库目录 | 说明 |
|------|--------|------|
| **`generate.py`** | **`generate_lib/`** | Infinigen 单房间合成与渲染（`generate_box.py` 等） |
| **`preprocess.py`** | **`preprocess_lib/`** | 对已有 `scene.blend` 做 GT / info 等预处理 |

路径常量（`INFINIGEN_IDL3D_ROOT`、`SCENES_*`）见仓库根 **`../configs/infinigen_paths.py`**。

- 详细脚本说明：**[`generate_lib/README.md`](generate_lib/README.md)**（生成侧；内含与 `preprocess` 的衔接说明）。
