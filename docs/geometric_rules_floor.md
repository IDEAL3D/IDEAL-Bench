# Geometric Rules

> **Infinigen MYSCRIPTS exports:** `GT.json` and `info.json` each include **`scene_id`** (the scene folder name, e.g. `kitchen_medium_s1759680675`). There is **no** `schema_version`, `coordinate_frame`, or `geometric_rules_ref` inside the JSON; conventions live in this file and `INFINIGEN/MYSCRIPTS/DOCS/PREPROCESS_DESIGN.md`.

## Infinigen `GT.json`

- **`scene_id`:** Same string as the parent directory of `scene.blend` / `GT.json`.
- **World frame:** Blender world, Z-up right-handed, **X right / Y forward / Z up** (same as below).
- **`scene.objects[].position`:** Root object **origin** in world space (`matrix_world.translation`), **not** bounding-box bottom-center. Infinigen places origins per asset type; do not assume “on floor” from `position.z` alone.
- **`scene.objects[].dimensions`:** Merged evaluated mesh geometry in the **asset root’s local frame**: spans along root local X, Y, Z. This avoids world-axis AABB inflation when the asset is yawed (e.g. 45° on the floor).
- **`scene.objects[].rotation_yaw`:** Either integer degrees [0, 359] (semantic forward vs +Y; see `extract_gt.semantic_yaw_degrees`), or JSON **`null`** for categories where yaw is undefined (e.g. `*_lamp`, `lamp`, and names with the `_plant_` token such as `large_plant_container`). Downstream metrics should skip yaw for `null`.
- **`scene.room.size`:** `[W, D, H]` from floor/ceiling room logic in **world** space (see `INFINIGEN/MYSCRIPTS/DOCS/PREPROCESS_DESIGN.md`).
- **Further limits:** `INFINIGEN/MYSCRIPTS/DOCS/LIMITATION.md`.

## Infinigen `info.json`

- **`scene_id`:** Same as in `GT.json` for that scene.
- **Extrinsics:** `world_to_opencv_camera` — 3×4 `world_to_camera_rt` maps Blender world XYZ to OpenCV camera XYZ before applying `K` (`x_right_y_down_z_forward`; see `infinigen.core.util.camera`).
- **`intrinsic.K` / `extrinsic.world_to_camera_rt`:** Pinhole model.
- **`categories_in_view`:** Categories of assets intersecting the render camera frustum (subset of the scene).
- **`asset_count_in_view`:** Number of asset root objects in that frustum (same filter as `GT.json` / `collect_assets`: excludes doors, windows, walls, floor, ceiling, skirting, etc.).
- **`furniture_overlap`:** Optional; broad-phase uses **world-space** AABB; details in `INFINIGEN/MYSCRIPTS/DOCS/PREPROCESS_DESIGN.md`.
- **Rendering:** Single render camera per scene image; ideal pinhole, **no** lens distortion modeled in JSON.

---

## Coordinate System
- Z-up right-hand: X = right, Y = forward, Z = up
- Origin: floor geometric center, Z = 0 is ground level

## Object Position
- Anchor: bottom-center of bounding box
- Unit: meters (m), precision 0.01m
- Format: [x, y, z], z = 0 means object rests on floor

## Object Size
- Format: [W, D, H] = [X-axis, Y-axis, Z-axis]
- Unit: meters (m), precision 0.01m

## Object Rotation
- Yaw only (rotation around Z-axis)
- Unit: degrees (integer), range [0, 359]
- Direction: counter-clockwise is positive
- Zero point: yaw = 0 means object faces +Y direction