#!/usr/bin/env python3
"""
reconstruct.py — Reconstruction entry point: ``compose.py`` then ``render.py`` (Blender ``bpy``).

For each task, ``compose.py`` reads a prediction JSON plus GT sidecars (``GT.json``,
``objects_in_view.json`` or ``info.json``) and ``scene.blend``; ``render.py`` writes
``<model>_render.json``, ``<model>.png``, and a render ``.blend`` **next to** the prediction
JSON without editing the source ``scene.blend``. GPU: ``configs.gpu_select``; parallel runs:
``--workers`` / ``-j`` with ``--gpu`` for round-robin across devices.

Usage:
  # Full (default) — enumerate inference_result, GT from data/scenes
  python reconstruct.py

  # Full — restrict models / scenes
  python reconstruct.py --model-id gemini-2.5-pro --scene office_s123

  # Full — pick a Gemini JSON for one scene
  python reconstruct.py --gemini --scene-id office_s123

  # Sandbox — dev subset (you populate dirs; default five model stems unless --all-models)
  python reconstruct.py --mode sandbox --sandbox-tree flat

  # Sandbox — nested tree like production
  python reconstruct.py --mode sandbox --sandbox-tree nested --all-models

  # Dry-run — print compose/render commands only
  python reconstruct.py --dry-run

Outputs (beside each ``<model>.json``): ``<model>_render.json``, ``<model>.png``, render blend.

``--mode full`` (default):
  Predictions: ``result/inference/inference_result/<room_type>/<scene_id>/<model>.json``
  GT scenes: ``data/scenes/<room_type>/<scene_id>/`` (unless ``--scenes-root``)

``--mode sandbox``:
  Predictions: ``result/reconstruction/prediction/…`` (``--prediction-root``)
  GT scenes: ``result/reconstruction/gt/…`` (``--gt-root``)
  Both trees start empty — copy only the scenes you need.
  ``--sandbox-tree flat``: ``prediction/<scene_id>/<model>.json``
  ``--sandbox-tree nested``: ``prediction/<room_type>/<scene_id>/<model>.json``
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.reconstruction import runtime as _rt
from configs.gpu_select import resolve_cuda_visible_devices


# ---------------------------------------------------------------------------
# Constants (sandbox)
# ---------------------------------------------------------------------------

RECONSTRUCTION_TAG = "reconstruction"
DEFAULT_PRED_REL = Path("result") / "reconstruction" / "prediction"
DEFAULT_GT_REL = Path("result") / "reconstruction" / "gt"

DEFAULT_SANDBOX_MODEL_STEMS: frozenset[str] = frozenset(
    {
        "gemini-2.5-pro",
        "claude-sonnet-4-6",
        "gpt-4o",
        "google_gemma-4-31B-it",
        "qwen2.5-vl-72b-instruct",
    }
)

# (path_tag, model_id, scene_id, prediction_path, room_type_or_none)
ReconTask = tuple[str, str, str, Path, str | None]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _compose_script() -> Path:
    return Path(__file__).resolve().parent / "compose.py"


def _render_script() -> Path:
    return Path(__file__).resolve().parent / "render.py"


def _render_child_env(cuda_visible_devices: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices
    return env


def _cuda_slots_from_resolved(resolved: str) -> list[str]:
    s = resolved.strip().replace(" ", "")
    if not s:
        return ["0"]
    parts = [p for p in s.split(",") if p.strip()]
    return parts if parts else ["0"]


def _scenes_root_full(project_dir: Path, args: argparse.Namespace) -> Path:
    if args.scenes_root is not None:
        return _rt.resolve_path_for_task(project_dir, args.scenes_root)
    return project_dir / "data" / "scenes"


def _prediction_root(project_dir: Path, args: argparse.Namespace) -> Path:
    return _rt.resolve_path_for_task(project_dir, args.prediction_root)


def _gt_root(project_dir: Path, args: argparse.Namespace) -> Path:
    return _rt.resolve_path_for_task(project_dir, args.gt_root)


def _sandbox_internal_tree(args: argparse.Namespace) -> str:
    """``test`` = flat, ``full`` = nested (matches legacy discovery_* APIs)."""
    return "test" if args.sandbox_tree == "flat" else "full"


# ---------------------------------------------------------------------------
# full-mode: GT / gemini
# ---------------------------------------------------------------------------


def _resolve_scene_dir(
    project_dir: Path, scenes_root: Path, scene_id: str
) -> Path:
    roots: list[Path] = []
    for r in (
        scenes_root.resolve(),
        (project_dir / "data" / "scenes").resolve(),
        (project_dir / "data" / "scenes2test").resolve(),
    ):
        if r not in roots:
            roots.append(r)

    def _valid(d: Path) -> bool:
        if not d.is_dir():
            return False
        has_info = (d / "objects_in_view.json").is_file() or (d / "info.json").is_file()
        return has_info and (d / "GT.json").is_file()

    for r in roots:
        cand = (r / scene_id).resolve()
        if _valid(cand):
            return cand

    for r in roots:
        if not r.is_dir():
            continue
        for child in sorted(r.iterdir()):
            if not child.is_dir():
                continue
            cand = child / scene_id
            if _valid(cand):
                return cand.resolve()

    hints: list[str] = []
    for r in roots:
        d = (r / scene_id).resolve()
        if d.is_dir():
            miss = []
            if not ((d / "objects_in_view.json").is_file() or (d / "info.json").is_file()):
                miss.append("objects_in_view.json or info.json")
            if not (d / "GT.json").is_file():
                miss.append("GT.json")
            if miss:
                hints.append(f"{d} (missing: {', '.join(miss)})")

    msg = (
        "compose requires objects_in_view.json (or info.json) and GT.json; tried: "
        + ", ".join(str(r / scene_id) for r in roots)
    )
    if hints:
        msg += ". " + "; ".join(hints)
    raise FileNotFoundError(msg)


def resolve_gemini_prediction(
    project_dir: Path,
    scene_id: str,
    gemini_model: str | None,
) -> Path:
    def _is_pred(p: Path) -> bool:
        return not p.name.endswith("_render.json") and _rt.is_model_prediction_json(p)

    full_root = _rt.inference_predictions_root(project_dir) / _rt.INFERENCE_RESULT_DIR_NAME
    if not full_root.is_dir():
        raise FileNotFoundError(f"Inference directory not found: {full_root}")
    dirs_tries: list[Path] = []
    for room_dir in sorted(full_root.iterdir()):
        if not room_dir.is_dir():
            continue
        d = room_dir / scene_id
        if d.is_dir():
            dirs_tries.append(d)
    if not dirs_tries:
        raise FileNotFoundError(
            f"No scene directory for scene_id={scene_id!r} under {full_root}"
        )

    if gemini_model:
        for d in dirs_tries:
            exact = d / f"{gemini_model}.json"
            if exact.is_file():
                return exact.resolve()
        available = sorted(
            p.name
            for d in dirs_tries
            for p in d.glob("*gemini*.json")
            if _is_pred(p)
        )
        hint = dirs_tries[0] if len(dirs_tries) == 1 else f"{dirs_tries}"
        raise FileNotFoundError(
            f"Specified gemini model file not found ({gemini_model}) under {hint}\n"
            f"Available: {available}"
        )

    for d in dirs_tries:
        cands = sorted(p for p in d.glob("*gemini*.json") if _is_pred(p))
        if not cands:
            continue
        if len(cands) > 1:
            print(
                f"[reconstruct] Multiple gemini files found; using {cands[0].name}. "
                f"Use --gemini-model to specify. Others: {[p.name for p in cands[1:]]}",
                file=sys.stderr,
            )
        return cands[0].resolve()

    raise FileNotFoundError(
        f"No gemini prediction JSON found under scene_id={scene_id!r}: {dirs_tries}"
    )


# ---------------------------------------------------------------------------
# Sandbox discovery
# ---------------------------------------------------------------------------


def parse_reconstruction_prediction_file(pred_file: Path) -> ReconTask:
    pred_file = pred_file.resolve()
    parts = pred_file.parts
    ix: int | None = None
    for i in range(len(parts) - 1):
        if parts[i] == "result" and parts[i + 1] == "reconstruction":
            ix = i + 1
            break
    if ix is None:
        raise ValueError(
            "Sandbox --prediction path must include .../result/reconstruction/...\n"
            f"Got: {pred_file}"
        )
    rel_dirs = list(parts[ix + 1 : -1])
    if rel_dirs and rel_dirs[0] == "prediction":
        rel_dirs = rel_dirs[1:]
    if len(rel_dirs) == 1:
        scene_id, room = rel_dirs[0], None
    elif len(rel_dirs) == 2:
        room, scene_id = rel_dirs[0], rel_dirs[1]
    else:
        raise ValueError(
            "Under result/reconstruction expected "
            "prediction/<scene_id>/<model>.json or prediction/<room_type>/<scene_id>/<model>.json "
            f"(or legacy tree without prediction/ segment); got depth {len(rel_dirs)} for:\n  {pred_file}"
        )
    return RECONSTRUCTION_TAG, pred_file.stem, scene_id, pred_file, room


def discover_reconstruction_predictions(
    pred_root: Path,
    mode: str,
    model_filter: _rt.ModelStemFilter,
    scene_filter: set[str] | None,
    *,
    room_filter: str | None = None,
    flat_room_types: list[str] | None = None,
) -> list[ReconTask]:
    pred_root = pred_root.resolve()
    if not pred_root.is_dir():
        raise FileNotFoundError(f"Prediction root not found: {pred_root}")

    out: list[ReconTask] = []

    def _collect_scene_dir(scene_dir: Path, sid: str, room: str | None) -> None:
        if scene_filter is not None and sid not in scene_filter:
            return

        def _add(d: Path) -> None:
            for jf in sorted(d.glob("*.json")):
                if not _rt.is_model_prediction_json(jf):
                    continue
                mid = jf.stem
                if not _rt.model_stem_matches(mid, model_filter):
                    continue
                out.append((RECONSTRUCTION_TAG, mid, sid, jf.resolve(), room))

        flat = [jf for jf in sorted(scene_dir.glob("*.json")) if _rt.is_model_prediction_json(jf)]
        if flat:
            _add(scene_dir)
            return
        nested = scene_dir / "json"
        if nested.is_dir():
            _add(nested)

    if mode == "test":
        if room_filter is not None:
            raise ValueError("internal: room_filter applies only to nested sandbox tree")
        prefixes: list[str] | None = None
        if flat_room_types:
            pr = [f"{rt.strip().lower()}_" for rt in flat_room_types if str(rt).strip()]
            if pr:
                prefixes = pr
        for scene_dir in sorted(pred_root.iterdir()):
            if not scene_dir.is_dir():
                continue
            sid = scene_dir.name
            if prefixes is not None:
                low = sid.lower()
                if not any(low.startswith(p) for p in prefixes):
                    continue
            _collect_scene_dir(scene_dir, sid, None)
        return out

    room_dirs: list[Path]
    if room_filter is not None:
        rd = pred_root / room_filter
        if not rd.is_dir():
            raise FileNotFoundError(f"Room directory not found under prediction root: {rd}")
        room_dirs = [rd]
    else:
        room_dirs = sorted(p for p in pred_root.iterdir() if p.is_dir())

    for room_dir in room_dirs:
        room_name = room_dir.name
        for scene_dir in sorted(room_dir.iterdir()):
            if not scene_dir.is_dir():
                continue
            _collect_scene_dir(scene_dir, scene_dir.name, room_name)

    return out


def _normalized_room_types(ns: argparse.Namespace) -> list[str] | None:
    raw = getattr(ns, "room_types", None)
    if not raw:
        return None
    return list(dict.fromkeys(raw))


def _discover_tasks_batch_sandbox(
    pred_root: Path,
    tree: str,
    model_filter: _rt.ModelStemFilter,
    scene_filter: set[str] | None,
    room_types: list[str] | None,
) -> list[ReconTask]:
    if not room_types:
        return discover_reconstruction_predictions(
            pred_root,
            tree,
            model_filter,
            scene_filter,
            room_filter=None,
            flat_room_types=None,
        )
    if tree == "full":
        tasks: list[ReconTask] = []
        for rt in room_types:
            tasks.extend(
                discover_reconstruction_predictions(
                    pred_root,
                    tree,
                    model_filter,
                    scene_filter,
                    room_filter=rt,
                    flat_room_types=None,
                )
            )
        return tasks
    return discover_reconstruction_predictions(
        pred_root,
        tree,
        model_filter,
        scene_filter,
        room_filter=None,
        flat_room_types=room_types,
    )


def _ensure_flat_scene_matches_room_types(scene_id: str, room_types: list[str] | None) -> None:
    if not room_types:
        return
    low = scene_id.lower()
    ok = any(low.startswith(f"{rt.strip().lower()}_") for rt in room_types)
    if not ok:
        raise ValueError(
            f"scene_id {scene_id!r} does not match any --room-type prefix {room_types!r} "
            "(expected <room_type>_... under flat layout)"
        )


def _ensure_task_dir_room_matches_filter(implied_room: str, room_types: list[str] | None) -> None:
    if not room_types or implied_room in room_types:
        return
    raise ValueError(
        f"--task-dir implies room {implied_room!r}, but --room-type restricts to {room_types!r}"
    )


def _resolve_gt_scene_dir(
    gt_root: Path,
    scene_id: str,
    tree: str,
    room_type: str | None,
) -> Path:
    gt_root = gt_root.resolve()

    def _valid(d: Path) -> bool:
        if not d.is_dir():
            return False
        has_info = (d / "objects_in_view.json").is_file() or (d / "info.json").is_file()
        return has_info and (d / "GT.json").is_file()

    if room_type is not None:
        cand = (gt_root / room_type / scene_id).resolve()
        if _valid(cand):
            return cand
        raise FileNotFoundError(
            "compose requires objects_in_view.json (or info.json) and GT.json under "
            f"{cand}"
        )

    cand = (gt_root / scene_id).resolve()
    if _valid(cand):
        return cand

    if tree == "full":
        for child in sorted(gt_root.iterdir()):
            if not child.is_dir():
                continue
            c2 = (child / scene_id).resolve()
            if _valid(c2):
                return c2

    hints: list[str] = []
    d = (gt_root / scene_id).resolve()
    if d.is_dir():
        miss = []
        if not ((d / "objects_in_view.json").is_file() or (d / "info.json").is_file()):
            miss.append("objects_in_view.json or info.json")
        if not (d / "GT.json").is_file():
            miss.append("GT.json")
        if miss:
            hints.append(f"{d} (missing: {', '.join(miss)})")

    msg = (
        "compose requires objects_in_view.json (or info.json) and GT.json under "
        f"{gt_root}/…/{scene_id}/"
    )
    if hints:
        msg += ". " + "; ".join(hints)
    raise FileNotFoundError(msg)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IDEAL reconstruction: compose.py -> _render.json -> render.py",
    )
    p.add_argument(
        "--mode",
        choices=("full", "sandbox"),
        default="full",
        help=(
            "full: production preds in result/inference/inference_result/, GT in data/scenes/. "
            "sandbox: copy subset into result/reconstruction/{prediction,gt}/ for local iteration."
        ),
    )
    p.add_argument(
        "--sandbox-tree",
        choices=("flat", "nested"),
        default="flat",
        help="sandbox only: prediction/GT layout (flat: <scene>/; nested: <room>/<scene>/)",
    )
    p.add_argument(
        "--all-models",
        action="store_true",
        help="sandbox only: use every prediction JSON found (else default five stems)",
    )
    p.add_argument("--gemini", action="store_true", help="full only: pick a Gemini JSON under --scene-id")
    p.add_argument(
        "--gemini-model",
        default=None,
        metavar="MODEL_ID",
        help="stem of the gemini pred JSON (optional with --gemini)",
    )
    p.add_argument("--scene-id", default=None, help="filter or single-scene id (with --gemini)")
    p.add_argument(
        "--model-id",
        action="append",
        dest="model_ids",
        default=None,
        metavar="STEM",
        help="restrict to this model stem (repeat for several). In sandbox, defaults to five stems if omitted.",
    )
    p.add_argument("--scene", action="append", dest="scenes", default=None)
    p.add_argument("--task-dir", type=Path, default=None)
    p.add_argument("--prediction", type=Path, default=None)
    p.add_argument(
        "--scenes-root",
        type=Path,
        default=None,
        help="full only: GT / scene.blend root (default: data/scenes)",
    )
    p.add_argument(
        "--prediction-root",
        type=Path,
        default=DEFAULT_PRED_REL,
        help=f"sandbox: prediction tree root (default: {DEFAULT_PRED_REL})",
    )
    p.add_argument(
        "--gt-root",
        type=Path,
        default=DEFAULT_GT_REL,
        help=f"sandbox: GT tree root (default: {DEFAULT_GT_REL})",
    )
    p.add_argument(
        "--room-type",
        action="append",
        dest="room_types",
        default=None,
        metavar="NAME",
        help="sandbox: nested → only <NAME>/ under prediction root; flat → scene ids prefixed <NAME>_",
    )
    p.add_argument(
        "--blend-root",
        type=Path,
        default=None,
        help="parent of scene.blend dirs (default: parent of resolved GT scene dir)",
    )
    p.add_argument(
        "--gpu",
        type=str,
        default=None,
        metavar="SPEC",
        help="CUDA indices for compose/render subprocesses (configs.gpu_select)",
    )
    p.add_argument(
        "--workers",
        "-j",
        type=int,
        default=1,
        metavar="N",
        help="Concurrent tasks (with --gpu 0,1,... for multi-GPU round-robin)",
    )
    p.add_argument("--skip-render", action="store_true", help="compose only (no .blend / .png)")
    p.add_argument("--dry-run", action="store_true", help="print commands only")
    return p.parse_args()


def _validate_mode_flags(ns: argparse.Namespace) -> None:
    if ns.mode == "full":
        if ns.all_models:
            raise ValueError("--all-models applies only to --mode sandbox")
        if ns.room_types:
            raise ValueError("--room-type applies only to --mode sandbox")
        if ns.gemini and not ns.scene_id:
            raise ValueError("--gemini requires --scene-id")
    else:
        if ns.gemini:
            raise ValueError("--gemini applies only to --mode full")
        if ns.scenes_root is not None:
            raise ValueError("--scenes-root applies only to --mode full (sandbox uses --gt-root)")
        if ns.all_models and ns.model_ids:
            raise ValueError("Cannot combine --all-models with --model-id (pick one).")


def _model_stem_filter_full(ns: argparse.Namespace) -> _rt.ModelStemFilter:
    ids = getattr(ns, "model_ids", None)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    return frozenset(ids)


def _model_stem_filter_sandbox(ns: argparse.Namespace) -> _rt.ModelStemFilter:
    if ns.all_models:
        return None
    ids = getattr(ns, "model_ids", None)
    if not ids:
        return DEFAULT_SANDBOX_MODEL_STEMS
    if len(ids) == 1:
        return ids[0]
    return frozenset(ids)


def _single_model_pick_for_resolve(mf: _rt.ModelStemFilter) -> str | None:
    if mf is None or isinstance(mf, str):
        return mf
    if len(mf) == 1:
        return next(iter(mf))
    raise ValueError(
        "Pointing --task-dir at a single scene directory requires exactly one --model-id; "
        f"got {sorted(mf)}. Use a batch --task-dir parent instead."
    )


# ---------------------------------------------------------------------------
# Task collection
# ---------------------------------------------------------------------------


def collect_tasks_full(args: argparse.Namespace) -> list[ReconTask]:
    project_dir = _project_dir()
    scene_filter = set(args.scenes) if args.scenes else None
    tasks: list[ReconTask] = []
    mf = _model_stem_filter_full(args)

    if args.gemini:
        if args.task_dir is not None or args.prediction is not None:
            raise ValueError("--gemini cannot be combined with --task-dir or --prediction")
        pred = resolve_gemini_prediction(
            project_dir,
            args.scene_id,  # type: ignore[arg-type]
            args.gemini_model,
        )
        tasks.append((_rt.INFERENCE_RESULT_DIR_NAME, pred.stem, args.scene_id, pred, None))
        return tasks

    if args.task_dir is not None and args.prediction is not None:
        raise ValueError("--task-dir and --prediction are mutually exclusive")

    if args.prediction is not None:
        pred_file = _rt.resolve_path_for_task(project_dir, args.prediction)
        if not pred_file.is_file():
            raise FileNotFoundError(f"--prediction is not a file: {pred_file}")
        proto, mid, sid, pred = _rt.parse_inference_prediction_file(pred_file)
        tasks.append((proto, mid, sid, pred, None))

    elif args.task_dir is not None:
        td = _rt.resolve_path_for_task(project_dir, args.task_dir)
        if _rt.is_inference_result_batch_dir(project_dir, td):
            for proto, mid, sid, pred in _rt.discover_predictions_inference_result(
                project_dir, mf, scene_filter
            ):
                tasks.append((proto, mid, sid, pred, None))
        else:
            pred_file = _rt.resolve_task_prediction_path(
                project_dir, args.task_dir, _single_model_pick_for_resolve(mf)
            )
            proto, mid, sid, pred = _rt.parse_inference_prediction_file(pred_file)
            tasks.append((proto, mid, sid, pred, None))

    else:
        sid_filter = scene_filter
        if args.scene_id:
            only = {args.scene_id}
            sid_filter = only if scene_filter is None else scene_filter & only
            if not sid_filter:
                raise ValueError("--scene-id and --scene have no overlap")
        for proto, mid, sid, pred in _rt.discover_predictions_inference_result(
            project_dir,
            mf,
            sid_filter,
        ):
            tasks.append((proto, mid, sid, pred, None))

    return tasks


def collect_tasks_sandbox(args: argparse.Namespace) -> list[ReconTask]:
    project_dir = _project_dir()
    scene_filter = set(args.scenes) if args.scenes else None
    mf = _model_stem_filter_sandbox(args)
    pred_root = _prediction_root(project_dir, args)
    room_types = _normalized_room_types(args)
    tree = _sandbox_internal_tree(args)
    tasks: list[ReconTask] = []

    if args.task_dir is not None and args.prediction is not None:
        raise ValueError("--task-dir and --prediction are mutually exclusive")

    if args.prediction is not None:
        pred_file = _rt.resolve_path_for_task(project_dir, args.prediction)
        if not pred_file.is_file():
            raise FileNotFoundError(f"--prediction is not a file: {pred_file}")
        task = parse_reconstruction_prediction_file(pred_file)
        if room_types and tree == "test":
            _ensure_flat_scene_matches_room_types(task[2], room_types)
        tasks.append(task)

    elif args.task_dir is not None:
        td = _rt.resolve_path_for_task(project_dir, args.task_dir)
        td_r = td.resolve()
        pr_r = pred_root.resolve()

        if td_r == pr_r:
            sid_filter = scene_filter
            if args.scene_id:
                only = {args.scene_id}
                sid_filter = only if scene_filter is None else scene_filter & only
                if not sid_filter:
                    raise ValueError("--scene-id and --scene have no overlap")
            tasks.extend(
                _discover_tasks_batch_sandbox(pred_root, tree, mf, sid_filter, room_types)
            )

        elif tree == "full" and td_r.parent == pr_r:
            sid_filter = scene_filter
            if args.scene_id:
                only = {args.scene_id}
                sid_filter = only if scene_filter is None else scene_filter & only
                if not sid_filter:
                    raise ValueError("--scene-id and --scene have no overlap")
            _ensure_task_dir_room_matches_filter(td.name, room_types)
            tasks.extend(
                discover_reconstruction_predictions(
                    pred_root,
                    tree,
                    mf,
                    sid_filter,
                    room_filter=td.name,
                )
            )

        elif tree == "test" and td_r.parent == pr_r:
            only = {td.name}
            sid_f = only if scene_filter is None else scene_filter & only
            if not sid_f:
                raise ValueError("--scene filter excludes the scene implied by --task-dir")
            _ensure_flat_scene_matches_room_types(td.name, room_types)
            tasks.extend(
                discover_reconstruction_predictions(
                    pred_root, "test", mf, sid_f, flat_room_types=None
                )
            )

        elif tree == "full" and td_r.parent.parent == pr_r:
            room_name = td_r.parent.name
            only = {td.name}
            sid_f = only if scene_filter is None else scene_filter & only
            if not sid_f:
                raise ValueError("--scene filter excludes the scene implied by --task-dir")
            _ensure_task_dir_room_matches_filter(room_name, room_types)
            tasks.extend(
                discover_reconstruction_predictions(
                    pred_root,
                    "full",
                    mf,
                    sid_f,
                    room_filter=room_name,
                )
            )

        else:
            pred_file = _rt.resolve_task_prediction_path(
                project_dir, args.task_dir, _single_model_pick_for_resolve(mf)
            )
            task = parse_reconstruction_prediction_file(pred_file)
            if room_types and tree == "test":
                _ensure_flat_scene_matches_room_types(task[2], room_types)
            tasks.append(task)

    else:
        sid_filter = scene_filter
        if args.scene_id:
            only = {args.scene_id}
            sid_filter = only if scene_filter is None else scene_filter & only
            if not sid_filter:
                raise ValueError("--scene-id and --scene have no overlap")
        tasks.extend(
            _discover_tasks_batch_sandbox(pred_root, tree, mf, sid_filter, room_types)
        )

    return tasks


def collect_tasks(args: argparse.Namespace) -> list[ReconTask]:
    if args.mode == "full":
        return collect_tasks_full(args)
    return collect_tasks_sandbox(args)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def _build_compose_cmd(
    args: argparse.Namespace,
    model_id: str,
    scene_id: str,
    prediction: Path,
    room_type: str | None,
) -> list[str]:
    project_dir = _project_dir()
    if args.mode == "sandbox":
        gt_root = _gt_root(project_dir, args)
        scene_dir = _resolve_gt_scene_dir(
            gt_root, scene_id, _sandbox_internal_tree(args), room_type
        )
    else:
        scene_dir = _resolve_scene_dir(
            project_dir, _scenes_root_full(project_dir, args), scene_id
        )
    blend = _rt.scene_blend_path(scene_dir.parent, args.blend_root, scene_id)
    if not blend.is_file():
        raise FileNotFoundError(f"scene.blend not found for scene_id={scene_id!r}: {blend}")
    info_json = (
        scene_dir / "objects_in_view.json"
        if (scene_dir / "objects_in_view.json").is_file()
        else scene_dir / "info.json"
    )
    gt_json = scene_dir / "GT.json"

    return [
        sys.executable,
        str(_compose_script()),
        "--prediction",
        str(prediction.resolve()),
        "--info-json",
        str(info_json),
        "--gt-json",
        str(gt_json.resolve()),
        "--blend",
        str(blend.resolve()),
        "--scene-id",
        scene_id,
        "--model-id",
        model_id,
    ]


def _build_render_cmd(
    args: argparse.Namespace,
    model_id: str,
    scene_id: str,
    prediction: Path,
    room_type: str | None,
) -> list[str]:
    project_dir = _project_dir()
    if args.mode == "sandbox":
        gt_root = _gt_root(project_dir, args)
        scene_dir = _resolve_gt_scene_dir(
            gt_root, scene_id, _sandbox_internal_tree(args), room_type
        )
    else:
        scene_dir = _resolve_scene_dir(
            project_dir, _scenes_root_full(project_dir, args), scene_id
        )
    blend = _rt.scene_blend_path(scene_dir.parent, args.blend_root, scene_id)
    render_json = prediction.parent / f"{model_id}_render.json"
    out_png = prediction.parent / f"{model_id}.png"

    return [
        sys.executable,
        str(_render_script()),
        "--",
        str(blend.resolve()),
        str(render_json.resolve()),
        model_id,
        str(out_png.resolve()),
    ]


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------


def _indexed_worker(
    indexed: tuple[int, ReconTask],
    ns: argparse.Namespace,
    slots: list[str],
) -> int:
    idx, (_, mid, sid, pred, room) = indexed
    one = slots[idx % len(slots)]
    return _run_one(ns, mid, sid, pred, room, child_env=_render_child_env(one))


def _run_one(
    args: argparse.Namespace,
    mid: str,
    sid: str,
    pred: Path,
    room: str | None,
    *,
    child_env: dict[str, str],
) -> int:
    compose_cmd = _build_compose_cmd(args, mid, sid, pred, room)

    if args.dry_run:
        print("[reconstruct] compose:", " ".join(compose_cmd))
        if not args.skip_render:
            try:
                print(
                    "[reconstruct] render:",
                    " ".join(_build_render_cmd(args, mid, sid, pred, room)),
                )
            except FileNotFoundError as exc:
                print(f"[reconstruct] render skipped ({exc})", file=sys.stderr)
        return 0

    r = subprocess.run(compose_cmd, cwd=str(_project_dir()), env=child_env)
    if r.returncode != 0:
        print(f"[reconstruct] compose.py failed: scene={sid} model={mid}", file=sys.stderr)
        return r.returncode

    if args.skip_render:
        print(f"[reconstruct] --skip-render: {sid} {mid}", file=sys.stderr)
        return 0

    try:
        render_cmd = _build_render_cmd(args, mid, sid, pred, room)
    except FileNotFoundError as exc:
        print(f"[reconstruct] cannot build render command: {exc}", file=sys.stderr)
        return 1

    r2 = subprocess.run(render_cmd, cwd=str(_project_dir()), env=child_env)
    if r2.returncode != 0:
        print(f"[reconstruct] render.py failed: scene={sid} model={mid}", file=sys.stderr)
        return r2.returncode

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()

    try:
        _validate_mode_flags(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.workers < 1:
        print("--workers / -j must be >= 1", file=sys.stderr)
        return 2

    try:
        tasks = collect_tasks(args)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if not tasks:
        hint = (
            "result/reconstruction/prediction (populate manually for --mode sandbox)"
            if args.mode == "sandbox"
            else "result/inference/inference_result"
        )
        print(f"No prediction JSON files found (check {hint} or CLI args)", file=sys.stderr)
        return 1

    cuda_vis = resolve_cuda_visible_devices(args.gpu)
    cuda_slots = _cuda_slots_from_resolved(cuda_vis)

    if not args.dry_run and not args.skip_render:
        _rt.ensure_bpy_importable()

    n_workers_eff = 1 if args.dry_run else max(1, args.workers)
    pool_workers = max(1, min(n_workers_eff, len(tasks)))

    print(
        f"[reconstruct] CUDA_VISIBLE_DEVICES={cuda_vis} (round-robin): {cuda_slots!r}",
        file=sys.stderr,
    )
    if args.dry_run and args.workers > 1:
        print(
            f"[reconstruct] note: --dry-run is serial (--workers={args.workers} ignored)",
            file=sys.stderr,
        )
    elif pool_workers > 1:
        print(f"[reconstruct] parallel pool size {pool_workers}", file=sys.stderr)

    if args.mode == "sandbox":
        pd = _project_dir()
        print(
            f"[reconstruct] sandbox roots: pred={_prediction_root(pd, args)} "
            f"gt={_gt_root(pd, args)} tree={args.sandbox_tree}",
            file=sys.stderr,
        )

    if len(tasks) > 1:
        protos = sorted({p for p, _, _, _, _ in tasks})
        print(
            f"[reconstruct] {len(tasks)} tasks, mode={args.mode}, tag={','.join(protos)}",
            file=sys.stderr,
        )

    wp = partial(_indexed_worker, ns=args, slots=cuda_slots)

    if pool_workers <= 1:
        codes = [wp(pair) for pair in enumerate(tasks)]
    else:
        with ThreadPoolExecutor(max_workers=pool_workers) as ex:
            codes = list(ex.map(wp, enumerate(tasks)))

    for code in codes:
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    sys.exit(main())
