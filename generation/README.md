# `generation/`：生成与预处理

两条管线**并列**，代码互不嵌套：

| 入口 | 库 | 作用 |
|------|-----|------|
| [`generate.py`](generate.py) | [`generate_lib/`](generate_lib/) | Infinigen **矩形单房间**：合成 + 渲染，收尾四文件 |
| [`preprocess.py`](preprocess.py) | [`preprocess_lib/`](preprocess_lib/) | 对已有 `scene.blend` 产出 GT / `objects_in_view` 等 |
| [`filter_scenes.py`](filter_scenes.py) | [`preprocess_lib/`](preprocess_lib/) | 阶段1：`build_inview` 与 `objects_in_view` 相同的镜内语义（空则拒）+ `check_overlap`；无效目录移到 `<scenes-root>/invalid/` |

**依赖与约定**

- 路径：`INFINIGEN_SUBMODULE_ROOT`、`SCENES_*` 见 [`../configs/data_paths.py`](../configs/data_paths.py)；房型 canonical 见 [`../configs/constant.py`](../configs/constant.py)（`ROOM_TYPES`）。新场景目录名一般为 `{canonical}_s{seed}`。
- **如何运行**（参数、tmux、manifests、`core.py` 细节）：[**`../[INTERNAL]QUICKSTART/generate.md`**](../[INTERNAL]QUICKSTART/generate.md)。
- **生成侧模块关系**（`core` / `build_metadata` 等）：[`generate_lib/README.md`](generate_lib/README.md)。
- `metadata.json` 字段约定：[`../docs/METADATA_SCHEMA.md`](../docs/METADATA_SCHEMA.md)。
- 单房 box 局限：[`../docs/LIMITATION.md`](../docs/LIMITATION.md)。
- **`filter_scenes.py`**：先 instance 图判定，再调用 `check_overlap`（与 `preprocess` 相同思路：主进程不加载 `bpy`，重叠计算在 Blender Python 子进程里执行）。

