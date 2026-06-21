# Generation and preprocessing (box single-room)

This directory contains **scene synthesis** (`generate.py`, `generate_lib/`) and the **two-stage filter pipeline** for rendered scene bundles.

Canonical path cheat sheet lives in **[`README.md` — Generation and preprocessing (paths)](../README.md#generation-and-preprocessing-paths)** (`data/scenes_raw/`, manifests vs. benchmark tree `data/scenes/`).

## Synthesis — `generate.py`

Batch synthesis **writes** under **`IDEAL/data/scenes_raw/`** by default (`configs.data_paths.SCENES_RAW_ROOT`), and maintains **`IDEAL/data/scenes_raw/manifests/`** (`generation_batches.jsonl`, per-room `*_scenes.csv`). For alternate roots or low-level `--task` control, invoke **`generate_lib/core.py`** and pass **`--scenes-root`** / **`--output_folder`** where applicable.

Canonical room identifiers for **`--room-type`** / **`--room-types`** are defined in **`configs.constants.ROOM_TYPES`**:

- **Living-space:** `bedroom`, `homestudio`, `livingroom`, `kitchen`, `bathroom`, `diningroom`
- **Working-space:** `office`, `classroom`, `meetingroom`, `library`

Examples (run from **`IDEAL/generation/`**):

```bash
python generate.py --room-type bedroom --num-scenes 1
python generate.py --room-types bedroom livingroom kitchen --num-scenes 1
python generate.py --room-type office --num-scenes 2
```

## Stage 1 — `filter_scenes.py`

Directory-level gate before intensive in-view preprocessing: verifies **instance map + metadata + GT** alignment (`build_instance_seg_view_payload`), rejects **empty** in-view semantics, then (unless **`--dry-run`** without **`--check-overlap`**) runs furniture overlap checks via **`python -m preprocess_lib.check_overlap`** in a Blender subprocess.

- **`--scenes-root`:** optional; resolves at runtime to **`<IDEAL_repo>/data/scenes_raw`** when omitted (`preprocess_lib.defaults.resolve_default_scenes_root`).
- **Rejected directories** relocate to **`<scenes-root>/invalid/<scene_id>/`**. Batch append-only artefacts under **`scenes-root`** are named in **`preprocess_lib.defaults`** (`filter_scenes_report.csv`, `filter_scenes_batches.jsonl`).

Discovery matches **`preprocess_lib/scene_discovery.py`**: **`invalid/`** is never scanned at batch root depth.

```bash
cd IDEAL/generation
python filter_scenes.py [--scenes-root /path/to/batch_parent] [--dry-run] [--check-overlap] [--workers N] [--gpu …]
```

**No discoverable scenes:** exit code **`2`**, stderr message from **`describe_no_valid_scenes_error`** (missing path, empty tree, mismatched layout, or only payloads under **`invalid/`**); hints to bootstrap with **`IDEAL/generation/generate.py`** when starting from an empty **`scenes_raw`**.

## Stage 2 — `filter_inview.py`

Object-centric filters (**F1** visibility, **F2** aspect ratio, **F3** truncation via a **`bpy`** subprocess) (re-)write **`objects_in_view.json`** per scene.

**Bypass:** prefixes listed in **`NO_FILTER_ROOM_TYPES`** inside **`filter_inview.py`** skip F1–F3 and use **`build_inview_no_filter`** (currently **only** **`classroom`**; **`library`**, **`office`**, **`meetingroom`** still receive the three-filter pipeline).

- **`--scenes-root`:** same default and discovery contract as Stage 1.
- **`inview_batch.jsonl`** appends under **`scenes-root`** with per-scene records when filters fire or diagnostics matter; each run terminates with a **`batch_summary`** line.
- Directories parked under **`invalid/`** remain invisible to discovery—invoke Stage 2 only against accepted exports still nested under **`scenes-root`**.

```bash
python filter_inview.py [--scenes-root /path/to/batch_parent] [--workers N] [--skip] [--gpu …]
```

- **`--skip`:** omit scenes whose **`objects_in_view.json`** already exists (default behaviour rewrites the file).
- **`--workers`:** thread pool parallelism; each scene launches its own F3 subprocess—increase gradually to avoid **`bpy`/VRAM** starvation.

Same **no-scenes** behaviour as Stage **1**: exit **`2`** with the shared **`describe_no_valid_scenes_error`** text if discovery returns zero **`scene.blend`** paths.

### Recommended CLI order

1. **`generate.py`** → populate **`data/scenes_raw`** (manifests optional bookkeeping).
2. **`filter_scenes.py`** → quarantine defective rooms under **`invalid/`**.
3. **`filter_inview.py`** → emit **`objects_in_view.json`** for retained scenes only.

**Why this order:** Stage 1 rejects overlap and broken instance–GT chains before Blender-heavy F3 runs in Stage 2, and **`filter_scenes`** does **not** read **`objects_in_view.json`**—it rebuilds the stage-1 payload from renders and **`GT.json`**. Running **Stage 2 first** is usually mechanically fine (only Stage 1 moves directories into **`invalid/`**), but wastes work on batches Stage 1 will later quarantine, and any downstream policy that assumes overlap is already cleared before trusting **`objects_in_view.json`** breaks if Stage 1 is skipped or routinely run after Stage 2.

### Scene layout under `--scenes-root`

Resolution order (first match wins):

1. `<root>/<scene_id>/scene.blend`
2. `<root>/<group>/<scene_id>/scene.blend` (exactly **one** extra grouping level)
3. `<root>/scene.blend` (singleton batch root)

Artifact naming, thresholds, and log basenames live in **`preprocess_lib/defaults.py`**.
