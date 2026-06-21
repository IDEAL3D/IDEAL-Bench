#!/usr/bin/env python
"""
Backend for rectangular single-room scenes: runs ``infinigen_examples.single`` and, by default, keeps
blend, RGB beauty pass, uint16 instance map, ``metadata.json``, and ``GT.json`` per scene.
Room types are canonical ids in ``configs.constants.ROOM_TYPES``; default output layout is
``data/scenes_raw/{type}_s{seed}/`` (override with ``--scenes-root`` / ``--output_folder``).
For batch runs that update RAW manifests under ``data/scenes_raw/manifests/``, prefer ``generation/generate.py``.

Examples (from ``IDEAL/generation``)::

    python generate_lib/core.py
    python generate_lib/core.py --room-type livingroom --num-scenes 3 --room-size large
    python generate_lib/core.py --start-seed 42 --sample 256 --task coarse render

Full CLI: ``python generate_lib/core.py --help``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
from imageio import imwrite

from configs.constants import (
    LIVING_SPACE_ROOM_TYPES,
    ROOM_TYPES,
    WORKING_SPACE_ROOM_TYPES,
    infinigen_room_type,
)
from configs.data_paths import INFINIGEN_SUBMODULE_ROOT, SCENES_RAW_ROOT
from configs.gpu_select import resolve_cuda_visible_devices

SCRIPT_DIR = Path(__file__).resolve().parent
_GEN_LIB_DIR = SCRIPT_DIR
if str(_GEN_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_LIB_DIR))
import build_metadata  # noqa: E402

# Default scene parent: IDEAL/data/scenes_raw (``configs.data_paths.SCENES_RAW_ROOT``).

logging.basicConfig(
    format="[%(asctime)s.%(msecs)03d] [%(module)s] [%(levelname)s] | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Match generate.py: fixed wall thickness; floor slab extrusion ~half (BlueprintSolidifier SOLIDIFY).
# None skips gin injection here and may diverge from generate.py.
FIXED_WALL_THICKNESS_M: float | None = 0.25

# Interior wall height: inject ``RoomConstants.global_params.wall_height`` uniform by area tier.
# Omitted tiers keep gin default ("uniform", 2.8, 3.2).
WALL_HEIGHT_UNIFORM_BY_TIER: dict[str, tuple[float, float]] = {
    "small": (2.8, 3.2),
    "medium": (3.0, 4.0),
    "large": (4.0, 6.0),
    # xlarge and homestudio studio tier align with large unless specified elsewhere
    "xlarge": (4.0, 6.0),
    "studio": (4.0, 6.0),
}

# ---------------------------------------------------------------------------
# Footprint bounds (min_w, max_w, min_d, max_d) meters
# ---------------------------------------------------------------------------
# Fallback for room types without a ROOM_SIZE_TIERS entry
_DEFAULT_SIZE_TIERS: dict[str, tuple[float, float, float, float]] = {
    "small":  (2.5, 3.5, 2.5, 3.5),
    "medium": (3.5, 6.0, 3.5, 6.0),
    "large":  (6.0, 8.0, 6.0, 8.0),
}

# Per-room-type footprint tiers (keys are **canonical**, same as ``configs.constants.ROOM_TYPES``).
# Residential living_space (6): kitchen/bath/dining fixed single tier; bedroom/livingroom two tiers 50/50 by seed;
# homestudio is the old large-bedroom / open-studio layout (user selects room type).
ROOM_SIZE_TIERS: dict[str, dict[str, tuple[float, float, float, float]]] = {
    # --- Residential living_space ---
    "bedroom": {
        "small":  (3.0, 3.9, 3.0, 3.9),   # ~<16 m^2 avg — twin/single bed layout
        "medium": (4.0, 5.5, 4.0, 5.5),   # ~16-36 m^2, double bed
    },
    "homestudio": {
        "studio": (5.0, 7.0, 5.0, 7.0),   # Former large bedroom / home studio
    },
    "livingroom": {
        "small":  (3.0, 5.0, 3.0, 5.0),
        "medium": (4.0, 8.0, 4.0, 8.0),
    },
    "kitchen": {
        "medium": (3.2, 5.0, 3.2, 5.0),
    },
    "bathroom": {
        "medium": (2.5, 3.2, 2.5, 3.2),
    },
    "diningroom": {
        "medium": (3.5, 5.5, 3.5, 5.5),
    },
    # --- Working-space canonical types (office / meetingroom / …) ---
    "office": {
        "small":  (3.0, 4.0, 3.0, 4.0),        # ~9-16 m^2 single office
        "medium": (4.0, 6.0, 4.0, 6.0),        # ~16-36 m^2 multi-desk
        "large":  (6.0, 8.0, 6.0, 8.0),        # ~36-64 m^2 open office
    },
    "meetingroom": {
        "small":  (3.5, 5.0, 3.0, 4.5),        # ~10-22 m^2
        "medium": (5.0, 7.0, 4.0, 5.5),        # ~20-38 m^2
        "large":  (7.0, 10.0, 5.5, 7.0),       # ~38-70 m^2
    },
    "classroom": {
        "small":  (6.0, 6.5,  8.0, 8.5),       # ~3 bays x 6-7 rows
        "medium": (6.0, 7.0,  8.5, 9.5),       # ~3 bays x 7-8 rows
        "large":  (6.5, 7.5,  9.0, 10.0),      # ~3-4 bays x 7-8 rows
    },
    "library": {
        "small":  (7.5, 10.0,  6.0, 9.5),       # Reading room; block counts in working_space._place_library_grid
        "medium": (10.0, 15.0,  9.5, 16.0),     # Medium stack hall
        "large":  (16.0, 30.0, 18.0, 36.0),     # Large hall (merged former large/xlarge)
    },
}

# Union of all tier names across ROOM_SIZE_TIERS
ALL_SIZE_TIER_NAMES: list[str] = sorted(
    set(_DEFAULT_SIZE_TIERS)
    | {t for tiers in ROOM_SIZE_TIERS.values() for t in tiers},
    key=lambda x: list(_DEFAULT_SIZE_TIERS).index(x) if x in _DEFAULT_SIZE_TIERS else 99,
)


def get_available_tiers(room_type: str) -> list[str]:
    """List tier names defined for ``room_type``, or default small/medium/large."""
    room_tiers = ROOM_SIZE_TIERS.get(room_type, {})
    if room_tiers:
        return list(room_tiers)
    return list(_DEFAULT_SIZE_TIERS)


def _is_living_space_room(canonical_room_type: str) -> bool:
    return canonical_room_type in LIVING_SPACE_ROOM_TYPES


def resolve_living_space_tier(canonical_room_type: str, seed: int) -> str:
    """Pick internal size tier for one residential scene (injected via gin; omitted from folder names)."""
    rng = random.Random(seed)
    if canonical_room_type in ("kitchen", "bathroom", "diningroom"):
        return "medium"
    if canonical_room_type == "homestudio":
        return "studio"
    if canonical_room_type in ("bedroom", "livingroom"):
        return rng.choice(["small", "medium"])
    raise ValueError(f"Not a residential living_space room type: {canonical_room_type!r}")


def get_size_for_room(
    room_type: str, tier: str
) -> tuple[float, float, float, float]:
    """Resolve (min_w, max_w, min_d, max_d) for ``room_type`` and ``tier``."""
    avail = get_available_tiers(room_type)
    if tier not in avail:
        raise ValueError(
            f"Room type '{room_type}' has no tier '{tier}'. Valid: {avail}"
        )
    room_tiers = ROOM_SIZE_TIERS.get(room_type, {})
    if tier in room_tiers:
        return room_tiers[tier]
    return _DEFAULT_SIZE_TIERS[tier]


# Canonical room type -> gin config stem / constraint module namespace
ROOM_CONFIG_MAP = {
    # Residential (living_space)
    "bedroom":     "living_space",
    "homestudio":  "living_space",
    "livingroom": "living_space",
    "kitchen":     "living_space",
    "bathroom":    "living_space",
    "diningroom": "living_space",
    # Office / education (working_space)
    "office":  "working_space",
    "meetingroom": "working_space",
    "classroom":    "working_space",
    "library":      "working_space",
}


def _gin_module_for_room(canonical_room_type: str) -> str:
    """Return the constraint module path for the given canonical room type."""
    config_name = ROOM_CONFIG_MAP[canonical_room_type]
    return f"infinigen_examples.constraints.{config_name}"

SIZE_OVERRIDE_GIN_TEMPLATE = "{module}.floor_plan.size_override"
FURNITURE_SIZE_OVERRIDE_GIN_TEMPLATE = "{module}.furniture_constraints.size_override"


def _size_override_gin_value(tup: tuple[float, float, float, float]) -> str:
    """Format ``(w0,w1,d0,d1)`` string for gin size_override."""
    return f"({tup[0]},{tup[1]},{tup[2]},{tup[3]})"


def _tee_pipe_to_buffer_and_stream(pipe, buffer: list[str], stream) -> None:
    """Read text lines from ``pipe``, append to ``buffer``, copy to ``stream`` (live progress)."""
    try:
        assert pipe is not None
        for line in iter(pipe.readline, ""):
            buffer.append(line)
            stream.write(line)
            stream.flush()
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _stderr_stdout_only_unfreed_blocks_noise(stderr: str | None, stdout: str | None) -> bool:
    """True if subprocess output is only shutdown leak-detector 'Not freed memory blocks' noise."""
    text = f"{stdout or ''}\n{stderr or ''}"
    if "Not freed memory blocks" not in text:
        return False
    fatal = (
        "Traceback (most recent call last)",
        "System is out of GPU memory",
        "CUDA error",
        "Fatal Python error",
        "Segmentation fault",
    )
    if any(s in text for s in fatal):
        return False
    err_lines = re.findall(r"(?m)^Error:\s*(.+)$", text)
    if not err_lines:
        return False
    return all(
        line.strip().startswith("Not freed memory blocks") for line in err_lines
    )


def _run(cmd: list[str], gpu: str | None = None) -> None:
    """Run ``cmd`` as a subprocess with cwd at ``INFINIGEN_SUBMODULE_ROOT``.

    Stream stdout/stderr live (``coarse`` + ``render`` can take many minutes). After exit,
    optionally treat bpy host shutdown ``Not freed memory blocks`` exit code 1 as success.
    """
    logger.info("[CMD] %s", " ".join(cmd))
    env = None
    if gpu is not None:
        # Release: drop CUDA_VISIBLE_DEVICES injection; use host env only (see configs.gpu_select).
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = gpu
        logger.info("CUDA_VISIBLE_DEVICES=%s", gpu)
    proc = subprocess.Popen(
        cmd,
        cwd=str(INFINIGEN_SUBMODULE_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    out_buf: list[str] = []
    err_buf: list[str] = []
    assert proc.stdout is not None and proc.stderr is not None
    t_out = threading.Thread(
        target=_tee_pipe_to_buffer_and_stream,
        args=(proc.stdout, out_buf, sys.stdout),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_tee_pipe_to_buffer_and_stream,
        args=(proc.stderr, err_buf, sys.stderr),
        daemon=True,
    )
    t_out.start()
    t_err.start()
    rc = proc.wait()
    t_out.join()
    t_err.join()
    stdout_text = "".join(out_buf)
    stderr_text = "".join(err_buf)
    if rc == 0:
        return
    if rc == 1 and _stderr_stdout_only_unfreed_blocks_noise(stderr_text, stdout_text):
        logger.warning(
            "Ignored subprocess exit code 1: only 'Not freed memory blocks' at shutdown "
            "(benign leak-detector noise from the bpy host process)."
        )
        return
    _tail = 200
    _stderr_lines = stderr_text.strip().splitlines()
    _stdout_lines = stdout_text.strip().splitlines()
    if _stderr_lines:
        _stderr_tail = "\n".join(_stderr_lines[-_tail:])
        logger.error("Subprocess stderr (last %s lines):\n%s", _tail, _stderr_tail)
        print(f"--- Subprocess stderr (last {_tail} lines) ---\n{_stderr_tail}", file=sys.stderr)
    if _stdout_lines:
        _stdout_tail = "\n".join(_stdout_lines[-_tail:])
        logger.error("Subprocess stdout (last %s lines):\n%s", _tail, _stdout_tail)
        print(f"--- Subprocess stdout (last {_tail} lines) ---\n{_stdout_tail}", file=sys.stderr)
    raise subprocess.CalledProcessError(rc, cmd, stdout_text, stderr_text)


def _write_gt_json_via_bpy(output_folder: Path) -> None:
    """Write ``GT.json`` beside ``scene.blend`` using in-process ``bpy``.

    Reuses or ``open_mainfile``-loads the scene, then ``extract_gt.resolve_room_metrics_box`` +
    ``write_gt_json``. Requires a Python that can import ``bpy``.
    """
    output_folder = Path(output_folder).resolve()
    blend_path = output_folder / "scene.blend"
    resolved = os.path.normpath(str(blend_path.resolve()))

    try:
        import bpy  # type: ignore[import-not-found]  # noqa: WPS433
    except ImportError as e:
        raise RuntimeError(
            "GT.json export requires bpy (use the same conda env / interpreter you use for generation "
            "with bpy)."
        ) from e

    try:
        cur = bpy.data.filepath
        cur_abs = os.path.normpath(os.path.abspath(cur)) if cur else ""
        if not cur_abs or cur_abs != resolved:
            bpy.ops.wm.open_mainfile(filepath=resolved)
        import extract_gt  # noqa: E402

        metrics = extract_gt.resolve_room_metrics_box()
        if metrics is None:
            raise RuntimeError("resolve_room_metrics_box returned None")
        extract_gt.write_gt_json(str(output_folder), metrics)
        logger.info("GT.json: in-process bpy")
    except Exception as e:
        raise RuntimeError(f"GT.json in-process bpy export failed for {output_folder}") from e


FINAL_KEEP_NAMES = frozenset(
    {
        "scene.blend",
        "color.png",
        "instance_segmentation.png",
        "metadata.json",
        "GT.json",
    }
)


def _build_gt_object_index_command(
    seed: int,
    input_folder: Path,
    gt_output_folder: Path,
    args,
) -> list[str]:
    """Second pass: flat Object Index render into ``gt_output_folder`` (avoids clobbering color.png)."""
    w, h = (640, 360) if args.render_width == 640 else (1280, 720)
    cmd: list[str] = [
        sys.executable,
        "-m",
        "infinigen_examples.single",
        "--seed",
        f"s{seed}",
        "--task",
        "render",
        "--input_folder",
        str(input_folder),
        "--output_folder",
        str(gt_output_folder),
        "--configs",
        *args.configs,
        "--overrides",
        "render.render_image_func=@flat/render_image",
        "flat/render_image.override_num_samples=16",
        "flat/render_image.skip_reorganize_frames=True",
        f"execute_tasks.generate_resolution=({w}, {h})",
        f"get_sensor_coords.W={w}",
        f"get_sensor_coords.H={h}",
    ]
    # Do not merge user --overrides here: must keep render.render_image_func=flat
    if args.task_uniqname is not None:
        cmd += ["--task_uniqname", args.task_uniqname]
    if args.debug:
        cmd += ["--debug", *args.debug]
    return cmd


def _rename_rgb_to_color_png(folder: Path) -> Path:
    """Rename first ``Image_*.png`` from the primary render to ``color.png``."""
    images = sorted(folder.glob("Image_*.png"))
    if not images:
        raise FileNotFoundError(f"No Image_*.png in {folder}")
    src = images[0]
    dst = folder / "color.png"
    if src.resolve() != dst.resolve():
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    return dst


def _ensure_color_png(folder: Path) -> Path:
    """Ensure ``color.png`` exists: prefer existing file, else rename ``Image_*.png`` or ``image.png``."""
    folder = Path(folder)
    dst = folder / "color.png"
    if dst.is_file():
        return dst
    alt = folder / "image.png"
    if alt.is_file():
        alt.rename(dst)
        logger.info("Renamed image.png -> color.png: %s", folder)
        return dst
    return _rename_rgb_to_color_png(folder)


def _ensure_mask_tag_json_for_rerender(input_folder: Path) -> None:
    """ensure MaskTag.json exists for render-only reruns (execute_tasks loads tags when input != coarse)."""
    p = Path(input_folder) / "MaskTag.json"
    if p.is_file():
        return
    p.write_text("{}", encoding="utf-8")
    logger.info("Wrote empty MaskTag.json (required for render input): %s", p)


def _write_instance_segmentation_png(npy_path: Path, out_png: Path) -> None:
    """Write Object Index pass as uint16 grayscale PNG (avoids uint8 wrap for pass index > 255)."""
    arr = np.load(npy_path)
    if arr.ndim != 2:
        arr = arr.reshape(arr.shape[0], arr.shape[1])
    arr = np.asarray(arr, dtype=np.float64)
    max_v = float(np.nanmax(arr)) if arr.size else 0.0
    if max_v > 65535:
        logger.warning(
            "Object index max %.0f > 65535; clamping to uint16 range",
            max_v,
        )
    u16 = np.ascontiguousarray(np.clip(arr, 0, 65535).astype(np.uint16))
    try:
        imwrite(out_png, u16)
    except Exception as e:
        logger.debug("imageio uint16 PNG write failed; trying PIL mode I: %s", e)
        try:
            from PIL import Image

            Image.fromarray(u16.astype(np.int32), mode="I").save(out_png)
        except Exception as e2:
            raise RuntimeError(
                "Cannot write 16-bit instance_segmentation.png (install imageio PNG support or Pillow)"
            ) from e2


def _finalize_scene_outputs(
    output_folder: Path,
    seed: int,
    canonical_room_type: str,
    args,
    gpu: str | None,
    *,
    clean_to_four: bool = True,
) -> None:
    """After coarse+render: flat Object Index pass, metadata, ``GT.json``, optional prune to five deliverables.

    If ``clean_to_four=False``, keep auxiliary files (useful when repairing legacy folders).
    """
    output_folder = Path(output_folder).resolve()
    blend_path = output_folder / "scene.blend"
    if not blend_path.is_file():
        raise FileNotFoundError(f"Missing scene.blend under {output_folder}")

    _ensure_color_png(output_folder)
    _ensure_mask_tag_json_for_rerender(output_folder)

    gt_dir = output_folder / ".gt_object_index_pass"
    if gt_dir.exists():
        shutil.rmtree(gt_dir, ignore_errors=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    try:
        gt_cmd = _build_gt_object_index_command(seed, output_folder, gt_dir, args)
        _run(gt_cmd, gpu=gpu)
    except subprocess.CalledProcessError as e:
        logger.exception("Object index (flat render) failed")
        raise RuntimeError(
            "instance_segmentation pass failed: ensure Infinigen can load scene.blend; "
            "if it persists, verify assets/ match the coarse run; "
            "an empty MaskTag.json is written when missing."
        ) from e

    seg_npy = sorted(gt_dir.glob("ObjectSegmentation*.npy"))
    if not seg_npy:
        raise FileNotFoundError(f"No ObjectSegmentation*.npy under {gt_dir}")
    objs_json = sorted(gt_dir.glob("Objects*.json"))
    if not objs_json:
        raise FileNotFoundError(f"No Objects*.json under {gt_dir}")

    _write_instance_segmentation_png(seg_npy[0], output_folder / "instance_segmentation.png")

    scene_id = f"{canonical_room_type}_s{seed}"
    metadata = build_metadata.build_metadata(
        scene_id,
        canonical_room_type,
        blend_path,
        objs_json[0],
    )
    (output_folder / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        _write_gt_json_via_bpy(output_folder)
    except Exception as e:
        logger.exception("GT.json export via bpy failed")
        raise RuntimeError(
            "GT.json export failed (in-process bpy only; fix bpy/GT errors above or use a Python "
            "with bpy)."
        ) from e

    if clean_to_four:
        _delete_all_except_final_four(output_folder)
        tmp_dir = output_folder.parent / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _delete_all_except_final_four(output_folder: Path) -> None:
    """Keep only ``FINAL_KEEP_NAMES`` (scene.blend, passes, metadata, GT)."""
    output_folder = Path(output_folder)
    for p in list(output_folder.iterdir()):
        if p.name in FINAL_KEEP_NAMES:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
    for name in FINAL_KEEP_NAMES:
        if not (output_folder / name).is_file():
            logger.warning("Final output missing file: %s", name)
    logger.info("Cleanup done; kept: %s", ", ".join(sorted(FINAL_KEEP_NAMES)))


def _build_single_command(
    seed: int,
    size_name: str,
    canonical_room_type: str,
    output_folder: Path,
    args,
) -> list[str]:
    """Build argv for ``infinigen_examples.single``: sizes from canonical table, gin room_type from Infinigen string."""
    infinigen_rt = infinigen_room_type(canonical_room_type)
    base = [sys.executable, "-m", "infinigen_examples.single"]
    cli_seed = f"s{seed}"

    cmd: list[str] = base + [
        "--seed",
        cli_seed,
        "--task",
        *args.task,
        "--output_folder",
        str(output_folder),
        "--configs",
        *args.configs,
    ]

    size_override = get_size_for_room(canonical_room_type, size_name)
    size_gin = _size_override_gin_value(size_override)
    module = _gin_module_for_room(canonical_room_type)
    size_gin_key = SIZE_OVERRIDE_GIN_TEMPLATE.format(module=module)
    furn_size_gin_key = FURNITURE_SIZE_OVERRIDE_GIN_TEMPLATE.format(module=module)
    default_overrides = [
        "compose_single_room.terrain_enabled=False",
        "compose_single_room.solve_small_enabled=False",
        "full/render_image.passes_to_save=[]",
        "full/render_image.skip_reorganize_frames=True",
        f"configure_render_cycles.num_samples={args.render_samples}",
        "configure_render_cycles.denoise=True",
        f"{size_gin_key}={size_gin}",
        f"{furn_size_gin_key}={size_gin}",
        f"{module}.floor_plan.room_type='{infinigen_rt}'",
        f"compose_single_room.room_type='{infinigen_rt}'",
    ]
    if FIXED_WALL_THICKNESS_M is not None:
        default_overrides.append(
            f"RoomConstants.global_params.wall_thickness={float(FIXED_WALL_THICKNESS_M)}"
        )
    wh = WALL_HEIGHT_UNIFORM_BY_TIER.get(size_name)
    if wh is not None:
        lo, hi = wh
        default_overrides.append(
            f'RoomConstants.global_params.wall_height=("uniform", {lo}, {hi})'
        )
    if args.light_multiplier is not None:
        default_overrides.append(f"compose_single_room.light_energy_multiplier={args.light_multiplier}")
    rw = getattr(args, "render_width", 1280)
    if rw == 640:
        w, h = 640, 360
    else:
        w, h = 1280, 720
    default_overrides.extend(
        [
            f"execute_tasks.generate_resolution=({w}, {h})",
            f"get_sensor_coords.W={w}",
            f"get_sensor_coords.H={h}",
        ]
    )
    overrides = default_overrides + list(args.overrides)
    if overrides:
        cmd += ["--overrides", *overrides]

    if args.input_folder is not None:
        cmd += ["--input_folder", str(args.input_folder)]
    if args.task_uniqname is not None:
        cmd += ["--task_uniqname", args.task_uniqname]
    if args.debug:
        cmd += ["--debug", *args.debug]

    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate rectangular single-room indoor scenes via infinigen_examples.single."
    )
    parser.add_argument(
        "--start-seed",
        type=int,
        default=None,
        help="Starting seed; if omitted, a random 31-bit value; incremented per scene in batch",
    )
    parser.add_argument("--num-scenes", type=int, default=1, help="Number of scenes to generate")
    parser.add_argument(
        "--room-size",
        type=str,
        default=None,
        help=(
            "Size tier for non-residential types only (ignored for residential). "
            "If omitted: working_space types draw small/medium/large 1/3 per scene; other non-residential default medium."
        ),
    )
    parser.add_argument(
        "--room-type",
        type=str,
        default="bedroom",
        choices=list(ROOM_TYPES),
        help="Canonical room type (configs.constants.ROOM_TYPES); folder name {room_type}_s{seed}",
    )
    parser.add_argument(
        "--task",
        nargs="+",
        default=["coarse", "render"],
        choices=["coarse", "populate", "fine_terrain", "ground_truth", "render", "mesh_save", "export"],
        help="Infinigen task pipeline stages (default coarse render)",
    )
    parser.add_argument(
        "--light-multiplier",
        type=float,
        default=None,
        help="Interior light energy multiplier (overrides gin default ~5.0); typical range ~3-10",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=128,
        dest="render_samples",
        metavar="N",
        help="Cycles samples (default 128; base.gin uses 8192); denoising always on",
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help=(
            "CUDA_VISIBLE_DEVICES: GPU index or list (e.g. --gpu 2 or --gpu 2,6). "
            "Use auto or IDEAL_GPU=auto for least-loaded GPU (nvidia-smi). "
            "If omitted: IDEAL_GPU, else inherited CUDA_VISIBLE_DEVICES, else auto (fallback 0)."
        ),
    )
    parser.add_argument(
        "--configs",
        nargs="+",
        default=None,
        help="Gin config names (default chosen from room type: living_space, working_space, …)",
    )
    parser.add_argument(
        "--render-width",
        type=int,
        choices=(1280, 640),
        default=1280,
        metavar="W",
        help="Output resolution: 1280 -> 1280x720 (default), 640 -> 640x360; sets execute_tasks + sensor size",
    )
    parser.add_argument(
        "--overrides",
        nargs="+",
        default=[],
        help="Extra gin overrides, e.g. module.param=value",
    )
    parser.add_argument(
        "--scenes-root",
        type=Path,
        default=None,
        help=f"Parent directory for batch scenes (default {SCENES_RAW_ROOT}); children {{roomtype}}_s{{seed}}/",
    )
    parser.add_argument(
        "--output_folder",
        type=Path,
        default=None,
        help="Output directory for single-scene runs (default under data/scenes_raw)",
    )
    parser.add_argument(
        "--input_folder",
        type=Path,
        default=None,
        help="Input folder for render-only continuation runs",
    )
    parser.add_argument("--task_uniqname", type=str, default=None)
    parser.add_argument("--debug", type=str, nargs="*", default=None, help="Forward --debug flags to Infinigen")
    args = parser.parse_args()

    args.gpu = resolve_cuda_visible_devices(args.gpu)

    canonical = args.room_type

    # Choose gin config from canonical room type -> living_space / working_space
    if args.configs is None:
        args.configs = [ROOM_CONFIG_MAP[canonical]]

    scenes_root = (
        args.scenes_root if args.scenes_root is not None else SCENES_RAW_ROOT
    ).resolve()
    scenes_root.mkdir(parents=True, exist_ok=True)

    if args.start_seed is None:
        args.start_seed = secrets.randbelow(2**31 - 1)
        print(f"[INFO] --start-seed omitted; using {args.start_seed}")

    living = _is_living_space_room(canonical)

    # Per-scene size tier names (resolved via canonical room type)
    if living:
        if args.room_size is not None and args.room_size != "medium":
            logger.warning(
                "Residential room type ignores --room-size=%s (using internal small/medium or fixed tiers).",
                args.room_size,
            )
        num_scenes = args.num_scenes
        size_list = [
            resolve_living_space_tier(canonical, args.start_seed + i)
            for i in range(num_scenes)
        ]
    else:
        num_scenes = args.num_scenes
        if args.room_size is None:
            if canonical in WORKING_SPACE_ROOM_TYPES:
                rng = random.Random(args.start_seed)
                size_list = [
                    rng.choice(["small", "medium", "large"]) for _ in range(num_scenes)
                ]
            else:
                size_list = ["medium"] * num_scenes
        else:
            size_list = [args.room_size] * num_scenes

    avail = get_available_tiers(canonical)
    for tier in set(size_list):
        if tier not in avail:
            parser.error(
                f"Room type '{canonical}' has no tier '{tier}'. Valid: {avail}"
            )

    for i in range(num_scenes):
        seed = args.start_seed + i
        size_name = size_list[i]
        if num_scenes == 1 and args.output_folder is not None:
            output_folder = Path(args.output_folder).resolve()
        else:
            output_folder = scenes_root / f"{canonical}_s{seed}"
        output_folder.mkdir(parents=True, exist_ok=True)

        print(f"\n=== Scene {i+1}/{num_scenes}: seed={seed}, room={canonical}, size={size_name} ===")
        cmd = _build_single_command(seed, size_name, canonical, output_folder, args)
        _run(cmd, gpu=args.gpu)

        if "render" in args.task:
            _finalize_scene_outputs(output_folder, seed, canonical, args, gpu=args.gpu)
            print(
                f"[OK] Scene saved: scene.blend, color.png, instance_segmentation.png, metadata.json, GT.json -> {output_folder}"
            )
        else:
            logger.warning(
                "Task list has no render: skipping object index / metadata; coarse intermediates may remain."
            )
            print(f"[OK] Scene output (no render postprocess): {output_folder}")


if __name__ == "__main__":
    main()
