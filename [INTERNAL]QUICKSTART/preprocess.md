# 场景真值与镜内 JSON（原 `generation/preprocess.py` 已移除）

矩形单房间 Box 管线的 **`GT.json`** 由生成收尾或 **`generate_lib/extract_gt.py`** 写出；**`objects_in_view.json`** 可由 **`inview.py`（推荐，两阶段管线第 2 步）** 或 Blender 内的 **`preprocess_lib/extract_inview.write_info_json_v2`**（含家具重叠摘要，字段可能与 `inview.py` 不完全一致）写出。几何约定见 `docs/geometric_rules_floor.md`、`docs/PREPROCESS_DESIGN.md`。

## 推荐流程（两阶段）

1. **合成**：在 **`IDEAL/generation`** 下运行 **`generate.py`**（默认写入 `data/scenes_raw/<房型或 flat>/...`）。

2. **阶段 1 — 场景级筛选**：镜内语义 + 家具重叠；不合格整目录移到 `<scenes-root>/invalid/`。

```bash
cd /path/to/IDEAL/generation
python filter_scenes.py --scenes-root /path/to/scenes_parent [--workers N] [--gpu ...]
```

3. **阶段 2 — 镜内物体级过滤**：在**已通过阶段 1** 的同一批目录上运行 **`inview.py`**，按可见像素、实例图长宽比、截断（Blender 子进程）重写 **`objects_in_view.json`**，并在 `<scenes-root>` 追加 **`inview_batch.jsonl`**（仅记录有过滤/报错等「有信息」的场景 + 末尾 batch汇总）。

```bash
cd /path/to/IDEAL/generation
python inview.py --scenes-root /path/to/scenes_parent [--workers N] [--skip-if-exists] [--gpu ...]
```

- **阈值**：`ASPECT_RATIO_MAX`、`TRUNCATION_RATIO_MIN` 等在 **`preprocess_lib/defaults.py`**。
- **`--skip-if-exists`**：若某场景目录已有 **`objects_in_view.json`** 则跳过（用于断点续跑）。

## 目录要建在哪里？

- **`--scenes-root`** 指向**你自己选的批处理根目录**即可（自定义路径完全支持），只要其下是一层 **`<scene_id>/scene.blend`**，且同目录有所需 **`GT.json`**、**`metadata.json`**、**`instance_segmentation.png`**。
- 常见做法：对 **`data/scenes_raw/<room_folder>`** 或 **`INFINIGEN/.../SCENES_FILTERED/<room_folder>`** 执行上述两步（路径以你本机为准）。
- **注意**：**`inview.py`** 当前只枚举「子目录 + `scene.blend`」，**没有**「`scenes-root/scene.blend` 单文件」回退；若只有单个 blend，请放在 **`some_root/my_scene_id/scene.blend`**。

## 历史说明

旧版统一预处理 CLI（**`preprocess.py`** 等）及 **`--final`** / RAW→`SCENES_PROCESSED` 批处理已删除；若需 manifests批处理，请自行维护或从 Git 历史恢复脚本。
