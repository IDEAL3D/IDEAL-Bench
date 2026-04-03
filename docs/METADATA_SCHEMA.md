# Scene `metadata.json` schema

Machine-readable sidecar next to `scene.blend`, `color.png`, and `instance_segmentation.png`. **No prose or locale-specific text** inside the JSON; this document defines meanings.

## Top-level keys

| Key | Type | Description |
|-----|------|-------------|
| `scene_id` | string | Directory name, `{roomtype}_s{seed}` (hyphen-free room type prefix). |
| `room_type` | string | Canonical id from `configs/constant.py` `ROOM_TYPES` (e.g. `classroom`, `livingroom`; compound words without underscore for stable `scene_id` parsing). |
| `render` | object | Camera / image parameters (see below). |
| `instance_map` | object | Instance segmentation metadata (see below). |

## `render`

| Key | Type | Description |
|-----|------|-------------|
| `resolution` | `[width, height]` | Render resolution in pixels. |
| `camera_location` | `[x, y, z]` | World-space translation of the active camera (metres). |
| `camera_rotation_euler` | `[rx, ry, rz]` | Euler angles in **radians**, order given by `rotation_order`. |
| `rotation_unit` | string | Always `"radians"`. |
| `rotation_order` | string | Always `"XYZ"` (matches `camera_rotation_euler`). |
| `focal_length_mm` | number | Camera focal length in millimetres. |
| `sensor_width` | number | Sensor width in millimetres. |

If `render` was recovered from `camview*.npz` fallback, focal length may assume a 36 mm full-frame sensor; see generation logs.

## `instance_map`

| Key | Type | Description |
|-----|------|-------------|
| `format` | string | Always `grayscale_uint16`. |
| `bits_per_channel` | number | Always `16`. |
| `background_value` | number | Always `0` (background / no hit). |
| `object_index_map` | object | Map from **decimal string** of Blender **Pass Index** → human-readable label. |

### `object_index_map` keys and values

- **Keys:** Decimal string of the Cycles **Object Index** (Pass Index), assigned before the GT pass by Infinigen `set_pass_indices()`, same integer as in `Objects*.json`’s `object_index` field.
- **Values:** Synthetic labels derived from Blender object names: non-alphanumeric characters replaced with `_`, lowercased; duplicate bases get `_001`, `_002`, … in deterministic order (sorted by object name).

### `instance_segmentation.png`

- Single-channel **16-bit grayscale** PNG; pixel value equals the Pass Index for that pixel (range `0`–`65535`).
- Keys in `object_index_map` match these values; use a reader that preserves 16-bit samples (e.g. `cv2.imread(..., cv2.IMREAD_UNCHANGED)`, `imageio`/`imageio.v3`, or Pillow `I;16`). Do not read as 8-bit default or indices may collide modulo 256.
