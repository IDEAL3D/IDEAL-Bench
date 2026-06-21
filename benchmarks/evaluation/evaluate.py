"""
evaluate.py — Unified evaluation entry point (dims 1–4 via numerical_metrics; dim 5 via metrics_grid).

When a prediction is unparsed (``_error``, ``_parse_status == "failed"``, or minimal schema
validation fails), **no D2–D5 computation** is run; the DB row stores ``parse_success=0``,
``p_pred=0``, all metric columns NULL. Aggregate summaries already average D2–D5 only over
``parse_success=1`` rows; per-scene ``d_overall`` is NULL.

Usage:
  # Single scene
  python evaluate.py --gt path/to/GT.json --pred path/to/pred.json --model gemini

  # Batch
  python evaluate.py --batch --all-models

  # Batch — also write per_model/*.csv
  python evaluate.py --batch --all-models --output

  # Single-model batch
  python evaluate.py --batch --model gemini

  # Dry-run — check GT/pred alignment only, no metrics
  python evaluate.py --dry-run

Output paths (all under <IDEAL>/result/evaluation/):
  results.db                   always written (batch mode)
  summary.json                 always written (batch mode)
  per_roomtype/<room>.json     always written (batch mode)
  per_model/<model>.csv        only when --output flag is set

Prediction layout (fixed):
  result/inference/inference_result/<room_type>/<scene_id>/<model>.json

GT scenes (unless --scenes-root overrides):
  data/scenes/<room_type>/<scene_id>/GT.json (nested)

Batch/dry-run abort with an error if any prediction scene id has no matching GT scene folder.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from metrics_grid import SUPPORTED_ROOMS, evaluate_grid
from extract_grid import get_room_type
from export_results import (
    EVAL_DB_FRAME_TAG,
    export_per_model_csv,
    export_per_roomtype_json,
    export_summary_json,
)

import numerical_metrics

_BENCHMARKS_ROOT = str(Path(__file__).resolve().parents[1])
if _BENCHMARKS_ROOT not in sys.path:
    sys.path.insert(0, _BENCHMARKS_ROOT)
from common.camera_transform import transform_pred_scene
from common.metric_registry import METRIC_META, rate_metric_keys_for_room

def _load_inview_objects(
    scene_dir: Path,
    gt_payload: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Load objects_in_view.json and return the matching full GT object dicts.

    Cross-references IDs from objects_in_view.json with GT.json so that the
    returned objects carry position, dimensions and rotation (not just category).
    Returns None if the file is missing.
    """
    inview_path = scene_dir / "objects_in_view.json"
    if not inview_path.is_file():
        return None
    with open(inview_path, "r", encoding="utf-8") as f:
        inview_data = json.load(f)

    inview_ids: set = {obj["id"] for obj in inview_data.get("objects", []) if "id" in obj}
    scene = gt_payload.get("scene") or {}
    return [o for o in scene.get("objects", []) if o.get("id") in inview_ids]


def _evaluate_scene_dims_1_4(
    gt_payload: Dict[str, Any],
    pred_payload: Dict[str, Any],
    *,
    inview_objects: Optional[List[Dict[str, Any]]] = None,
    object_metrics: bool = True,
) -> Dict[str, Any]:
    return numerical_metrics.evaluate_scene(
        gt_payload,
        pred_payload,
        inview_objects=inview_objects,
        object_metrics=object_metrics,
    )


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

def resolve_scenes_root(repo_root: Path) -> Path:
    """Default GT tree: nested ``data/scenes/<room_type>/<scene_id>/GT.json``."""
    return repo_root / "data" / "scenes"


def _skip_non_room_inference_dir(name: str) -> bool:
    """Skip meta folders under inference_result (e.g. ``_logs``, ``_smoke``)."""
    return name.startswith(("_", "."))


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


def discover_pred_scene_ids(result_root: Path) -> set[str]:
    """Scene folder names under *result_root* (``inference_result/<room>/<scene>/`` layout)."""
    if not result_root.is_dir():
        return set()
    ids: set[str] = set()
    for room_dir in result_root.iterdir():
        if not room_dir.is_dir():
            continue
        if _skip_non_room_inference_dir(room_dir.name):
            continue
        for scene_dir in room_dir.iterdir():
            if scene_dir.is_dir():
                ids.add(scene_dir.name)
    return ids


