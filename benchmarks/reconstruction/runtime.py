"""
runtime.py — Environment and path-layout layer for the reconstruction pipeline.

Responsibilities:
- Fixed ``result/`` directory layout conventions.
- Prediction file discovery across protocol/scene/model dimensions.
- Pre-flight ``bpy`` import check before launching render subprocesses.

This module has no CLI entry point. It is imported by ``reconstruct.py``.

Name rationale: "runtime" mirrors its usage in Go/Rust ecosystems — the
module describes the environment and preconditions the program requires,
as opposed to the business-logic scripts (``reconstruct.py``, ``compose.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path


def resolve_path_for_task(project_dir: Path, raw: Path) -> Path:
    """Resolve a ``--task-dir`` or ``--prediction`` argument to an absolute path.

    For relative paths: prefer ``cwd / raw`` if it exists, otherwise fall back
    to ``project_dir / raw`` (path relative to the IDEAL repository root).
    """
    raw = raw.expanduser()
    if raw.is_absolute():
        return raw.resolve()
    cwd_hit = (Path.cwd() / raw).resolve()
    proj_hit = (project_dir / raw).resolve()
    if cwd_hit.exists():
        return cwd_hit
    return proj_hit


def result_root(project_dir: Path) -> Path:
    """``IDEAL/result/`` — top-level artifacts directory (inference, reconstruction, …)."""
    return project_dir / "result"


def inference_predictions_root(project_dir: Path) -> Path:
    """Canonical VLM prediction layout: ``result/inference/<floor|camera>/<scene_id>/``."""
    return result_root(project_dir) / "inference"


def _legacy_prediction_protocol_root(project_dir: Path) -> Path:
    """Pre-migration layout: ``result/<floor|camera>/`` directly under ``result``."""
    return result_root(project_dir)


def prediction_protocol_scan_root(project_dir: Path) -> Path:
    """Return the directory whose immediate children are ``floor`` / ``camera``.

    Prefers ``result/inference``; falls back to the legacy ``result/`` root.
    """
    infer = inference_predictions_root(project_dir)
    if infer.is_dir():
        return infer
    return _legacy_prediction_protocol_root(project_dir)


def is_protocol_root_batch_dir(project_dir: Path, resolved_dir: Path) -> bool:
    """Return True if *resolved_dir* is ``result/inference/<floor|camera>`` (or legacy equivalent)."""
    d = resolved_dir.resolve()
    if not d.is_dir() or d.name not in ("floor", "camera"):
        return False
    rr = result_root(project_dir).resolve()
    parent = d.parent.resolve()
    if parent == rr:
        return True
    return parent == inference_predictions_root(project_dir).resolve()


def is_model_prediction_json(path: Path) -> bool:
    """Return True if *path* looks like a VLM prediction JSON.

    Excludes known sidecar files such as benchmark results, manifests, and
    summaries.
    """
    if path.suffix != ".json":
        return False
    name = path.name
    if name.endswith("_benchmark.json") or name.endswith("_bench.json"):
        return False
    if name.endswith("_render.json"):
        return False
    if name in ("manifest.json", "summary.json"):
        return False
    return True


def ensure_bpy_importable() -> None:
    """Verify that the current interpreter can ``import bpy`` before launching render.py.

    Exits with code 2 if ``bpy`` is not available, printing installation
    instructions.
    """
    try:
        import importlib.util
        if importlib.util.find_spec("bpy") is None:
            raise ImportError("bpy not found")
    except ImportError:
        print(
            "The current interpreter cannot import bpy.\n"
            "Install it with: pip install 'bpy>=4.2,<5' (see requirements.txt),\n"
            "or activate a conda environment that already has bpy configured.\n"
            "The reconstruction renderer requires this; locating a system Blender "
            "executable is no longer supported.",
            file=sys.stderr,
        )
        sys.exit(2)


def parse_inference_prediction_file(pred_file: Path) -> tuple[str, str, str, Path]:
    """Parse ``(protocol, model_id, scene_id, prediction_path)`` from a file path.

    Supported layouts
    -----------------
    - Canonical:  ``.../result/inference/<protocol>/<scene_id>/<model_id>.json``
    - Legacy:     ``.../result/<protocol>/<scene_id>/<model_id>.json``
    - Nested:     ``.../inference/result/.../<scene_id>/json/<model_id>.json``
    - Runs:       ``.../runs/inference/<protocol>/<model_id>/<scene_id>/prediction.json``
    """
    pred_file = pred_file.resolve()
    parts = pred_file.parts

    for i, seg in enumerate(parts):
        if seg != "result":
            continue
        if i > 0 and parts[i - 1] == "inference":
            continue
        if (
            i + 4 < len(parts)
            and parts[i + 1] == "inference"
            and parts[i + 4] == pred_file.name
            and pred_file.suffix == ".json"
        ):
            protocol = parts[i + 2]
            scene_id = parts[i + 3]
            if protocol in ("floor", "camera"):
                return protocol, pred_file.stem, scene_id, pred_file
        if (
            i + 3 < len(parts)
            and parts[i + 3] == pred_file.name
            and pred_file.suffix == ".json"
        ):
            protocol = parts[i + 1]
            scene_id = parts[i + 2]
            if protocol in ("floor", "camera"):
                return protocol, pred_file.stem, scene_id, pred_file

    for i, seg in enumerate(parts):
        if seg == "result" and i >= 1 and parts[i - 1] == "inference":
            if (
                i + 4 < len(parts)
                and parts[i + 3] == "json"
                and pred_file.suffix == ".json"
            ):
                protocol = parts[i + 1]
                scene_id = parts[i + 2]
                return protocol, pred_file.stem, scene_id, pred_file
            break

    if "runs" in parts and "inference" in parts:
        i = parts.index("inference")
        if (
            i >= 1
            and i + 3 < len(parts)
            and parts[i + 1] != "result"
            and pred_file.name == "prediction.json"
        ):
            protocol = parts[i + 1]
            model_id = parts[i + 2]
            scene_id = parts[i + 3]
            return protocol, model_id, scene_id, pred_file

    raise ValueError(
        "Cannot parse prediction path. Expected one of:\n"
        "  .../result/inference/<protocol>/<scene_id>/<model_id>.json\n"
        "  .../result/<protocol>/<scene_id>/<model_id>.json\n"
        "  .../runs/inference/<protocol>/<model_id>/<scene_id>/prediction.json\n"
        f"Got: {pred_file}"
    )


def resolve_task_prediction_path(
    project_dir: Path, task_dir: Path, model_id: str | None
) -> Path:
    """Resolve a single prediction JSON from a ``--task-dir`` argument.

    Accepts flat scene directories, legacy ``json/`` subdirectories, and
    ``runs/`` task directories containing ``prediction.json``.
    """
    td = resolve_path_for_task(project_dir, task_dir)

    if td.is_file():
        if td.suffix == ".json" or td.name == "prediction.json":
            return td
        raise ValueError(f"--task-dir points to an unsupported file type: {td}")
    if not td.is_dir():
        raise ValueError(f"--task-dir does not exist: {td}")

    legacy = td / "prediction.json"
    if legacy.is_file():
        return legacy

    def _pick(candidates: list[Path], directory: Path) -> Path:
        if model_id:
            p = directory / f"{model_id}.json"
            if not p.is_file():
                raise ValueError(f"File not found: {p}")
            return p
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError(
            f"Multiple prediction JSONs in {directory}; specify one with --model-id"
        )

    if td.name == "json":
        jfs = sorted(p for p in td.glob("*.json") if is_model_prediction_json(p))
        return _pick(jfs, td)

    jfs = sorted(p for p in td.glob("*.json") if is_model_prediction_json(p))
    if jfs:
        return _pick(jfs, td)

    jdir = td / "json"
    if jdir.is_dir():
        jfs = sorted(p for p in jdir.glob("*.json") if is_model_prediction_json(p))
        return _pick(jfs, jdir)

    raise ValueError(
        f"Cannot infer prediction JSON from directory "
        f"(expected prediction.json, a model .json, or a json/ subdirectory): {td}"
    )


def discover_predictions(
    project_dir: Path,
    protocol: str | None,
    model_filter: str | None,
    scene_filter: set[str] | None,
) -> list[tuple[str, str, str, Path]]:
    """Scan the result tree and return all matching prediction files.

    Searches ``result/inference/<protocol>/<scene_id>/*.json`` (canonical) and
    the legacy ``result/<protocol>/...`` layout. Nested ``json/`` subdirectories
    under a scene directory are also scanned.

    Returns a list of ``(protocol, model_id, scene_id, path)`` tuples.
    """
    root = result_root(project_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Missing result directory: {root}")

    scan_root = prediction_protocol_scan_root(project_dir)
    out: list[tuple[str, str, str, Path]] = []

    protocols = (
        [protocol]
        if protocol
        else sorted(p.name for p in scan_root.iterdir() if p.is_dir())
    )

    for proto in protocols:
        pdir = scan_root / proto
        if not pdir.is_dir():
            continue
        for scene_dir in sorted(pdir.iterdir()):
            if not scene_dir.is_dir():
                continue
            sid = scene_dir.name
            if scene_filter is not None and sid not in scene_filter:
                continue

            def _add(d: Path) -> None:
                for jf in sorted(d.glob("*.json")):
                    if not is_model_prediction_json(jf):
                        continue
                    mid = jf.stem
                    if model_filter and mid != model_filter:
                        continue
                    out.append((proto, mid, sid, jf.resolve()))

            flat = [jf for jf in sorted(scene_dir.glob("*.json")) if is_model_prediction_json(jf)]
            if flat:
                _add(scene_dir)
            else:
                nested = scene_dir / "json"
                if nested.is_dir():
                    _add(nested)

    return out


def scene_blend_path(scenes_root: Path, blend_root: Path | None, scene_id: str) -> Path:
    """Return the expected path to ``scene.blend`` for a given scene.

    Uses *blend_root* if provided, otherwise falls back to *scenes_root*.
    """
    root = blend_root if blend_root is not None else scenes_root
    return root / scene_id / "scene.blend"