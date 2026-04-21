"""
evaluate.py — Unified evaluation entry point (dims 1–4 via [WIP]metrics; dim 5 via metrics_grid).

Usage:
  # Single scene
  python evaluate.py --gt path/to/GT.json --pred path/to/pred.json --model gemini --frame camera

  # Batch — write DB + summary.json + per_roomtype/ only
  python evaluate.py --batch --frame floor --all-models

  # Batch — also write per_model/*.csv
  python evaluate.py --batch --frame floor --all-models --output

  # Single-model batch
  python evaluate.py --batch --frame camera --model gemini

  # Dry-run — check GT/pred alignment only, no metrics
  python evaluate.py --dry-run --frame floor

Output paths (all under <IDEAL>/result/evaluation/):
  results.db                   always written (batch mode)
  summary.json                 always written (batch mode)
  per_roomtype/<room>.json     always written (batch mode)
  per_model/<model>.csv        only when --output flag is set
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from metrics_grid import SUPPORTED_ROOMS, evaluate_grid
from extract_grid import get_room_type
from export_results import (
    export_per_model_csv,
    export_per_roomtype_json,
    export_summary_json,
)

# ---------------------------------------------------------------------------
# Dynamic loader for [WIP]metrics.py
# ---------------------------------------------------------------------------

_WIP_METRICS_MODULE: Any = None


def _wip_metrics():
    """Load [WIP]metrics.py by file path (brackets in filename prevent normal import)."""
    global _WIP_METRICS_MODULE
    if _WIP_METRICS_MODULE is not None:
        return _WIP_METRICS_MODULE
    wip_path = Path(__file__).resolve().parent / "[WIP]metrics.py"
    if not wip_path.is_file():
        raise FileNotFoundError(f"WIP metrics module not found: {wip_path}")
    spec = importlib.util.spec_from_file_location("ideal_wip_metrics", wip_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec for {wip_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ideal_wip_metrics"] = mod
    spec.loader.exec_module(mod)
    _WIP_METRICS_MODULE = mod
    return mod


def _evaluate_scene_dims_1_4(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
) -> Dict[str, Any]:
    return _wip_metrics().evaluate_scene(gt_payload, pred_payload)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_eastern() -> str:
    """Current US Eastern time (fixed UTC-4 EDT) as ISO-8601 string."""
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSON / path helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _default_repo_root() -> Path:
    """IDEAL repository root (two levels above benchmarks/)."""
    return Path(__file__).resolve().parents[2]


def _relpath_from_repo(repo_root: Path, path: Path) -> str:
    """Express path relative to the IDEAL repo root to avoid exposing server absolute paths."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return f"<outside_repo>/{path.name}"


def _eval_root(repo_root: Path) -> Path:
    return repo_root / "result" / "evaluation"


# ---------------------------------------------------------------------------
# Scene and inference path discovery
# ---------------------------------------------------------------------------

def discover_scene_dirs(scenes_root: Path) -> List[Path]:
    skip = {"manifests", ".git", "__pycache__"}
    out: List[Path] = []
    for child in sorted(scenes_root.iterdir()):
        if not child.is_dir() or child.name in skip:
            continue
        if (child / "GT.json").is_file():
            out.append(child)
            continue
        for sub in sorted(child.iterdir()):
            if sub.is_dir() and (sub / "GT.json").is_file():
                out.append(sub)
    return sorted(out, key=lambda p: str(p))


def result_branch_for_frame(frame: str) -> str:
    f = frame.strip().lower()
    if f in ("floor", "room", "room_center"):
        return "floor"
    if f in ("camera", "cam", "camera_center"):
        return "camera"
    raise ValueError(f"Unknown frame {frame!r}; use 'floor' or 'camera'")


def resolve_result_root(repo_root: Path, frame: str) -> Path:
    return repo_root / "result" / "inference" / result_branch_for_frame(frame)


def normalize_result_root(path: Path, frame: str) -> Path:
    p = path.expanduser().resolve()
    branch = result_branch_for_frame(frame)
    if p.name == "result":
        return p / "inference" / branch
    if p.name == "inference":
        return p / branch
    return p