def validate_pred_scenes_have_gt_dirs(
    repo_root: Path,
    scenes_root: Path,
    result_root: Path,
) -> None:
    """Raise ``ValueError`` if a prediction exists for a scene id with no GT directory."""
    gt_ids = {p.name for p in discover_scene_dirs(scenes_root)}
    pred_ids = discover_pred_scene_ids(result_root)
    orphan = sorted(pred_ids - gt_ids)
    if not orphan:
        return
    n_show = 25
    head = orphan[:n_show]
    extra = len(orphan) - len(head)
    tail = f" … (+{extra} more)" if extra > 0 else ""
    raise ValueError(
        "Prediction scene id(s) have no matching GT scene folder under "
        f"{_relpath_from_repo(repo_root, scenes_root)}: {head}{tail}"
    )


def resolve_result_root(repo_root: Path) -> Path:
    """Default prediction root: ``result/inference/inference_result``."""
    return repo_root / "result" / "inference" / "inference_result"


def normalize_result_root(path: Path) -> Path:
    """Normalise a user-supplied ``--result-root`` to the prediction tree root.

    If the user passes ``.../result`` or ``.../inference``, append
    ``inference/inference_result`` or ``inference_result`` respectively.
    Passing the ``inference_result`` path explicitly is honoured as-is.
    """
    p = path.expanduser().resolve()
    leaf = "inference_result"
    if p.name == "result":
        return p / "inference" / leaf
    if p.name == "inference":
        return p / leaf
    return p


