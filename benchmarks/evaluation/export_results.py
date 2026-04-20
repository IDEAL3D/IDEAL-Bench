"""
export_results.py — Export evaluation results from results.db.

Generates:
  per_roomtype/<room_type>.json   — per-room-type cross-model comparison
  summary.json                    — all models, all metrics, aggregated
  per_model/<model>.csv           — per-scene raw metrics for one model (optional)

Usage:
  python export_results.py
  python export_results.py --db path/to/results.db --out path/to/result/evaluation
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import canonical room type list from project constants.
# ROOM_TYPES : list[str] — 10 canonical room type ids (no underscores in compounds).
import sys
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "configs") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "configs"))
from constants import ROOM_TYPES

# ---------------------------------------------------------------------------
# Metric applicability — explicit room type lists
# ---------------------------------------------------------------------------
#
# Two distinct evaluation pipelines:
#
#   Non-grid scenes (living space + meetingroom):
#     D1 (PE/RE/IOU/OOR/OOB) + D2 (RR/HR) + D3 + D4
#     Object matching via match_objects(); full geometry evaluation.
#
#   Grid scenes (classroom / library / office):
#     D3 + D4 + D5 only.
#     No object matching; layout evaluated via grid metrics.
#     D1 and D2 are not applicable — they are set to null in output files.

_NON_GRID_ROOM_TYPES: List[str] = sorted([
    "bedroom", "homestudio", "livingroom",
    "kitchen", "bathroom", "diningroom",
    "meetingroom",
])

_GRID_ROOM_TYPES: List[str] = sorted([
    "classroom", "library", "office",
])

# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

# Ordered list of all 12 DB column names.
METRICS: List[str] = [
    "d1_pe", "d1_re", "d1_iou",
    "d1_oor", "d1_oob",
    "d2_rr", "d2_hr",
    "d3_acc",
    "d4_fail",
    "d5_grid", "d5_in_grid_pe", "d5_yaw",
]

# Metadata for each metric used when building summary.json and per_roomtype/*.json.
# applicable_room_types: "all" | list[str] of canonical room type ids
# is_ratio: True  → single scalar (mean only); False → {mean, median, min, max}
METRIC_META: Dict[str, Dict[str, Any]] = {
    "d1_pe": {
        "label": "D1_pe",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d1_re": {
        "label": "D1_re",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d1_iou": {
        "label": "D1_iou",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d1_oor": {
        "label": "D1_oor",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d1_oob": {
        "label": "D1_oob",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d2_rr": {
        "label": "D2_rr",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d2_hr": {
        "label": "D2_hr",
        "applicable_room_types": _NON_GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d3_acc": {
        "label": "D3_acc",
        "applicable_room_types": "all",
        "is_ratio": True,
    },
    "d4_fail": {
        "label": "D4_fail",
        "applicable_room_types": "all",
        "is_ratio": True,
    },
    "d5_grid": {
        "label": "D5_grid",
        "applicable_room_types": _GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d5_in_grid_pe": {
        "label": "D5_in_grid_pe",
        "applicable_room_types": _GRID_ROOM_TYPES,
        "is_ratio": False,
    },
    "d5_yaw": {
        "label": "D5_yaw",
        "applicable_room_types": _GRID_ROOM_TYPES,
        "is_ratio": False,
    },
}

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_eastern() -> str:
    """Current US Eastern time (fixed UTC-4 EDT) as ISO-8601 string."""
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).isoformat(timespec="seconds")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_all_models(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT DISTINCT model FROM scene_results ORDER BY model")
    return [r[0] for r in cur.fetchall()]


def get_scenes_for_model(
    conn: sqlite3.Connection, model: str, frame: str
) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM scene_results WHERE model=? AND frame=? ORDER BY scene_id",
        (model, frame),
    )
    return _rows_to_dicts(cur)

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _stats(values: List[Optional[float]]) -> Dict[str, Optional[float]]:
    """Compute mean/median/min/max over non-None values."""
    v = [x for x in values if x is not None]
    if not v:
        return {"mean": None, "median": None, "min": None, "max": None}
    v_sorted = sorted(v)
    n = len(v_sorted)
    median = (
        v_sorted[n // 2]
        if n % 2 == 1
        else (v_sorted[n // 2 - 1] + v_sorted[n // 2]) / 2
    )
    return {
        "mean":   round(sum(v) / n, 6),
        "median": round(median, 6),
        "min":    round(v_sorted[0], 6),
        "max":    round(v_sorted[-1], 6),
    }

# ---------------------------------------------------------------------------
# Export: per_model/*.csv
# ---------------------------------------------------------------------------

def export_per_model_csv(
    conn: sqlite3.Connection,
    out_dir: Path,
    frame: str,
) -> None:
    """Write one CSV per model under <out_dir>/per_model/. Each row is one scene."""
    models = get_all_models(conn)
    csv_dir = out_dir / "per_model"
    csv_dir.mkdir(parents=True, exist_ok=True)

    for model in models:
        rows = get_scenes_for_model(conn, model, frame)
        if not rows:
            continue

        csv_path = csv_dir / f"{model}.csv"
        fieldnames = ["scene_id", "room_type"] + METRICS

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k) for k in fieldnames})

        print(f"  per_model/{model}.csv ({len(rows)} scenes)")

# ---------------------------------------------------------------------------
# Export: per_roomtype/*.json
# ---------------------------------------------------------------------------

def export_per_roomtype_json(
    conn: sqlite3.Connection,
    out_dir: Path,
    frame: str,
) -> None:
    """Write one JSON per room type under <out_dir>/per_roomtype/.

    Only room types that have at least one row in the DB are written.
    Room type strings come from the canonical ROOM_TYPES list in constants.py.
    """
    models = get_all_models(conn)
    rt_dir = out_dir / "per_roomtype"
    rt_dir.mkdir(parents=True, exist_ok=True)

    for room_type in ROOM_TYPES:
        metrics_block: Dict[str, Any] = {}
        n_scenes_by_model: Dict[str, int] = {}

        for model in models:
            cur = conn.execute(
                "SELECT * FROM scene_results WHERE model=? AND frame=? AND room_type=?",
                (model, frame, room_type),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                continue
            n_scenes_by_model[model] = len(rows)

            for metric in METRICS:
                meta  = METRIC_META[metric]
                label = meta["label"]
                if label not in metrics_block:
                    metrics_block[label] = {
                        "applicable_room_types": meta["applicable_room_types"]
                    }
                apt = meta["applicable_room_types"]
                not_applicable = apt != "all" and room_type not in apt
                if not_applicable:
                    # Metric does not apply to this room type — write null.
                    metrics_block[label][model] = None
                    continue
                values = [r.get(metric) for r in rows]
                if meta["is_ratio"]:
                    v = [x for x in values if x is not None]
                    metrics_block[label][model] = round(sum(v) / len(v), 6) if v else None
                else:
                    metrics_block[label][model] = _stats(values)

        if not n_scenes_by_model:
            continue  # No data for this room type — skip file entirely.

        payload = {
            "room_type": room_type,
            "frame":     frame,
            "n_scenes":  n_scenes_by_model,
            "metrics":   metrics_block,
        }
        json_path = rt_dir / f"{room_type}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  per_roomtype/{room_type}.json")

# ---------------------------------------------------------------------------
# Export: summary.json
# ---------------------------------------------------------------------------

def export_summary_json(
    conn: sqlite3.Connection,
    out_dir: Path,
    frame: str,
) -> None:
    """Write summary.json — all models × all metrics, aggregated across applicable room types."""
    models  = get_all_models(conn)
    results: Dict[str, Any] = {}

    for metric in METRICS:
        meta  = METRIC_META[metric]
        label = meta["label"]
        block: Dict[str, Any] = {"applicable_room_types": meta["applicable_room_types"]}

        for model in models:
            apt = meta["applicable_room_types"]
            if apt == "all":
                cur = conn.execute(
                    f"SELECT {metric} FROM scene_results WHERE model=? AND frame=?",
                    (model, frame),
                )
            else:
                placeholders = ",".join("?" for _ in apt)
                cur = conn.execute(
                    f"SELECT {metric} FROM scene_results "
                    f"WHERE model=? AND frame=? AND room_type IN ({placeholders})",
                    [model, frame] + list(apt),
                )
            values = [r[0] for r in cur.fetchall()]

            if meta["is_ratio"]:
                v = [x for x in values if x is not None]
                block[model] = round(sum(v) / len(v), 6) if v else None
            else:
                block[model] = _stats(values)

        results[label] = block

    cur = conn.execute(
        "SELECT model, COUNT(*) FROM scene_results WHERE frame=? GROUP BY model",
        (frame,),
    )
    evaluated_scenes = dict(cur.fetchall())

    payload = {
        "generated_at":    _now_eastern(),
        "frame":           frame,
        "evaluated_scenes": evaluated_scenes,
        "models":          models,
        "results":         results,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("  summary.json")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export IDEAL evaluation results from results.db."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (default: <IDEAL>/result/evaluation/results.db)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory (default: <IDEAL>/result/evaluation)",
    )
    parser.add_argument(
        "--frame",
        default="camera",
        choices=("floor", "camera"),
        help="Coordinate frame to export (default: camera)",
    )
    parser.add_argument(
        "--per-model",
        action="store_true",
        help="Also write per_model/*.csv (default: summary + per_roomtype only)",
    )
    args = parser.parse_args()

    repo    = _REPO_ROOT
    db_path = (
        Path(args.db).expanduser().resolve()
        if args.db
        else repo / "result" / "evaluation" / "results.db"
    )
    out_dir = (
        Path(args.out).expanduser().resolve()
        if args.out
        else repo / "result" / "evaluation"
    )

    if not db_path.is_file():
        print(f"Error: database not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"DB:     {db_path.relative_to(repo)}")
    print(f"Output: {out_dir.relative_to(repo)}")
    print(f"Frame:  {args.frame}\n")

    print("Exporting per_roomtype/*.json ...")
    export_per_roomtype_json(conn, out_dir, args.frame)

    print("\nExporting summary.json ...")
    export_summary_json(conn, out_dir, args.frame)

    if args.per_model:
        print("\nExporting per_model/*.csv ...")
        export_per_model_csv(conn, out_dir, args.frame)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()