def find_pred_json(result_root: Path, scene_folder_name: str, model: str) -> Optional[Path]:
    d = result_root / scene_folder_name
    if not d.is_dir():
        return None
    exact = d / f"{model}.json"
    if exact.is_file():
        return exact
    candidates = sorted(d.glob(f"{model}*.json"))
    return candidates[0] if candidates else None


def _is_prediction_json(path: Path) -> bool:
    if path.suffix != ".json":
        return False
    name = path.name
    if name.endswith(("_render.json", "_benchmark.json", "_bench.json")):
        return False
    if name in ("manifest.json", "summary.json"):
        return False
    return True


def discover_model_stems(result_root: Path) -> List[str]:
    """Discover all model name stems (JSON file stems) under result/inference/<frame>/."""
    stems: set[str] = set()
    for scene_dir in result_root.iterdir():
        if not scene_dir.is_dir():
            continue
        for f in scene_dir.glob("*.json"):
            if _is_prediction_json(f):
                stems.add(f.stem)
    return sorted(stems)


def iter_gt_pred_pairs(
    scenes_root: Path,
    result_root: Path,
    model: str,
) -> Iterator[Tuple[Path, Path]]:
    for scene_dir in discover_scene_dirs(scenes_root):
        pred = find_pred_json(result_root, scene_dir.name, model)
        if pred is None:
            continue
        yield scene_dir / "GT.json", pred


# ── Dry-run ───────────────────────────────────────────────────

def dry_run_scene_alignment(
    repo_root: Path,
    scenes_root: Path,
    result_root: Path,
) -> Dict[str, Any]:
    """Compare GT scene dirs vs inference dirs without running metrics or writing to DB."""
    gt_ids   = {p.name for p in discover_scene_dirs(scenes_root)}
    pred_ids = set(
        p.name for p in result_root.iterdir() if p.is_dir()
    ) if result_root.is_dir() else set()

    matched  = sorted(gt_ids & pred_ids)
    gt_only  = sorted(gt_ids - pred_ids)
    pred_only= sorted(pred_ids - gt_ids)

    models_by_scene: Dict[str, List[str]] = {}
    for sid in matched:
        d = result_root / sid
        models_by_scene[sid] = sorted(
            f.stem for f in d.glob("*.json") if _is_prediction_json(f)
        )

    return {
        "scenes_root":                          _relpath_from_repo(repo_root, scenes_root),
        "result_root":                          _relpath_from_repo(repo_root, result_root),
        "gt_scene_count":                       len(gt_ids),
        "pred_scene_dir_count":                 len(pred_ids),
        "matched_count":                        len(matched),
        "gt_only_scene_ids":                    gt_only,
        "pred_only_scene_ids":                  pred_only,
        "prediction_json_stems_by_matched_scene": models_by_scene,
    }