def find_pred_json(
    result_root: Path,
    scene_folder_name: str,
    model: str,
) -> Optional[Path]:
    """Locate prediction JSON: ``result_root/<room_type>/<scene_id>/<model>.json``."""
    for room_dir in sorted(result_root.iterdir()):
        if not room_dir.is_dir():
            continue
        if _skip_non_room_inference_dir(room_dir.name):
            continue
        d = room_dir / scene_folder_name
        if not d.is_dir():
            continue
        exact = d / f"{model}.json"
        if exact.is_file():
            return exact
        candidates = sorted(d.glob(f"{model}*.json"))
        if candidates:
            return candidates[0]
    return None


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
    """Discover all model name stems under ``inference_result/<room>/<scene>/*.json``."""
    stems: set[str] = set()
    for room_dir in result_root.iterdir():
        if not room_dir.is_dir():
            continue
        if _skip_non_room_inference_dir(room_dir.name):
            continue
        for scene_dir in room_dir.iterdir():
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
    gt_ids = {p.name for p in discover_scene_dirs(scenes_root)}

    pred_ids: set[str] = set()
    if result_root.is_dir():
        for room_dir in result_root.iterdir():
            if not room_dir.is_dir():
                continue
            if _skip_non_room_inference_dir(room_dir.name):
                continue
            for scene_dir in room_dir.iterdir():
                if scene_dir.is_dir():
                    pred_ids.add(scene_dir.name)

    matched  = sorted(gt_ids & pred_ids)
    gt_only  = sorted(gt_ids - pred_ids)
    pred_only= sorted(pred_ids - gt_ids)

    models_by_scene: Dict[str, List[str]] = {}
    for sid in matched:
        stems: List[str] = []
        for room_dir in result_root.iterdir():
            if not room_dir.is_dir():
                continue
            if _skip_non_room_inference_dir(room_dir.name):
                continue
            d = room_dir / sid
            if d.is_dir():
                stems.extend(
                    f.stem for f in d.glob("*.json") if _is_prediction_json(f)
                )
        models_by_scene[sid] = sorted(set(stems))

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
    n_gt            INTEGER,
    k_inview        INTEGER,
    p_pred          INTEGER,
    parse_success   INTEGER,
    rta_match       INTEGER,
    d2_nor          REAL,
    d2_ibr          REAL,
    d3_pa           REAL,
    d3_pe_mean      REAL,
    d3_ra           REAL,
    d3_re_mean      REAL,
    d3_ba           REAL,
    d3_iou_mean     REAL,
    d4_rr           REAL,
    d4_nhr          REAL,
    d5_gcr          REAL,
    d5_gsr          REAL,
    d5_gcr2         REAL,
    d5_gsr2         REAL,
    d5_gcr_pool     REAL,
    d5_gsr_pool     REAL,
    d_overall       REAL,
    evaluated_at    TEXT,
    PRIMARY KEY (model, frame, scene_id)
);
"""

_DB_MIGRATION_COLUMNS = [
    ("n_gt",          "INTEGER"),
    ("k_inview",      "INTEGER"),
    ("p_pred",        "INTEGER"),
    ("parse_success", "INTEGER"),
    ("rta_match",     "INTEGER"),
    ("d3_pa",         "REAL"),
    ("d3_pe_mean",    "REAL"),
    ("d3_ra",         "REAL"),
    ("d3_re_mean",    "REAL"),
    ("d3_ba",         "REAL"),
    ("d3_iou_mean",   "REAL"),
    ("d5_gsr",        "REAL"),
    ("d5_gcr2",       "REAL"),
    ("d5_gsr2",       "REAL"),
    ("d5_gcr_pool",   "REAL"),
    ("d5_gsr_pool",   "REAL"),
    ("d_overall",     "REAL"),
]


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(DB_SCHEMA)
    conn.commit()
    # Legacy frame label consolidation (PRIMARY KEY column).
    conn.execute(
        "UPDATE scene_results SET frame = ? WHERE frame = ?",
        (EVAL_DB_FRAME_TAG, "room_centered"),
    )
    conn.commit()
    # Idempotent migration: add new columns to existing databases.
    for col_name, col_type in _DB_MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE scene_results ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
    return conn


def _flatten_metrics(
    result: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    if result.get("parse_failed"):
        return {
            "model":         model,
            "frame":         EVAL_DB_FRAME_TAG,
            "scene_id":      result["scene_id"],
            "room_type":     result.get("room_type"),
            "n_gt":          result.get("n_gt"),
            "k_inview":      result.get("k_inview"),
            "p_pred":        0,
            "parse_success": 0,
            "rta_match":     0,
            "d2_nor":        None,
            "d2_ibr":        None,
            "d3_pa":         None,
            "d3_pe_mean":    None,
            "d3_ra":         None,
            "d3_re_mean":    None,
            "d3_ba":         None,
            "d3_iou_mean":   None,
            "d4_rr":         None,
            "d4_nhr":        None,
            "d5_gcr":        None,
            "d5_gsr":        None,
            "d5_gcr2":       None,
            "d5_gsr2":       None,
            "d5_gcr_pool":   None,
            "d5_gsr_pool":   None,
            "d_overall":     None,
            "evaluated_at":  _now_eastern(),
        }

    d14    = result.get("dimensions_1_4", {})
    d5     = result.get("dimension_5", {})
    geo    = d14.get("geometry_spatial_accuracy", {})
    rec    = d14.get("object_recognition", {})
    scn    = d14.get("scene_recognition", {})
    sout   = d14.get("structured_output", {})
    counts = d14.get("global_counts", {})

    # Object counts
    n_gt     = counts.get("N_gt_objects")
    k_inview = counts.get("K_inview_objects")
    p_pred   = counts.get("P_pred_objects")

    # D1 flags (not stored as metric columns — used only for aggregate PSR/RTA computation)
    _psr_raw   = (sout.get("parse_success") or {}).get("parse_success_rate")
    _rta_raw   = (scn.get("room_type_accuracy") or {}).get("room_type_accuracy")
    parse_success = 1 if _psr_raw == 1.0 else 0   # 0 for failures and None
    rta_match     = 1 if _rta_raw == 1.0 else 0   # 0 for wrong type, parse failure, and None

    # D2: Physical Plausibility (denominator = P, total pred count)
    _gi    = d14.get("gt_independent") or {}
    d2_nor = (_gi.get("non_overlap") or {}).get("non_overlap_rate")
    d2_ibr = (_gi.get("in_bounds")   or {}).get("in_bounds_rate")
    # D3: Geometric Accuracy — discrete pass rates (primary) + continuous means (diagnostic)
    _pe  = geo.get("position_accuracy")  or {}
    _re  = geo.get("rotation_accuracy")  or {}
    _iou = geo.get("geometric_accuracy") or {}
    d3_pa       = _pe.get("pa_rate")
    d3_pe_mean  = _pe.get("mean_pe_distance")
    d3_ra       = _re.get("ra_rate")
    d3_re_mean  = _re.get("mean_abs_yaw_error_deg")
    d3_ba       = _iou.get("ba_rate")
    d3_iou_mean = _iou.get("mean_iou")
    # D4: Object Recognition (non-grid only)
    d4_rr  = rec.get("recognition_rate_Km_over_K")
    d4_nhr = rec.get("non_hallucination_rate")

    # D5: Grid Layout (grid only)
    # targets[0] = desk (classroom/office) or library_block (library)
    # targets[1] = chair (classroom/office only; absent for library)
    d5_gcr = d5_gsr = d5_gcr2 = d5_gsr2 = None
    if d5.get("applicable") and d5.get("targets"):
        for idx, t in enumerate(d5["targets"][:2]):
            if not t.get("skip_reason"):
                gcr_val = (t.get("gcr") or {}).get("gcr")
                gsr_val = (t.get("gsr") or {}).get("gsr")
                if idx == 0:
                    d5_gcr, d5_gsr = gcr_val, gsr_val
                else:
                    d5_gcr2, d5_gsr2 = gcr_val, gsr_val

    # Pooled grid metrics (one GCR / one GSR per scene for summary & D_overall).
    _gcr_parts = [x for x in (d5_gcr, d5_gcr2) if x is not None]
    _gsr_parts = [x for x in (d5_gsr, d5_gsr2) if x is not None]
    d5_gcr_pool: Optional[float] = (
        round(sum(_gcr_parts) / len(_gcr_parts), 6) if _gcr_parts else None
    )
    d5_gsr_pool: Optional[float] = (
        round(sum(_gsr_parts) / len(_gsr_parts), 6) if _gsr_parts else None
    )

    # Per-scene overall: mean of applicable D2-D5 rate metrics.
    # N/A metrics (null because K_m=0 or p_pred=0) are excluded from the denominator.
    # Parse-failed scenes produce d_overall = None.
    room_type = result.get("room_type")
    if parse_success and room_type:
        scene_vals: Dict[str, Optional[float]] = {
            "d2_nor": d2_nor, "d2_ibr": d2_ibr,
            "d3_pa":  d3_pa,  "d3_ra":  d3_ra,  "d3_ba": d3_ba,
            "d4_rr":  d4_rr,  "d4_nhr": d4_nhr,
            "d5_gcr_pool": d5_gcr_pool, "d5_gsr_pool": d5_gsr_pool,
        }
        applicable = rate_metric_keys_for_room(room_type, include_d1=False)
        counted = [v for k in applicable if (v := scene_vals.get(k)) is not None]
        d_overall: Optional[float] = round(sum(counted) / len(counted), 6) if counted else None
    else:
        d_overall = None

    return {
        "model":         model,
        "frame":         EVAL_DB_FRAME_TAG,
        "scene_id":      result["scene_id"],
        "room_type":     room_type,
        "n_gt":          n_gt,
        "k_inview":      k_inview,
        "p_pred":        p_pred,
        "parse_success": parse_success,
        "rta_match":     rta_match,
        "d2_nor":        d2_nor,
        "d2_ibr":        d2_ibr,
        "d3_pa":         d3_pa,
        "d3_pe_mean":    d3_pe_mean,
        "d3_ra":         d3_ra,
        "d3_re_mean":    d3_re_mean,
        "d3_ba":         d3_ba,
        "d3_iou_mean":   d3_iou_mean,
        "d4_rr":         d4_rr,
        "d4_nhr":        d4_nhr,
        "d5_gcr":        d5_gcr,
        "d5_gsr":        d5_gsr,
        "d5_gcr2":       d5_gcr2,
        "d5_gsr2":       d5_gsr2,
        "d5_gcr_pool":   d5_gcr_pool,
        "d5_gsr_pool":   d5_gsr_pool,
        "d_overall":     d_overall,
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
    *,
    inview_objects: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    scene_id  = gt_payload.get("scene_id", "unknown")
    room_type = get_room_type(gt_payload)

    if numerical_metrics.is_prediction_unparsed(pred_payload):
        gt_objs = numerical_metrics._extract_objects(gt_payload)
        inv = inview_objects if inview_objects is not None else gt_objs
        return {
            "scene_id":       scene_id,
            "room_type":      room_type,
            "parse_failed":   True,
            "n_gt":           len(gt_objs),
            "k_inview":       len(inv),
            "dimensions_1_4": None,
            "dimension_5":    None,
        }

    if _is_grid_room(room_type):
        # Grid: D1 + D2 + D5 only — skip Hungarian and D3/D4 (see ``object_metrics=False``).
        dim_1_4 = _evaluate_scene_dims_1_4(
            gt_payload,
            pred_payload,
            inview_objects=inview_objects,
            object_metrics=False,
        )
        dim_5 = evaluate_grid(gt_payload, pred_payload)
    else:
        # Non-grid: D1–D4; do not run D5 grid metrics.
        dim_1_4 = _evaluate_scene_dims_1_4(
            gt_payload, pred_payload, inview_objects=inview_objects
        )
        dim_5 = {"applicable": False, "room_type": room_type}

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
) -> Tuple[int, List[str]]:
    """
    Evaluate all scenes for one model; upsert each row to DB immediately.
    Returns (n_evaluated, list_of_missing_scene_dirs).
    """
    all_scene_dirs = discover_scene_dirs(scenes_root)
    have_pred: set  = set()
    missing:   List[str] = []

    for gt_path, pred_path in iter_gt_pred_pairs(
        scenes_root, result_root, model
    ):
        have_pred.add(gt_path.parent.name)
        gt_payload   = _load_json(str(gt_path))
        inview_objs  = _load_inview_objects(gt_path.parent, gt_payload)
        pred_raw     = _load_json(str(pred_path))
        pred_payload, _ = transform_pred_scene(pred_raw, gt_path.parent)
        result = evaluate(gt_payload, pred_payload, inview_objects=inview_objs)
        upsert_row(conn, _flatten_metrics(result, model))
        if result.get("parse_failed"):
            print(
                f"  [{model}] {result['scene_id']} -> upserted "
                f"(unparsed; D2–D5 skipped)",
                file=sys.stderr,
            )
        else:
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
    parser.add_argument(
        "--scenes-root",
        default=None,
        help="Root containing GT scene dirs (default: data/scenes)",
    )
    parser.add_argument(
        "--result-root",
        default=None,
        help=(
            "Prediction tree root (inference_result layout: <room>/<scene>/<model>.json). "
            "If this path ends in .../result or .../inference, "
            "``inference/inference_result`` or ``inference_result`` is appended. "
            "Default: result/inference/inference_result from repo root."
        ),
    )
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
        else resolve_scenes_root(repo)
    )
    if args.result_root:
        result_root = normalize_result_root(Path(args.result_root))
    else:
        result_root = resolve_result_root(repo)

    if not scenes_root.is_dir():
        parser.error(f"Scenes root is not a directory: {scenes_root}")

    # ── Dry-run / batch: prediction scenes must match GT scene folders ──
    if args.dry_run or args.batch:
        try:
            validate_pred_scenes_have_gt_dirs(repo, scenes_root, result_root)
        except ValueError as exc:
            parser.error(str(exc))

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

        db_path     = Path(args.db) if args.db else eval_root / "results.db"
        conn        = init_db(db_path)
        gt_path     = Path(args.gt)
        gt_payload  = _load_json(str(gt_path))
        inview_objs = _load_inview_objects(gt_path.parent, gt_payload)
        pred_raw    = _load_json(args.pred)
        pred_payload, _ = transform_pred_scene(pred_raw, gt_path.parent)
        result      = evaluate(gt_payload, pred_payload, inview_objects=inview_objs)
        upsert_row(conn, _flatten_metrics(result, args.model))
        if result.get("parse_failed"):
            print(
                f"[{args.model}] {result['scene_id']} -> upserted "
                f"(unparsed; D2–D5 skipped)",
                file=sys.stderr,
            )
        else:
            print(f"[{args.model}] {result['scene_id']} -> upserted")
        conn.close()
        return

    # --- Batch mode ---
    if args.all_models and args.model:
        parser.error("--all-models and --model are mutually exclusive")
    if not args.all_models and not args.model:
        parser.error("--batch requires --model or --all-models")

    models = (
        discover_model_stems(result_root)
        if args.all_models
        else [args.model]
    )
    if not models:
        parser.error(f"No prediction JSON files found under {_relpath_from_repo(repo, result_root)}")

    db_path = Path(args.db) if args.db else eval_root / "results.db"
    conn    = init_db(db_path)

    print(f"Models: {models}")
    print(f"DB: {_relpath_from_repo(repo, db_path)}\n")

    # Evaluate each model; upsert results to DB immediately after each scene.
    for model in models:
        print(f"--- {model} ---")
        n_eval, missing = run_batch_for_model(conn, scenes_root, result_root, model)
        print(f"  Done: {n_eval} scene(s) upserted; {len(missing)} missing pred")
        if missing:
            print(f"  Missing pred: {missing[:5]}{'...' if len(missing) > 5 else ''}")
        print()

    # All models done — generate output files from DB (overwrite existing).
    print("Generating output files...")

    export_per_roomtype_json(conn, eval_root, EVAL_DB_FRAME_TAG)
    print(f"  per_roomtype/ -> {_relpath_from_repo(repo, eval_root / 'per_roomtype')}")

    export_summary_json(conn, eval_root, EVAL_DB_FRAME_TAG)
    print(f"  summary.json -> {_relpath_from_repo(repo, eval_root / 'summary.json')}")

    if args.output:
        export_per_model_csv(conn, eval_root, EVAL_DB_FRAME_TAG)
        print(f"  per_model/ -> {_relpath_from_repo(repo, eval_root / 'per_model')}")

    conn.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()