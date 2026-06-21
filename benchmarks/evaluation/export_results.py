"""
export_results.py — Export evaluation results from results.db.

Generates:
  per_roomtype/<room_type>.json   — per-room-type cross-model comparison
  summary.json                    — all models, all metrics, aggregated
  per_model/<model>.csv           — per-scene raw metrics for one model (optional)

Usage:
  python export_results.py
  python export_results.py --db path/to/results.db --out path/to/result/evaluation

D1 note:
  PSR and RTA are aggregate statistics derived from the ``parse_success`` and
  ``rta_match`` integer flag columns in the DB (not from per-scene float columns).
  PSR divides by all scenes (parse failures yield parse_success=0).
  RTA divides by parse-success scenes only (same conditioning as D2–D5 aggregates).
  Per-model CSV omits D1 entirely; they appear only in per_roomtype / summary JSON.

Overall note:
  - Per-scene ``d_overall`` (stored in DB): mean of applicable D2-D5 rate metrics
    (grid rooms use pooled ``d5_gcr_pool`` / ``d5_gsr_pool`` only), None for parse-failed scenes.
  - Aggregate ``D_overall`` (JSON outputs): mean of all applicable rate-metric MEANS
    for that model/room-type, including D1 PSR/RTA.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "configs") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "configs"))
from constants import ROOM_TYPES

_BENCHMARKS_ROOT = str(Path(__file__).resolve().parents[1])
if _BENCHMARKS_ROOT not in sys.path:
    sys.path.insert(0, _BENCHMARKS_ROOT)
from common.metric_registry import (
    METRICS,
    METRIC_META,
    D1_METRIC_META,
    NON_GRID_ROOM_TYPES,
    GRID_ROOM_TYPES,
    rate_metric_keys_for_room,
)

EVAL_DB_FRAME_TAG = "default"

# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------

def _fmt(value: Optional[float], unit: Optional[str]) -> Optional[str]:
    """Format a raw metric value into a human-readable string."""
    if value is None:
        return None
    if unit == "%":
        return f"{value * 100:.1f}%"
    if unit == "m":
        return f"{value:.3f} m"
    if unit == "°":
        return f"{value:.1f}°"
    return f"{value:.4f}"  # dimensionless (e.g. IoU)


def _fmt_stats(stats: Dict[str, Any], unit: Optional[str]) -> Dict[str, Any]:
    """Attach a human-readable ``display`` field (mean with unit) to a _stats dict."""
    out = dict(stats)
    out["display"] = _fmt(stats.get("mean"), unit)
    return out


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_eastern() -> str:
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
# D1 aggregate helpers
# ---------------------------------------------------------------------------

def _d1_for_model(
    conn: sqlite3.Connection,
    model: str,
    frame: str,
    room_type: Optional[str] = None,
) -> Dict[str, Optional[float]]:
    """Return PSR and RTA aggregate values for a model (optionally filtered by room type).

    PSR = SUM(parse_success) / COUNT(*) — all scenes, including parse failures.
    RTA = SUM(rta_match | parse_success=1) / SUM(parse_success) — room type accuracy
    only where structured output is valid (denominator aligns with D2–D5).
    """
    rta_num_sql = (
        "SUM(CASE WHEN parse_success = 1 THEN COALESCE(rta_match, 0) ELSE 0 END)"
    )
    if room_type:
        cur = conn.execute(
            f"SELECT SUM(parse_success), {rta_num_sql}, COUNT(*) "
            "FROM scene_results WHERE model=? AND frame=? AND room_type=?",
            (model, frame, room_type),
        )
    else:
        cur = conn.execute(
            f"SELECT SUM(parse_success), {rta_num_sql}, COUNT(*) "
            "FROM scene_results WHERE model=? AND frame=?",
            (model, frame),
        )
    row = cur.fetchone()
    if not row or row[2] == 0:
        return {"d1_psr": None, "d1_rta": None, "n_total": 0, "n_parsed": 0}
    n_parsed = int(row[0] or 0)
    n_rta_hits = int(row[1] or 0)
    n_total = int(row[2])
    d1_psr = round(n_parsed / n_total, 6)
    d1_rta = (
        round(n_rta_hits / n_parsed, 6) if n_parsed > 0 else None
    )
    return {
        "d1_psr": d1_psr,
        "d1_rta": d1_rta,
        "n_total": n_total,
        "n_parsed": n_parsed,
    }


# ---------------------------------------------------------------------------
# Aggregate-level overall score
# ---------------------------------------------------------------------------

def _aggregate_overall(
    conn: sqlite3.Connection,
    model: str,
    frame: str,
    room_type: Optional[str],
    d1_vals: Dict[str, Optional[float]],
    metric_means: Dict[str, Optional[float]],
) -> Optional[float]:
    """Compute overall score for a model at the aggregate level.

    overall = mean of all applicable rate-metric means (including D1 PSR/RTA).
    The set of applicable rate metrics depends on the room type.
    For summary.json (room_type=None), all rate metrics across all applicable
    room types are included.
    """
    if room_type is not None:
        rate_keys = rate_metric_keys_for_room(room_type, include_d1=True)
    else:
        # Global summary: all rate metrics across their respective applicable rooms.
        # Collect all rate metrics that have a non-None mean for this model.
        all_keys: List[str] = []
        seen: set = set()
        for rt in ROOM_TYPES:
            for k in rate_metric_keys_for_room(rt, include_d1=True):
                if k not in seen:
                    seen.add(k)
                    all_keys.append(k)
        rate_keys = all_keys

    values: List[float] = []
    for k in rate_keys:
        if k in D1_METRIC_META:
            v = d1_vals.get(k)
        else:
            v = metric_means.get(k)
        if v is not None:
            values.append(v)

    return round(sum(values) / len(values), 6) if values else None


# ---------------------------------------------------------------------------
# Export: per_model/*.csv
# ---------------------------------------------------------------------------

_CSV_NA = "N/A"  # sentinel written to CSV cells that are not applicable or not computable


def _csv_metric_value(
    row: Dict[str, Any],
    metric: str,
    room_type: str,
) -> Any:
    """Return the CSV cell value for one metric in one scene row.

    Rules:
      - Metric structurally not applicable to this room type -> N/A
      - Parse succeeded but metric value is None (e.g. K_m=0 for D3) -> N/A
      - All other cases -> raw numeric value (including genuine 0.0)
    """
    meta = METRIC_META[metric]
    apt = meta["applicable_room_types"]
    if apt != "all" and room_type not in apt:
        return _CSV_NA
    v = row.get(metric)
    return _CSV_NA if v is None else v


def export_per_model_csv(
    conn: sqlite3.Connection,
    out_dir: Path,
    frame: str,
) -> None:
    """Write one CSV per model under <out_dir>/per_model/.

    Columns: scene_id, room_type, parse_status, [D2-D5 metrics], d_overall (last).
    D1 columns are excluded — PSR/RTA are aggregate statistics, not per-scene scores.

    parse_status values:
      parse_ok     — output parsed and passed schema validation
      parse_failed — output could not be parsed; all metric cells are blank

    Metric cell values:
      numeric  — valid measurement (including genuine 0.0)
      N/A      — metric not applicable to this room type, OR applicable but
                 not computable (e.g. D3 when no matched pairs exist)
      (blank)  — parse_failed row (entire row is invalid)
    """
    models = get_all_models(conn)
    csv_dir = out_dir / "per_model"
    csv_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = ["scene_id", "room_type", "parse_status"] + METRICS + ["d_overall"]

    for model in models:
        rows = get_scenes_for_model(conn, model, frame)
        if not rows:
            continue

        csv_path = csv_dir / f"{model}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                ok = bool(row.get("parse_success"))
                room_type = row.get("room_type") or ""
                out: Dict[str, Any] = {
                    "scene_id":    row["scene_id"],
                    "room_type":   room_type,
                    "parse_status": "parse_ok" if ok else "parse_failed",
                }
                if ok:
                    for metric in METRICS:
                        out[metric] = _csv_metric_value(row, metric, room_type)
                    out["d_overall"] = row.get("d_overall")
                else:
                    for metric in METRICS:
                        out[metric] = ""
                    out["d_overall"] = ""
                writer.writerow(out)

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

    D1: PSR uses all scenes; RTA uses parse-success scenes only (like D2–D5).
    D2-D5 metrics are averaged over parse-success scenes only (WHERE parse_success=1).
    D_overall is the mean of all applicable rate-metric means (including D1).
    """
    models = get_all_models(conn)
    rt_dir = out_dir / "per_roomtype"
    rt_dir.mkdir(parents=True, exist_ok=True)

    for room_type in ROOM_TYPES:
        metrics_block: Dict[str, Any] = {}
        n_scenes_by_model: Dict[str, Any] = {}
        # Collect per-model metric means for overall computation.
        model_metric_means: Dict[str, Dict[str, Optional[float]]] = {}
        model_d1_vals: Dict[str, Dict[str, Optional[float]]] = {}

        for model in models:
            cur = conn.execute(
                "SELECT COUNT(*) FROM scene_results WHERE model=? AND frame=? AND room_type=?",
                (model, frame, room_type),
            )
            if (cur.fetchone() or [0])[0] == 0:
                continue

            d1 = _d1_for_model(conn, model, frame, room_type=room_type)
            n_scenes_by_model[model] = {"total": d1["n_total"], "parsed": d1["n_parsed"]}
            model_d1_vals[model] = d1
            model_metric_means[model] = {}
            no_parse_data = d1["n_parsed"] == 0

            # D1 metrics block
            for key, meta in D1_METRIC_META.items():
                label = meta["label"]
                unit  = meta.get("unit")
                if label not in metrics_block:
                    metrics_block[label] = {
                        "applicable_room_types": meta["applicable_room_types"],
                        "better": meta.get("better"),
                        "unit":   unit,
                    }
                raw = d1.get(key)
                metrics_block[label][model] = {"value": raw, "display": _fmt(raw, unit)}

            # D2-D5 metrics block (averaged over parse-success scenes only)
            for metric in METRICS:
                meta  = METRIC_META[metric]
                label = meta["label"]
                unit  = meta.get("unit")
                if label not in metrics_block:
                    metrics_block[label] = {
                        "applicable_room_types": meta["applicable_room_types"],
                        "better": meta.get("better"),
                        "unit":   unit,
                    }
                apt = meta["applicable_room_types"]
                not_applicable = apt != "all" and room_type not in apt
                if not_applicable:
                    metrics_block[label][model] = "N/A"
                    model_metric_means[model][metric] = None
                    continue
                if no_parse_data:
                    metrics_block[label][model] = {"note": "no_parse_success"}
                    model_metric_means[model][metric] = None
                    continue

                cur2 = conn.execute(
                    f"SELECT {metric} FROM scene_results "
                    f"WHERE model=? AND frame=? AND room_type=? AND parse_success=1",
                    (model, frame, room_type),
                )
                values = [r[0] for r in cur2.fetchall()]
                if meta["is_ratio"]:
                    v = [x for x in values if x is not None]
                    raw = round(sum(v) / len(v), 6) if v else None
                    metrics_block[label][model] = {"value": raw, "display": _fmt(raw, unit)}
                    model_metric_means[model][metric] = raw
                else:
                    s = _stats(values)
                    if s["mean"] is None:
                        metrics_block[label][model] = {"note": "no_data"}
                    else:
                        metrics_block[label][model] = _fmt_stats(s, unit)
                    model_metric_means[model][metric] = s.get("mean")

        if not n_scenes_by_model:
            continue

        # D_overall block
        overall_block: Dict[str, Any] = {
            "applicable_room_types": "all",
            "better": "↑",
            "unit": "%",
        }
        for model in n_scenes_by_model:
            ov = _aggregate_overall(
                conn, model, frame, room_type,
                model_d1_vals[model], model_metric_means[model],
            )
            overall_block[model] = {"value": ov, "display": _fmt(ov, "%")}
        metrics_block["D_overall"] = overall_block

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
    """Write summary.json — all models x all metrics, aggregated across applicable room types.

    D1: PSR over all scenes; RTA over parse-success scenes only (aligned with D2–D5).
    D2-D5 metrics are averaged over parse-success scenes for each applicable room type.
    D_overall is the mean of all applicable rate-metric means (including D1).
    evaluated_scenes: per-model dict with {total, parsed} counts.
    """
    models  = get_all_models(conn)
    results: Dict[str, Any] = {}

    # Collect per-model overall metric means for D_overall computation.
    model_metric_means: Dict[str, Dict[str, Optional[float]]] = {m: {} for m in models}
    model_d1_vals:      Dict[str, Dict[str, Optional[float]]] = {}

    # D1 metrics
    d1_block_psr: Dict[str, Any] = {
        "applicable_room_types": "all", "better": "↑", "unit": "%",
    }
    d1_block_rta: Dict[str, Any] = {
        "applicable_room_types": "all", "better": "↑", "unit": "%",
    }
    evaluated_scenes: Dict[str, Any] = {}

    for model in models:
        d1 = _d1_for_model(conn, model, frame)
        model_d1_vals[model] = d1
        evaluated_scenes[model] = {"total": d1["n_total"], "parsed": d1["n_parsed"]}
        psr = d1.get("d1_psr")
        rta = d1.get("d1_rta")
        d1_block_psr[model] = {"value": psr, "display": _fmt(psr, "%")}
        d1_block_rta[model] = {"value": rta, "display": _fmt(rta, "%")}

    results[D1_METRIC_META["d1_psr"]["label"]] = d1_block_psr
    results[D1_METRIC_META["d1_rta"]["label"]] = d1_block_rta

    # D2-D5 metrics (averaged over parse-success scenes, applicable room types)
    for metric in METRICS:
        meta  = METRIC_META[metric]
        label = meta["label"]
        unit  = meta.get("unit")
        block: Dict[str, Any] = {
            "applicable_room_types": meta["applicable_room_types"],
            "better": meta.get("better"),
            "unit":   unit,
        }

        for model in models:
            apt = meta["applicable_room_types"]
            # Check if metric is applicable to any room type this model ran on.
            if apt != "all":
                cur_rt = conn.execute(
                    f"SELECT COUNT(*) FROM scene_results "
                    f"WHERE model=? AND frame=? AND parse_success=1 "
                    f"AND room_type IN ({','.join('?' for _ in apt)})",
                    [model, frame] + list(apt),
                )
                applicable_count = (cur_rt.fetchone() or [0])[0]
                if applicable_count == 0 and model_d1_vals[model]["n_parsed"] > 0:
                    # Model has parse successes but none in applicable room types.
                    block[model] = "N/A"
                    model_metric_means[model][metric] = None
                    continue
                if applicable_count == 0:
                    # No parse successes in applicable rooms — could be global parse failure.
                    block[model] = {"note": "no_parse_success"}
                    model_metric_means[model][metric] = None
                    continue

            no_parse_data = model_d1_vals[model]["n_parsed"] == 0
            if no_parse_data and apt == "all":
                block[model] = {"note": "no_parse_success"}
                model_metric_means[model][metric] = None
                continue

            if apt == "all":
                cur = conn.execute(
                    f"SELECT {metric} FROM scene_results "
                    f"WHERE model=? AND frame=? AND parse_success=1",
                    (model, frame),
                )
            else:
                placeholders = ",".join("?" for _ in apt)
                cur = conn.execute(
                    f"SELECT {metric} FROM scene_results "
                    f"WHERE model=? AND frame=? AND parse_success=1 "
                    f"AND room_type IN ({placeholders})",
                    [model, frame] + list(apt),
                )
            values = [r[0] for r in cur.fetchall()]

            if meta["is_ratio"]:
                v = [x for x in values if x is not None]
                raw = round(sum(v) / len(v), 6) if v else None
                block[model] = {"value": raw, "display": _fmt(raw, unit)}
                model_metric_means[model][metric] = raw
            else:
                s = _stats(values)
                if s["mean"] is None:
                    block[model] = {"note": "no_data"}
                else:
                    block[model] = _fmt_stats(s, unit)
                model_metric_means[model][metric] = s.get("mean")

        results[label] = block

    # D_overall block
    overall_block: Dict[str, Any] = {
        "applicable_room_types": "all",
        "better": "↑",
        "unit": "%",
    }
    for model in models:
        ov = _aggregate_overall(
            conn, model, frame, None,
            model_d1_vals[model], model_metric_means[model],
        )
        overall_block[model] = {"value": ov, "display": _fmt(ov, "%")}
    results["D_overall"] = overall_block

    payload = {
        "generated_at":     _now_eastern(),
        "frame":            frame,
        "evaluated_scenes": evaluated_scenes,
        "models":           models,
        "results":          results,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("  summary.json")

# ---------------------------------------------------------------------------
# Schema migration (idempotent — safe to call on every export run)
# ---------------------------------------------------------------------------

def _migrate_db(conn: sqlite3.Connection) -> None:
    """Add parse_success / rta_match / d_overall columns if absent.

    If the legacy d1_psr / d1_rta columns still exist, backfill the new flag
    columns from them so that re-export works without re-running evaluation.
    """
    for col, typ in [("parse_success", "INTEGER"), ("rta_match", "INTEGER"), ("d_overall", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE scene_results ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            pass  # column already exists

    # Check if legacy columns exist for backfill.
    cur = conn.execute("PRAGMA table_info(scene_results)")
    existing_cols = {r[1] for r in cur.fetchall()}
    if "d1_psr" in existing_cols:
        conn.execute(
            "UPDATE scene_results SET parse_success = CASE WHEN d1_psr = 1.0 THEN 1 ELSE 0 END "
            "WHERE parse_success IS NULL"
        )
    if "d1_rta" in existing_cols:
        conn.execute(
            "UPDATE scene_results SET rta_match = CASE WHEN d1_rta = 1.0 THEN 1 ELSE 0 END "
            "WHERE rta_match IS NULL"
        )
    conn.commit()

    for col, typ in [("d5_gcr_pool", "REAL"), ("d5_gsr_pool", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE scene_results ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            pass

    _backfill_d5_pool_from_raw(conn)


def _backfill_d5_pool_from_raw(conn: sqlite3.Connection) -> None:
    """Populate ``d5_gcr_pool`` / ``d5_gsr_pool`` from legacy desk+chair columns when missing."""
    cur = conn.execute("PRAGMA table_info(scene_results)")
    existing = {r[1] for r in cur.fetchall()}
    if "d5_gcr" not in existing or "d5_gcr_pool" not in existing:
        return
    rows = conn.execute(
        "SELECT model, frame, scene_id, d5_gcr, d5_gsr, d5_gcr2, d5_gsr2, "
        "d5_gcr_pool, d5_gsr_pool FROM scene_results"
    ).fetchall()
    for (
        model,
        frame,
        scene_id,
        gcr,
        gsr,
        gcr2,
        gsr2,
        pool_gcr,
        pool_gsr,
    ) in rows:
        gcr_parts = [x for x in (gcr, gcr2) if x is not None]
        gsr_parts = [x for x in (gsr, gsr2) if x is not None]
        new_cr = round(sum(gcr_parts) / len(gcr_parts), 6) if gcr_parts else None
        new_sr = round(sum(gsr_parts) / len(gsr_parts), 6) if gsr_parts else None
        sets: List[str] = []
        vals: List[Any] = []
        if pool_gcr is None and new_cr is not None:
            sets.append("d5_gcr_pool = ?")
            vals.append(new_cr)
        if pool_gsr is None and new_sr is not None:
            sets.append("d5_gsr_pool = ?")
            vals.append(new_sr)
        if not sets:
            continue
        vals.extend([model, frame, scene_id])
        conn.execute(
            f"UPDATE scene_results SET {', '.join(sets)} "
            f"WHERE model=? AND frame=? AND scene_id=?",
            vals,
        )
    conn.commit()


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

    # Idempotent schema migration: add new flag/overall columns if missing,
    # and backfill parse_success / rta_match from legacy d1_psr / d1_rta columns
    # when the latter are still present (allows re-export without re-evaluation).
    _migrate_db(conn)

    print(f"DB:     {db_path.relative_to(repo)}")
    print(f"Output: {out_dir.relative_to(repo)}\n")

    print("Exporting per_roomtype/*.json ...")
    export_per_roomtype_json(conn, out_dir, EVAL_DB_FRAME_TAG)

    print("\nExporting summary.json ...")
    export_summary_json(conn, out_dir, EVAL_DB_FRAME_TAG)

    if args.per_model:
        print("\nExporting per_model/*.csv ...")
        export_per_model_csv(conn, out_dir, EVAL_DB_FRAME_TAG)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