# ── SQLite ────────────────────────────────────────────────────

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS scene_results (
    model           TEXT    NOT NULL,
    frame           TEXT    NOT NULL,
    scene_id        TEXT    NOT NULL,
    room_type       TEXT,
    d1_pe           REAL,
    d1_re           REAL,
    d1_iou          REAL,
    d1_oor          REAL,
    d1_oob          REAL,
    d2_rr           REAL,
    d2_hr           REAL,
    d3_acc          REAL,
    d4_fail         REAL,
    d5_grid         REAL,
    d5_in_grid_pe   REAL,
    d5_yaw          REAL,
    evaluated_at    TEXT,
    PRIMARY KEY (model, frame, scene_id)
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(DB_SCHEMA)
    conn.commit()
    return conn


def _flatten_metrics(
    result: Dict[str, Any],
    model: str,
    frame: str,
) -> Dict[str, Any]:
    d14  = result.get("dimensions_1_4", {})
    d5   = result.get("dimension_5", {})
    geo  = d14.get("geometry_spatial_accuracy", {})
    rec  = d14.get("object_recognition", {})
    scn  = d14.get("scene_recognition", {})
    sout = d14.get("structured_output", {})

    d1_pe  = (geo.get("position_error")  or {}).get("mean_error_m")
    d1_re  = (geo.get("rotation_error")  or {}).get("rotation_error_rate")
    d1_iou = (geo.get("bbox_iou")        or {}).get("mean_iou")
    d1_oor = ((d14.get("gt_independent") or {}).get("object_collision")    or {}).get("collision_rate_Q_over_N")
    d1_oob = ((d14.get("gt_independent") or {}).get("object_out_of_bounds") or {}).get("out_of_bounds_rate_P_over_N")
    d2_rr  = rec.get("recognition_rate_K_over_N")
    d2_hr  = rec.get("hallucination_rate_M_over_K")
    d3_acc = (scn.get("room_type_accuracy")  or {}).get("room_type_accuracy")
    d4_fail= (sout.get("json_failure")       or {}).get("json_failure_rate")

    d5_grid = d5_pe = d5_yaw = None
    if d5.get("applicable") and d5.get("targets"):
        t = d5["targets"][0]
        if not t.get("skip_reason"):
            gc = t.get("grid_coverage") or {}
            pe = t.get("in_grid_pe")    or {}
            ye = t.get("yaw_error")     or {}
            d5_grid = gc.get("grid_coverage_score")
            d5_pe   = pe.get("in_grid_position_error_m")
            d5_yaw  = ye.get("yaw_error_deg")

    return {
        "model":         model,
        "frame":         frame,
        "scene_id":      result["scene_id"],
        "room_type":     result.get("room_type"),
        "d1_pe":         d1_pe,
        "d1_re":         d1_re,
        "d1_iou":        d1_iou,
        "d1_oor":        d1_oor,
        "d1_oob":        d1_oob,
        "d2_rr":         d2_rr,
        "d2_hr":         d2_hr,
        "d3_acc":        d3_acc,
        "d4_fail":       d4_fail,
        "d5_grid":       d5_grid,
        "d5_in_grid_pe": d5_pe,
        "d5_yaw":        d5_yaw,
        "evaluated_at":  _now_eastern(),
    }


def upsert_row(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    cols = list(row.keys())
    sql  = (
        f"INSERT OR REPLACE INTO scene_results "
        f"({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    )
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _is_grid_room(room_type: Optional[str]) -> bool:
    """Rooms that use layout-grid metrics (dimension 5)."""
    if not room_type:
        return False
    return str(room_type).strip().lower() in SUPPORTED_ROOMS


def evaluate(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
) -> Dict[str, Any]:
    scene_id  = gt_payload.get("scene_id", "unknown")
    room_type = get_room_type(gt_payload)

    if _is_grid_room(room_type):
        # Grid pipeline (classroom / library / office): D3 + D4 + D5 only.
        # D1 and D2 are not applicable — object matching is not run.
        dim_1_4 = _evaluate_scene_dims_1_4(gt_payload, pred_payload)
        dim_5   = evaluate_grid(gt_payload, pred_payload)
    else:
        # Non-grid pipeline (living space + meetingroom): D1-D4, no D5.
        dim_1_4 = _evaluate_scene_dims_1_4(gt_payload, pred_payload)
        dim_5   = {"applicable": False, "room_type": room_type}

    return {
        "scene_id":       scene_id,
        "room_type":      room_type,
        "dimensions_1_4": dim_1_4,
        "dimension_5":    dim_5,
    }



# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------

def run_batch_for_model(
    conn: sqlite3.Connection,
    scenes_root: Path,
    result_root: Path,
    model: str,
    frame: str,
) -> Tuple[int, List[str]]:
    """
    Evaluate all scenes for one model; upsert each row to DB immediately.
    Returns (n_evaluated, list_of_missing_scene_dirs).
    """
    all_scene_dirs = discover_scene_dirs(scenes_root)
    have_pred: set  = set()
    missing:   List[str] = []

    for gt_path, pred_path in iter_gt_pred_pairs(scenes_root, result_root, model):
        have_pred.add(gt_path.parent.name)
        result = evaluate(_load_json(str(gt_path)), _load_json(str(pred_path)))
        upsert_row(conn, _flatten_metrics(result, model, frame))
        print(f"  [{model}] {result['scene_id']} -> upserted")

    for sd in all_scene_dirs:
        if sd.name not in have_pred:
            missing.append(sd.name)

    return len(have_pred), missing


# ── CLI ───────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="IDEAL evaluation CLI")
    parser.add_argument("--batch",      action="store_true", help="Run in batch mode")
    parser.add_argument("--dry-run",    action="store_true", help="Only check GT/pred alignment; no metrics, no DB writes")
    parser.add_argument("--all-models", action="store_true", help="Auto-discover all model stems under result_root")
    parser.add_argument("--gt",    help="Path to GT JSON (single-scene mode)")
    parser.add_argument("--pred",  help="Path to prediction JSON (single-scene mode)")
    parser.add_argument("--model", help="Model name (JSON stem)")
    parser.add_argument("--scenes-root", default=None)
    parser.add_argument("--result-root", default=None)
    parser.add_argument("--frame", default="camera", choices=("floor", "camera"))
    parser.add_argument("--db",    default=None, help="Path to SQLite database (default: result/evaluation/results.db)")
    parser.add_argument(
        "--output",
        action="store_true",
        help="Also write per_model/*.csv (default: off)",
    )
    args = parser.parse_args()

    repo        = _default_repo_root()
    eval_root   = _eval_root(repo)
    scenes_root = (
        Path(args.scenes_root).expanduser().resolve()
        if args.scenes_root
        else repo / "data" / "scenes"
    )
    result_root = (
        normalize_result_root(Path(args.result_root), args.frame)
        if args.result_root
        else resolve_result_root(repo, args.frame)
    )

    # ── Dry-run ──
    if args.dry_run:
        report = dry_run_scene_alignment(repo, scenes_root, result_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # --- Single-scene mode ---
    if not args.batch:
        if not args.gt or not args.pred:
            parser.error("Single-scene mode requires --gt and --pred, or use --batch")
        if not args.model:
            parser.error("Single-scene mode requires --model")

        db_path = Path(args.db) if args.db else eval_root / "results.db"
        conn    = init_db(db_path)
        result  = evaluate(_load_json(args.gt), _load_json(args.pred))
        upsert_row(conn, _flatten_metrics(result, args.model, args.frame))
        print(f"[{args.model}] {result['scene_id']} -> upserted")
        conn.close()
        return

    # --- Batch mode ---
    if args.all_models and args.model:
        parser.error("--all-models and --model are mutually exclusive")
    if not args.all_models and not args.model:
        parser.error("--batch requires --model or --all-models")

    models = discover_model_stems(result_root) if args.all_models else [args.model]
    if not models:
        parser.error(f"No prediction JSON files found under {_relpath_from_repo(repo, result_root)}")

    db_path = Path(args.db) if args.db else eval_root / "results.db"
    conn    = init_db(db_path)

    print(f"Models: {models}")
    print(f"DB: {_relpath_from_repo(repo, db_path)}\n")

    # Evaluate each model; upsert results to DB immediately after each scene.
    for model in models:
        print(f"--- {model} ---")
        n_eval, missing = run_batch_for_model(conn, scenes_root, result_root, model, args.frame)
        print(f"  Done: {n_eval} scene(s) upserted; {len(missing)} missing pred")
        if missing:
            print(f"  Missing pred: {missing[:5]}{'...' if len(missing) > 5 else ''}")
        print()

    # All models done — generate output files from DB (overwrite existing).
    print("Generating output files...")

    export_per_roomtype_json(conn, eval_root, args.frame)   # always generated
    print(f"  per_roomtype/ -> {_relpath_from_repo(repo, eval_root / 'per_roomtype')}")

    export_summary_json(conn, eval_root, args.frame)       # always generated
    print(f"  summary.json -> {_relpath_from_repo(repo, eval_root / 'summary.json')}")

    if args.output:
        export_per_model_csv(conn, eval_root, args.frame)  # only when --output flag is set
        print(f"  per_model/ -> {_relpath_from_repo(repo, eval_root / 'per_model')}")

    conn.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()