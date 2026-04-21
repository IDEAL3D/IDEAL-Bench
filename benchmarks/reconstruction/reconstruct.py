#!/usr/bin/env python3
"""
reconstruct.py — IDEAL reconstruction pipeline (full flow).

Task discovery
--------------
Prediction files are discovered under::

    <IDEAL>/result/inference/<protocol>/<scene_id>/<model_id>.json

Discovery logic lives in ``runtime.py``. When ``--protocol`` is omitted,
only the ``floor`` directory is scanned by default.

Per-task execution (in order; each step must succeed before the next runs)
--------------------------------------------------------------------------
1. **compose.py** — runs ``match_objects`` to pair predictions with in-view
   GT slots, writes geometry back, merges with ``GT.json``, and emits
   ``{model_id}_render.json`` next to the prediction file.

2. **render.py** (same directory as this script) — executed with the current interpreter (which
   must be able to ``import bpy``): reads ``{model_id}_render.json``, saves
   ``{model_id}.blend``, renders ``{model_id}.png``.  The original
   ``scene.blend`` is never modified.

Output layout::

    result/inference/<protocol>/<scene_id>/<model_id>_render.json
    result/inference/<protocol>/<scene_id>/<model_id>.blend
    result/inference/<protocol>/<scene_id>/<model_id>.png

Usage (from the repository root)::

    cd benchmarks/reconstruction
    conda activate infinigen

    python reconstruct.py                      # floor protocol, all scenes
    python reconstruct.py --protocol floor
    python reconstruct.py --protocol all       # floor + camera
    python reconstruct.py --scene-id bedroom_s14704319 --gemini
    python reconstruct.py --scene-id bedroom_s1412833193 --gemini \\
        --gemini-model gemini-2.5-pro-preview-03-25
    python reconstruct.py --task-dir result/inference/floor/bedroom_s1412833192 \\
        --model-id gpt-5.2-2025-12-11
    python reconstruct.py --prediction result/inference/floor/.../model.json --dry-run
    python reconstruct.py --scene-id ... --gemini --skip-render
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# 与 compose.py 相同：脚本在 benchmarks/reconstruction/ 下执行时须把 IDEAL 根加入 sys.path。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.reconstruction import runtime as _rt


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _project_dir() -> Path:
    # benchmarks/reconstruction/reconstruct.py  ->  IDEAL/
    return Path(__file__).resolve().parent.parent.parent


def _compose_script() -> Path:
    return Path(__file__).resolve().parent / "compose.py"


def _render_script() -> Path:
    return Path(__file__).resolve().parent / "render.py"


def _resolve_scene_dir(project_dir: Path, scenes_root: Path, scene_id: str) -> Path:
    """Locate the scene directory that contains both the sidecar JSON and GT.json.

    Search order: ``scenes_root`` -> ``data/scenes`` -> ``data/scenes2test``.
    """
    roots: list[Path] = []
    for r in (scenes_root, project_dir / "data" / "scenes", project_dir / "data" / "scenes2test"):
        rr = r.expanduser().resolve()
        if rr not in roots:
            roots.append(rr)

    def _valid(d: Path) -> bool:
        if not d.is_dir():
            return False
        has_info = (d / "objects_in_view.json").is_file() or (d / "info.json").is_file()
        return has_info and (d / "GT.json").is_file()

    for r in roots:
        cand = (r / scene_id).resolve()
        if _valid(cand):
            return cand

    # 目录存在但缺 sidecar / GT 时给更明确的提示（常见于占位空目录）
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
    protocol: str,
    scene_id: str,
    gemini_model: str | None = None,
) -> Path:
    """Locate a Gemini prediction JSON for the given scene.

    If *gemini_model* is provided (stem without ``.json``), that exact file is
    required.  Otherwise the first ``*gemini*.json`` in the scene directory
    (lexicographic order) is returned, with a warning when multiple candidates
    exist.
    """
    d = _rt.inference_predictions_root(project_dir) / protocol / scene_id
    if not d.is_dir():
        raise FileNotFoundError(f"Scene result directory not found: {d}")

    def _is_pred(p: Path) -> bool:
        return not p.name.endswith("_render.json") and _rt.is_model_prediction_json(p)

    if gemini_model:
        exact = d / f"{gemini_model}.json"
        if not exact.is_file():
            available = sorted(p.name for p in d.glob("*gemini*.json") if _is_pred(p))
            raise FileNotFoundError(
                f"Specified gemini model file not found: {exact}\n"
                f"Available: {available}"
            )
        return exact.resolve()

    cands = sorted(p for p in d.glob("*gemini*.json") if _is_pred(p))
    if not cands:
        raise FileNotFoundError(f"No gemini prediction JSON found in: {d}")
    if len(cands) > 1:
        print(
            f"[reconstruct] Multiple gemini files found; using {cands[0].name}. "
            f"Use --gemini-model to specify. Others: {[p.name for p in cands[1:]]}",
            file=sys.stderr,
        )
    return cands[0].resolve()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    root = _project_dir()
    p = argparse.ArgumentParser(
        description="IDEAL: compose.py -> {model}_render.json -> render.py -> .blend / .png"
    )
    p.add_argument("--gemini", action="store_true",
                   help="single-scene mode: use a gemini prediction JSON (requires --scene-id)")
    p.add_argument("--gemini-model", default=None, metavar="MODEL_ID",
                   help="stem of the gemini file to use (e.g. gemini-2.5-pro-preview-03-25); "
                        "auto-selected when omitted")
    p.add_argument("--scene-id", default=None,
                   help="scene directory name; used with --gemini or as a filter")
    p.add_argument("--protocol", default="floor", choices=("floor", "camera", "all"),
                   help="prediction coordinate frame (default: floor)")
    p.add_argument("--model-id", default=None)
    p.add_argument("--scene", action="append", dest="scenes", default=None)
    p.add_argument("--task-dir", type=Path, default=None)
    p.add_argument("--prediction", type=Path, default=None)
    p.add_argument("--scenes-root", type=Path, default=root / "data" / "scenes",
                   help="root that contains <scene_id>/objects_in_view.json and GT.json "
                        "(default: data/scenes; data/scenes2test is also tried automatically)")
    p.add_argument("--blend-root", type=Path, default=None,
                   help="parent of scene.blend directories; defaults to --scenes-root")
    p.add_argument("--skip-render", action="store_true",
                   help="run compose only; skip render.py (no .blend / .png produced)")
    p.add_argument("--dry-run", action="store_true",
                   help="print commands without executing them")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Task collection
# ---------------------------------------------------------------------------


def collect_tasks(args: argparse.Namespace) -> list[tuple[str, str, str, Path]]:
    """Return a list of (protocol, model_id, scene_id, prediction_path) tuples."""
    project_dir = _project_dir()
    scene_filter = set(args.scenes) if args.scenes else None
    tasks: list[tuple[str, str, str, Path]] = []

    if args.gemini:
        if not args.scene_id:
            raise ValueError("--gemini requires --scene-id")
        if args.task_dir is not None or args.prediction is not None:
            raise ValueError("--gemini cannot be combined with --task-dir or --prediction")
        if args.protocol == "all":
            raise ValueError("--gemini requires an explicit --protocol (floor or camera), not 'all'")
        pred = resolve_gemini_prediction(project_dir, args.protocol, args.scene_id, args.gemini_model)
        tasks.append((args.protocol, pred.stem, args.scene_id, pred))
        return tasks

    if args.task_dir is not None and args.prediction is not None:
        raise ValueError("--task-dir and --prediction are mutually exclusive")

    if args.prediction is not None:
        pred_file = _rt.resolve_path_for_task(project_dir, args.prediction)
        if not pred_file.is_file():
            raise FileNotFoundError(f"--prediction is not a file: {pred_file}")
        proto, mid, sid, pred = _rt.parse_inference_prediction_file(pred_file)
        tasks.append((proto, mid, sid, pred))

    elif args.task_dir is not None:
        td = _rt.resolve_path_for_task(project_dir, args.task_dir)
        if _rt.is_protocol_root_batch_dir(project_dir, td):
            for p, m, s, pred in _rt.discover_predictions(project_dir, td.name, args.model_id, scene_filter):
                tasks.append((p, m, s, pred))
        else:
            pred_file = _rt.resolve_task_prediction_path(project_dir, args.task_dir, args.model_id)
            proto, mid, sid, pred = _rt.parse_inference_prediction_file(pred_file)
            tasks.append((proto, mid, sid, pred))

    else:
        sid_filter = scene_filter
        if args.scene_id:
            only = {args.scene_id}
            sid_filter = only if scene_filter is None else scene_filter & only
            if not sid_filter:
                raise ValueError("--scene-id and --scene have no overlap")
        proto_scan = None if args.protocol == "all" else args.protocol
        for proto, mid, sid, pred in _rt.discover_predictions(project_dir, proto_scan, args.model_id, sid_filter):
            tasks.append((proto, mid, sid, pred))

    return tasks


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def _build_compose_cmd(
    args: argparse.Namespace, protocol: str, model_id: str, scene_id: str, prediction: Path
) -> list[str]:
    project_dir = _project_dir()
    scene_dir = _resolve_scene_dir(project_dir, args.scenes_root, scene_id)
    blend = _rt.scene_blend_path(scene_dir.parent, args.blend_root, scene_id)
    if not blend.is_file():
        # compose now reads original (obj.location, obj.dimensions) from
        # scene.blend via bpy, so a missing blend is a hard failure rather
        # than a silent skip.
        raise FileNotFoundError(
            f"scene.blend not found for scene_id={scene_id!r}: {blend}"
        )
    info_json = (
        scene_dir / "objects_in_view.json"
        if (scene_dir / "objects_in_view.json").is_file()
        else scene_dir / "info.json"
    )
    gt_json = scene_dir / "GT.json"

    cmd = [
        sys.executable, str(_compose_script()),
        "--prediction", str(prediction.resolve()),
        "--info-json",  str(info_json),
        "--gt-json",    str(gt_json.resolve()),
        "--blend",      str(blend.resolve()),
        "--protocol",   protocol,
        "--scene-id",   scene_id,
        "--model-id",   model_id,
    ]
    return cmd


def _build_render_cmd(
    args: argparse.Namespace, protocol: str, model_id: str, scene_id: str, prediction: Path
) -> list[str]:
    scene_dir = _resolve_scene_dir(_project_dir(), args.scenes_root, scene_id)
    blend = _rt.scene_blend_path(scene_dir.parent, args.blend_root, scene_id)
    render_json = prediction.parent / f"{model_id}_render.json"
    out_png = prediction.parent / f"{model_id}.png"

    # render.py v2 reads compose's render.json directly; the schema already
    # tells it which objects are matched / hidden / hallucinated, so the
    # objects_in_view.json whitelist no longer needs to be passed through.
    return [
        sys.executable, str(_render_script()), "--",
        str(blend.resolve()),
        str(render_json.resolve()),
        model_id,
        protocol,
        str(out_png.resolve()),
    ]


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------


def _run_one(
    args: argparse.Namespace, proto: str, mid: str, sid: str, pred: Path
) -> int:
    compose_cmd = _build_compose_cmd(args, proto, mid, sid, pred)

    if args.dry_run:
        print("[reconstruct] compose:", " ".join(compose_cmd))
        if not args.skip_render:
            try:
                print("[reconstruct] render:", " ".join(_build_render_cmd(args, proto, mid, sid, pred)))
            except FileNotFoundError as exc:
                print(f"[reconstruct] render skipped ({exc})", file=sys.stderr)
        return 0

    r = subprocess.run(compose_cmd, cwd=str(_project_dir()))
    if r.returncode != 0:
        print(f"[reconstruct] compose.py failed: scene={sid} model={mid}", file=sys.stderr)
        return r.returncode

    if args.skip_render:
        print(f"[reconstruct] --skip-render: {sid} {mid}", file=sys.stderr)
        return 0

    _rt.ensure_bpy_importable()

    try:
        render_cmd = _build_render_cmd(args, proto, mid, sid, pred)
    except FileNotFoundError as exc:
        print(f"[reconstruct] cannot build render command: {exc}", file=sys.stderr)
        return 1

    r2 = subprocess.run(render_cmd, cwd=str(_project_dir()))
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
        tasks = collect_tasks(args)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if not tasks:
        print("No prediction JSON files found (check result/ or arguments)", file=sys.stderr)
        return 1

    if len(tasks) > 1:
        protos = sorted({p for p, _, _, _ in tasks})
        print(f"[reconstruct] {len(tasks)} tasks, protocol={','.join(protos)}", file=sys.stderr)

    for proto, mid, sid, pred in tasks:
        code = _run_one(args, proto, mid, sid, pred)
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    sys.exit(main())