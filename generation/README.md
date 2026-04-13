# Generation and preprocessing (Box single-room)

This folder holds **scene synthesis** (`generate.py`, `generate_lib/`) and the **two-stage filter pipeline** for already-rendered scene directories.

## Stage 1 — `filter_scenes.py`

Scene-level gate: in-view semantics (instance map + `GT.json` alignment) and furniture overlap via `python -m preprocess_lib.check_overlap`. Failed scenes move to `<scenes-root>/invalid/`. Reports append under `scenes-root` (see `preprocess_lib/defaults.py` for CSV / JSONL names).

```bash
cd /path/to/IDEAL/generation
python filter_scenes.py --scenes-root /path/to/scenes_parent [--workers N] [--gpu ...]
```

## Stage 2 — `filter_inview.py`

Object-level filters (F1 visibility, F2 aspect ratio, F3 truncation in a `bpy` subprocess) rewrite **`objects_in_view.json`** per scene. **`inview_batch.jsonl`** is appended under **`--scenes-root`** (per-scene lines only when something notable happened; run ends with a `batch_summary` record).

```bash
python filter_inview.py --scenes-root /path/to/scenes_parent [--workers N] [--skip] [--gpu ...]
```

- **`--skip`**: skip scenes that already have `objects_in_view.json`.
- **`--workers`**: parallel scenes (thread pool). Each scene still spawns its own F3 `bpy` process; many workers on one GPU can exhaust VRAM — start with **4** and raise (e.g. **8**) only if stable.

## Scene layout

`--scenes-root` is the parent of scene folders containing `scene.blend`. Discovery order: `<root>/<scene_id>/scene.blend`, then `<root>/<group>/<scene_id>/scene.blend`, then `<root>/scene.blend`. See **`preprocess_lib/scene_discovery.py`**.

Thresholds and filenames: **`preprocess_lib/defaults.py`**.

## Further reading

- Internal (Chinese): `[INTERNAL]QUICKSTART/preprocess.md`
- Design notes: `docs/PREPROCESS_DESIGN.md`
