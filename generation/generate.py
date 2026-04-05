#!/usr/bin/env python
"""
Batch driver for rectangular single-room scenes: delegates to ``generate_lib/core.py``.
Room types are canonical ids in ``configs.constant.ROOM_TYPES`` (10 types). Each scene is finalized to
``scene.blend``, ``color.png``, uint16 ``instance_segmentation.png``, and ``metadata.json``.
By default outputs under the Infinigen tree ``SCENES_SKETCH``; ``--scenes-raw`` writes ``SCENES_RAW`` and updates manifests.
Non-box room layouts are deprecated; see ``../docs/LIMITATION.md``.

Common flags: ``--room-type`` / ``--room-types``, ``--num-scenes``, ``--room-size`` (non-residential), ``--sample``, ``--render``,
``--light-multiplier``, ``--gpu``, ``--start-seed``, ``--scenes-raw``, ``--overrides``.
This entry point always runs coarse+render with denoising; for single-stage runs or custom I/O, use ``generate_lib/core.py`` directly.

Examples (from ``IDEAL/generation``)::

    python generate.py --room-type livingroom --num-scenes 20 --scenes-raw --gpu 3
    python generate.py --room-type homestudio --num-scenes 20 --scenes-raw --gpu 2
   

Long-running jobs, tmux, manifests, and ``core.py`` notes: ``../QUICKSTART/generate.md``.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.constant import (
    LIVING_SPACE_ROOM_TYPES,
    ROOM_TYPES,
    WORKING_SPACE_ROOM_TYPES,
)
from configs.infinigen_paths import (
    INFINIGEN_SUBMODULE_ROOT,
    SCENES_RAW_ROOT,
    SCENES_SKETCH_ROOT,
)

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATE_LIB_DIR = SCRIPT_DIR / "generate_lib"

# SCENES_RAW/manifests: generation batch ledger (distinct from preprocess_batches.jsonl).
GENERATION_BATCHES_JSONL = "generation_batches.jsonl"
_LEGACY_RAW_BATCHES_JSONL = "batches.jsonl"

# Optional per-row status in generation_batches.jsonl (English enum; set manually or by pipeline):
#   pending_preprocess — generation done or in progress; preprocess/archive loop not closed
#   archived — accepted into main pipeline (e.g. preprocessed and published)
#   deleted — batch discarded; exclude from preprocess (clean disk + CSV when appropriate)
GENERATION_BATCH_STATUS_PENDING_PREPROCESS = "pending_preprocess"
GENERATION_BATCH_STATUS_ARCHIVED = "archived"
GENERATION_BATCH_STATUS_DELETED = "deleted"
GENERATION_BATCH_STATUSES = frozenset(
    {
        GENERATION_BATCH_STATUS_PENDING_PREPROCESS,
        GENERATION_BATCH_STATUS_ARCHIVED,
        GENERATION_BATCH_STATUS_DELETED,
    }
)


def _generation_batches_jsonl_path(manifests_dir: Path) -> Path:
    """Path to RAW generation batch ledger; migrate legacy ``batches.jsonl`` once to ``generation_batches.jsonl``."""
    new_p = manifests_dir / GENERATION_BATCHES_JSONL
    old_p = manifests_dir / _LEGACY_RAW_BATCHES_JSONL
    if new_p.is_file():
        return new_p
    if old_p.is_file():
        old_p.rename(new_p)
    return new_p

# Fixed wall thickness (meters), injected via gin as ``RoomConstants.global_params.wall_thickness``.
# Infinigen defaults to random 0.2-0.3 m. The floor slab uses a second SOLIDIFY in ``BlueprintSolidifier.make_room``
# with extrusion **wall_thickness/2** (coupled to wall thickness, not a separate parameter).
# E.g. 0.25 m walls -> ~0.125 m floor extrusion; walkable height still follows the full room mesh.
# Set to None to keep Infinigen's default random wall thickness.
FIXED_WALL_THICKNESS_M: float | None = 0.25


def _gin_wall_thickness_override_args() -> list[str]:
    if FIXED_WALL_THICKNESS_M is None:
        return []
    return [f"RoomConstants.global_params.wall_thickness={float(FIXED_WALL_THICKNESS_M)}"]


# Matches generate_lib.core: non-residential tiers selectable here; residential tiers are fixed/random inside core.
# ``auto`` is generate.py-only: for working_space types, omit ``--room-size`` so core picks small/medium/large 1/3 per scene.
ROOM_SIZE_PRESETS = ("small", "medium", "large", "xlarge", "auto")


def get_render_gpu() -> str:
    """GPU id string when ``--gpu`` is omitted (forwarded to ``core`` as ``CUDA_VISIBLE_DEVICES``).

    Reads ``IDEAL_GPU``; if unset, returns ``'2'``.

    Release note: remove this helper and all ``CUDA_VISIBLE_DEVICES`` injection in the generation
    stack; let the user's environment manage device selection.
    """
    return os.environ.get("IDEAL_GPU", "2")


def _try_infinigen_git_sha() -> str | None:
    """Return Infinigen submodule (``INFINIGEN_SUBMODULE_ROOT``) git HEAD, or None if unavailable."""
    sub = INFINIGEN_SUBMODULE_ROOT
    if not (sub / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", str(sub), "rev-parse", "HEAD"],
            text=True,
            timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _next_room_batch_id(manifests_dir: Path, room_type: str) -> str:
    """Next batch id ``{room_type}_batch_{NNN}`` (e.g. bedroom_batch_003), incrementing max suffix among existing rows."""
    prefix = f"{room_type}_batch_"
    max_n = 0
    p = _generation_batches_jsonl_path(manifests_dir)
    if not p.is_file():
        return f"{prefix}001"
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = rec.get("batch_id", "")
            if not isinstance(bid, str) or not bid.startswith(prefix):
                continue
            suffix = bid[len(prefix) :]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:03d}"


def _manifests_dir() -> Path:
    d = SCENES_RAW_ROOT / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _room_type_scenes_csv_path(manifests_dir: Path, room_type: str) -> Path:
    """Per-room-type scene list CSV: ``{room_type}_scenes.csv``."""
    return manifests_dir / f"{room_type}_scenes.csv"


def _append_csv_rows_for_batch(
    manifests_dir: Path,
    room_type: str,
    batch_id: str,
    scene_ids: list[str],
) -> Path:
    """Append rows to ``{room_type}_scenes.csv``; columns batch_scene_index, batch_id, scene_id."""
    csv_path = _room_type_scenes_csv_path(manifests_dir, room_type)
    csv_header = ["batch_scene_index", "batch_id", "scene_id"]
    write_header = not csv_path.is_file() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(csv_header)
        for i, sid in enumerate(scene_ids):
            w.writerow([i + 1, batch_id, sid])
    return csv_path


def _append_jsonl_line(manifests_dir: Path, record: dict) -> None:
    batches_path = _generation_batches_jsonl_path(manifests_dir)
    with batches_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_scenes_raw_batch_pending_line(
    *,
    batch_id: str,
    room_type: str,
    num_requested: int,
    batch_start_seed: int,
    argv_for_record: list[str],
) -> None:
    """Append a pending batch row when ``--scenes-raw`` starts; num_generated/end_seed_inclusive null until updated per scene."""
    manifests_dir = _manifests_dir()
    sha = _try_infinigen_git_sha()
    record: dict = {
        "batch_id": batch_id,
        "room_type": room_type,
        "num_requested": num_requested,
        "num_generated": None,
        "start_seed": batch_start_seed,
        "end_seed_inclusive": None,
        "scenes_root": "SCENES_RAW",
        "generate_argv": argv_for_record,
        "status": GENERATION_BATCH_STATUS_PENDING_PREPROCESS,
    }
    if sha:
        record["infinigen_submodule_git_sha"] = sha
    _append_jsonl_line(manifests_dir, record)
    print(
        f"[generate.py] RAW manifest: registered batch row {batch_id} "
        f"(num_generated/end_seed_inclusive=null; updated after each scene)"
    )


def _append_scenes_raw_csv_row_only(
    *,
    batch_id: str,
    room_type: str,
    scene_id: str,
    scene_index: int,
    num_requested: int,
) -> None:
    """Append one row to ``{room_type}_scenes.csv`` after a scene succeeds."""
    manifests_dir = _manifests_dir()
    csv_path = _room_type_scenes_csv_path(manifests_dir, room_type)
    csv_header = ["batch_scene_index", "batch_id", "scene_id"]
    write_header = not csv_path.is_file() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(csv_header)
        w.writerow([scene_index + 1, batch_id, scene_id])
    print(
        f"[generate.py] RAW manifest: {csv_path.name} {scene_index + 1}/{num_requested} -> {scene_id}"
    )


def _atomic_write_generation_batches_jsonl(manifests_dir: Path, lines: list[str]) -> None:
    """Rewrite ``generation_batches.jsonl`` atomically via a ``.tmp`` file."""
    p = _generation_batches_jsonl_path(manifests_dir)
    text = "\n".join(lines) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _update_scenes_raw_batch_progress(
    manifests_dir: Path,
    batch_id: str,
    num_generated: int,
    end_seed_inclusive: int,
    num_requested: int,
) -> None:
    """Rewrite the JSONL row for ``batch_id`` with num_generated and end_seed_inclusive (last completed seed)."""
    p = _generation_batches_jsonl_path(manifests_dir)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    raw_lines = p.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    found = False
    for line in raw_lines:
        if not line.strip():
            out_lines.append(line)
            continue
        rec = json.loads(line)
        if rec.get("batch_id") == batch_id:
            rec["num_generated"] = num_generated
            rec["end_seed_inclusive"] = end_seed_inclusive
            if "status" not in rec:
                rec["status"] = GENERATION_BATCH_STATUS_PENDING_PREPROCESS
            out_lines.append(json.dumps(rec, ensure_ascii=False))
            found = True
        else:
            out_lines.append(line)
    if not found:
        raise RuntimeError(
            f"No batch row with batch_id={batch_id!r} in {GENERATION_BATCHES_JSONL}"
        )
    _atomic_write_generation_batches_jsonl(manifests_dir, out_lines)
    print(
        f"[generate.py] RAW manifest: {batch_id} progress {num_generated}/{num_requested}, "
        f"end_seed_inclusive={end_seed_inclusive}"
    )


def _build_argv_box(args: argparse.Namespace) -> list[str]:
    """Build argv for ``generate_lib/core.py`` (no interpreter or script path)."""
    argv = [
        "--room-type",
        args.room_type,
        "--num-scenes",
        str(args.num_scenes),
        "--sample",
        str(args.render_samples),
        "--render-width",
        str(getattr(args, "render_width", 1280)),
        "--light-multiplier",
        str(args.light_multiplier),
        "--gpu",
        args.gpu or get_render_gpu(),
    ]
    if args.room_type in LIVING_SPACE_ROOM_TYPES:
        pass
    elif args.room_size == "auto":
        if args.room_type not in WORKING_SPACE_ROOM_TYPES:
            argv.extend(["--room-size", "medium"])
    else:
        argv.extend(["--room-size", args.room_size])
    if args.start_seed is not None:
        argv.extend(["--start-seed", str(args.start_seed)])
    merged_ov = _gin_wall_thickness_override_args() + list(args.overrides or [])
    if merged_ov:
        argv.extend(["--overrides", *merged_ov])
    if getattr(args, "scenes_root_for_box", None) is not None:
        argv.extend(["--scenes-root", str(args.scenes_root_for_box)])
    return argv


def _run_one_room_type(
    args: argparse.Namespace,
    room_type: str,
    num_scenes: int,
    *,
    raw_manifest: bool,
) -> None:
    """Run generation for ``num_scenes`` scenes of one ``room_type``.

    If ``raw_manifest`` (``--scenes-raw``): when ``--start-seed`` is omitted, draw a random start seed
    here (same distribution as ``generate_lib.core``) so manifests stay consistent.

    With ``--scenes-raw``, append a pending row to ``generation_batches.jsonl``, then after each scene
    append CSV and rewrite that row so num_generated/end_seed_inclusive track progress; interrupt leaves partial progress.
    """
    run_args = copy.copy(args)
    run_args.room_type = room_type
    run_args.num_scenes = num_scenes
    if raw_manifest:
        if args.start_seed is None:
            run_args.start_seed = secrets.randbelow(2**31 - 1)
    else:
        if args.start_seed is None:
            run_args.start_seed = None

    script = GENERATE_LIB_DIR / "core.py"

    if raw_manifest:
        assert run_args.start_seed is not None
        batch_start = int(run_args.start_seed)
        folder = room_type
        argv_for_record = sys.argv.copy()
        manifests_dir = _manifests_dir()
        batch_id = _next_room_batch_id(manifests_dir, room_type)

        _append_scenes_raw_batch_pending_line(
            batch_id=batch_id,
            room_type=room_type,
            num_requested=num_scenes,
            batch_start_seed=batch_start,
            argv_for_record=argv_for_record,
        )

        try:
            for i in range(num_scenes):
                one = copy.copy(run_args)
                one.num_scenes = 1
                one.start_seed = batch_start + i
                argv = _build_argv_box(one)
                cmd = [sys.executable, str(script)] + argv
                print(f"\n{'=' * 60}")
                print(
                    f"[generate.py] room-type={room_type}  scene {i + 1}/{num_scenes}  "
                    f"(seed={batch_start + i})"
                )
                print(f"{'=' * 60}")
                subprocess.run(cmd, check=True)

                sid = f"{folder}_s{batch_start + i}"
                _append_scenes_raw_csv_row_only(
                    batch_id=batch_id,
                    room_type=room_type,
                    scene_id=sid,
                    scene_index=i,
                    num_requested=num_scenes,
                )
                _update_scenes_raw_batch_progress(
                    manifests_dir,
                    batch_id,
                    num_generated=i + 1,
                    end_seed_inclusive=batch_start + i,
                    num_requested=num_scenes,
                )
        except KeyboardInterrupt:
            print(
                "\n[generate.py] Interrupted: generation_batches.jsonl and CSV reflect progress through the last scene; "
                "resume remaining scenes if num_generated < num_requested, or edit the batch row per manifests/README.txt.\n",
                file=sys.stderr,
            )
            raise

        return

    argv = _build_argv_box(run_args)
    cmd = [sys.executable, str(script)] + argv
    print(f"\n{'=' * 60}")
    print(f"[generate.py] room-type={room_type}  num={num_scenes}")
    print(f"{'=' * 60}")
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="INFINIGEN entry point: delegates to generate_lib.core for rectangular single-room scenes.",
    )

    # Room type: mutually exclusive; omit both to default to bedroom.
    type_group = parser.add_mutually_exclusive_group()
    type_group.add_argument(
        "--room-type",
        type=str,
        default=None,
        choices=list(ROOM_TYPES),
        help="Single canonical room type (default bedroom if both omitted)",
    )
    type_group.add_argument(
        "--room-types",
        nargs="+",
        metavar="ROOM_TYPE",
        choices=list(ROOM_TYPES),
        default=None,
        help="Multiple room types; each gets --num-scenes scenes",
    )

    parser.add_argument(
        "--num-scenes",
        type=int,
        default=1,
        help="Number of scenes per room type (default 1)",
    )
    parser.add_argument(
        "--room-size",
        type=str,
        default="auto",
        choices=list(ROOM_SIZE_PRESETS),
        help=(
            "Size tier for non-residential types only (ignored for residential). Default **auto**: "
            "working_space types omit ``--room-size`` so core draws small/medium/large 1/3 per scene. "
            "Explicit small/medium/large/xlarge fixes the tier for all scenes of that type."
        ),
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=128,
        metavar="N",
        dest="render_samples",
        help="Cycles samples per render (default 128)",
    )
    parser.add_argument(
        "--render",
        type=int,
        choices=(1280, 640),
        default=1280,
        metavar="W",
        dest="render_width",
        help="Image width: 1280 -> 1280x720 (default), 640 -> 640x360",
    )
    parser.add_argument(
        "--light-multiplier",
        type=float,
        default=5.0,
        help="Interior light energy multiplier (default 5.0)",
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help=(
            "GPU id for CUDA_VISIBLE_DEVICES (e.g. --gpu 2). If omitted, uses IDEAL_GPU env "
            "or default '2' (same as generate_lib.core.get_render_gpu)."
        ),
    )
    parser.add_argument(
        "--start-seed",
        type=int,
        default=None,
        help="Starting seed; multi room-type runs still use independent random starts unless set",
    )
    parser.add_argument(
        "--scenes-raw",
        action="store_true",
        help=(
            f"Write scenes under {SCENES_RAW_ROOT}: append a pending row to {GENERATION_BATCHES_JSONL}, "
            f"update it and CSV after each scene; interrupt may leave num_generated < num_requested. "
            f"Without this flag, default root is {SCENES_SKETCH_ROOT}"
        ),
    )
    parser.add_argument("--overrides", nargs="+", default=[], help="Extra gin override strings (advanced)")
    args = parser.parse_args()

    if args.gpu is None:
        args.gpu = get_render_gpu()

    scenes_root_for_box: Path | None = None
    if args.scenes_raw:
        scenes_root_for_box = SCENES_RAW_ROOT.resolve()
    args.scenes_root_for_box = scenes_root_for_box
    if scenes_root_for_box is not None:
        print(f"[generate.py] Scene root: {scenes_root_for_box}")

    res_label = "640x360" if args.render_width == 640 else "1280x720"
    print(f"[generate.py] Render resolution: {res_label}")

    # Build (room_type, count) task list
    if args.room_types is not None:
        task_list = [(rt, args.num_scenes) for rt in args.room_types]
    else:
        room_type = args.room_type or "bedroom"
        args.room_type = room_type
        task_list = [(room_type, args.num_scenes)]

    total = sum(n for _, n in task_list)
    print(f"[generate.py] {len(task_list)} room type(s), {total} scene(s) total:")
    for rt, n in task_list:
        print(f"  {rt}: {n} scene(s)")

    raw_manifest = bool(args.scenes_raw)

    for room_type, count in task_list:
        _run_one_room_type(args, room_type, count, raw_manifest=raw_manifest)

    print(f"\n[generate.py] Done ({total} scene(s)).")


if __name__ == "__main__":
    main()
