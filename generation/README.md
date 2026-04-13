# `generation/`：生成与镜内筛选

**阶段关系**：先 **`filter_scenes.py`（阶段 1）**，再 **`inview.py`（阶段 2）**。二者都通过 **`--scenes-root`** 指向批处理根目录；发现规则见 **`preprocess_lib/scene_discovery`**（扁平一层或「房型/分组」下再一层场景目录，见「目录约定」）。

| 入口 | 库 | 作用 |
|------|-----|------|
| [`generate.py`](generate.py) | [`generate_lib/`](generate_lib/) | Infinigen **矩形单房间**：合成 + 渲染，收尾四文件 |
| [`filter_scenes.py`](filter_scenes.py) | [`preprocess_lib/`](preprocess_lib/) | **阶段 1**：镜内语义（`build_instance_seg_view_payload`）+空镜内则拒 + `check_overlap`；无效场景移到 `<scenes-root>/invalid/`；写 `filter_scenes_report.csv` / `filter_scenes_batches.jsonl` |
| [`inview.py`](inview.py) | [`preprocess_lib/`](preprocess_lib/) | **阶段 2**：可见性 / 长宽比 / 截断（F3 为 **`python -m preprocess_lib.truncation_filter`** 子进程）后重写 **`objects_in_view.json`**；**不搬目录**；在 `<scenes-root>` 追加 **`inview_batch.jsonl`**（阈值见 [`preprocess_lib/defaults.py`](preprocess_lib/defaults.py)） |

**目录约定（阶段 1 / 2 一致）**

- **推荐**：每个场景目录含 **`scene.blend`**、**`GT.json`**、**`metadata.json`**（含 `instance_map.object_index_map`）、**`instance_segmentation.png`**。
- **布局（自动二选一，同一 `scenes-root`）**：先试 **`<scenes-root>/<scene_id>/scene.blend`**；若根下没有这种子目录，再试 **`<scenes-root>/<房型或分组>/<scene_id>/scene.blend`**（多一层）；若仍无，再试 **`<scenes-root>/scene.blend`**。CSV / JSONL / `invalid/` 仍写在 **`scenes-root`** 下。
- **`--scenes-root` 可为任意路径**。更深层级（如三层）不会继续递归；可将 **`--scenes-root`** 指到中间那一层，或整理目录。

**依赖与约定**

- 路径：`INFINIGEN_SUBMODULE_ROOT`、`SCENES_*` 见 [`../configs/data_paths.py`](../configs/data_paths.py)；房型 canonical 见 [`../configs/constant.py`](../configs/constant.py)（`ROOM_TYPES`）。新场景目录名一般为 `{canonical}_s{seed}`。
- **如何运行**（参数、tmux、manifests、`generate_lib/core.py`）：[**`../[INTERNAL]QUICKSTART/generate.md`**](../[INTERNAL]QUICKSTART/generate.md)；镜内两阶段说明：[**`../[INTERNAL]QUICKSTART/preprocess.md`**](../[INTERNAL]QUICKSTART/preprocess.md)。
- **生成侧模块关系**（`core` / `build_metadata` 等）：[`generate_lib/README.md`](generate_lib/README.md)。
- `metadata.json` 字段约定：[`../docs/METADATA_SCHEMA.md`](../docs/METADATA_SCHEMA.md)。
- 单房 box 局限：[`../docs/LIMITATION.md`](../docs/LIMITATION.md)。
- **`preprocess_lib`**：`build_inview`（镜内列表与 F1/F2）、`check_overlap`、`truncation_filter`（F3）、旧版 **`extract_inview.write_info_json_v2`**（Blender 内写 `objects_in_view` 并带家具重叠摘要，与 `inview.py` 写出字段可能不完全相同，见 `docs/PREPROCESS_DESIGN.md`）。

