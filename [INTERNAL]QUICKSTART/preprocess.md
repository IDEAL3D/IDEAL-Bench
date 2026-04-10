# 场景预处理（`generation/preprocess.py`）

矩形单房间 Box 管线的预处理入口：由 `preprocess_lib` 写出 `GT.json`、`objects_in_view.json`（顶层含家具重叠 `pair_count` / 体积摘要）。路径约定与生成侧 **`data/scenes_raw`** 及 sibling **`INFINIGEN/`**（`SCENES2TEST`、`SCENES_PROCESSED` 等）一致；详细几何与类别约定见仓库 `docs/`（如 `geometric_rules_floor.md`）。

## 快速命令

在 **`IDEAL/generation`** 下执行（与 `generate.py` 同级）：

```bash
cd /path/to/IDEAL/generation
```

**单场景（指定 blend）：**

```bash
python preprocess.py --blend /path/to/scene_folder/scene.blend
```

默认会写出 `GT.json` 与 `objects_in_view.json`（含家具重叠摘要）。

**开发批处理（非 `--final`）：**

- 默认将 **`IDEAL/data/scenes_raw`** 下含 `scene.blend` 的顶层场景目录同步到 **`INFINIGEN/SCENES2TEST`**，再在 `SCENES2TEST` 下批处理各 `<scene_id>/scene.blend`。
- 若不想同步：加 **`--no-sync-raw`**，直接处理 `--scenes-root` 下已有数据。
- 非 `--final` 模式**不会**写入 `SCENES_PROCESSED` 与 preprocess manifests。

```bash
# 默认：同步 data/scenes_raw → SCENES2TEST 后批处理
python preprocess.py

# 指定批处理根（各子目录内 scene.blend）；指定后默认不再从 RAW 同步
python preprocess.py --scenes-root /path/to/parent_of_scene_dirs
```

**正式管线（`--final`）：**

- 数据源：**`data/scenes_raw` → `INFINIGEN/SCENES_PROCESSED`**，并维护 **`SCENES_PROCESSED/manifests/`**。
- 批处理结束后，家具重叠场景会移至 **`rejected_furniture_overlap/`**，并写入 manifests 侧重叠日志与 **`overlap_scenes.csv`**。
- **必须**配合 **`--room-type`**：
  - `root`：RAW 根目录下**平铺**的十种房型 `scene_id`（前缀 + `_s<seed>`），不包含 `data/scenes_raw/bedroom` 等子目录内的场景。
  - 否则为具体房型名（与 `configs.constant` 中列表一致），处理 **`data/scenes_raw/<房型>/`** 下一层子目录。

```bash
python preprocess.py --final --room-type root
python preprocess.py --final --room-type bedroom
```

仅在非 `root` 时，可用 **`--raw-batch-id`**（可重复）与 RAW manifests 里对应房型的 `*_scenes.csv` 中的 `batch_id` 求交过滤；与 `--room-type root` **互斥**。

## 与 `generate.py` 的关系

生成入口为 `generation/generate.py`，场景落盘在 `data/scenes_raw`；预处理将 RAW（或 `SCENES2TEST` 等批处理根）中的场景转为评测/推理所需的 `GT.json` 与镜内资产 JSON，**不由 `generate.py` 直接写出**。参见 [场景生成 Quickstart](./generate.md) 中的「与预处理的关系」。

## 常用参数（速查）

| 参数 | 说明 |
|------|------|
| `--blend` | 单个 `scene.blend` 路径 |
| `--scenes-root` | 非 `--final`：批处理父目录（子目录为 `<scene_id>` 且含 `scene.blend`）；省略则默认 `SCENES2TEST` |
| `--no-sync-raw` | 非 `--final`：跳过 `data/scenes_raw` → `SCENES2TEST` 复制 |
| `--sync-raw-overwrite` | 非 `--final`：同步时若目标已有同名场景目录则先删除再复制 |
| `--no-semantics` | 只写 `GT.json`，不写 `objects_in_view.json`（不跑实例分割视域与重叠） |
| `--final` / `--formal` | 正式管线：`data/scenes_raw` → `SCENES_PROCESSED` + manifests |
| `--room-type` | 仅 `--final`：`root` 或具体房型 |
| `--raw-batch-id` | 仅 `--final` 且房型非 `root`：按 RAW batch 过滤 |
| `--no-copy` | 仅 `--final`：跳过从 RAW 拷贝（假定已在 `SCENES_PROCESSED`） |
| `--overwrite-processed` | 仅 `--final`：拷贝时覆盖已存在的处理目录 |
| `--force` / `--non-skip` | 已有 `GT.json` 仍重跑 |
| `--log-file` / `--no-log` | 摘要日志路径或关闭 |

单场景与子进程细节见源码 `preprocess_lib/pipeline.py` 与 `preprocess_lib/core.py`。
