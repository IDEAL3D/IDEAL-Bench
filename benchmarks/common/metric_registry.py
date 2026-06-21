"""
metric_registry.py — Single source of truth for all evaluation metric definitions.

Defines:
  NON_GRID_ROOM_TYPES      — room types evaluated with D1-D4
  GRID_ROOM_TYPES          — room types evaluated with D1-D2 + D5
  METRICS                  — ordered list of D2-D5 DB column names (no D1)
  METRIC_META              — per-metric metadata for D2-D5 metrics
  D1_METRIC_META           — metadata for D1 aggregate flags (PSR, RTA)
  rate_metric_keys_for_room() — applicable rate-metric keys for a room type

D1 design note:
  PSR and RTA are aggregate statistics, not per-scene scores.  They are derived from
  two DB flag columns (``parse_success`` INT and ``rta_match`` INT) rather than from
  floating-point metric columns.  D1 entries live in D1_METRIC_META and are handled
  separately by ``export_results._d1_for_model``; they do not appear in METRICS or
  METRIC_META.

  PSR divides by **all** evaluated scenes (parse failures contribute 0 to the numerator).
  RTA divides by **parse-success** scenes only (same conditioning as D2–D5 aggregates),
  so a parse failure penalises PSR but does not also deflate RTA.

Import from here in any script that needs to enumerate or describe metrics:
  evaluate.py, export_results.py, benchmarks/analysis scripts, plot scripts, etc.

All primary metrics use ``better`` as exported to ``summary.json`` (often up-arrow).
Continuous means may remain lower-is-better in this registry when raw units favour
lower values; ``benchmarks/analysis/main_table/main_table.py`` table / radar read ``better`` from
``summary.json`` first, so manual edits there override these defaults.
"""
from __future__ import annotations

from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Room type groups
# ---------------------------------------------------------------------------
#
# Two distinct evaluation pipelines:
#
#   Non-grid scenes (living space + meetingroom):
#     D1 (RTA/PSR) + D2 (NOR/IBR) + D3 (PA/GA/RA) + D4 (RR/NHR)
#     Object matching via match_objects(); full geometry evaluation.
#
#   Grid scenes (classroom / library / office):
#     D1 (RTA/PSR) + D2 (NOR/IBR) + D5 pooled GCR/GSR (per-scene mean of desk/block + chair).
#     No object matching; layout evaluated via grid metrics.
#     D3 and D4 are not applicable — they are set to null in output files.

NON_GRID_ROOM_TYPES: List[str] = sorted([
    "bedroom", "homestudio", "livingroom",
    "kitchen", "bathroom", "diningroom",
    "meetingroom",
])

GRID_ROOM_TYPES: List[str] = sorted([
    "classroom", "library", "office",
])

# ---------------------------------------------------------------------------
# D1 metric metadata (aggregate-only — not stored as per-scene DB metric columns)
# ---------------------------------------------------------------------------
#
# PSR and RTA do not share the same denominator: parse-failed scenes count toward
# PSR's divisor (with 0 in the numerator) but are excluded from RTA's divisor.
#
# Derived at export time (``export_results._d1_for_model``) from per-scene flags:
#   parse_success (INT 0/1) -> D1_PSR = SUM(parse_success) / COUNT(*)
#   rta_match     (INT 0/1) -> D1_RTA = SUM(rta_match WHERE parse_success=1)
#                                     / SUM(parse_success)

D1_METRIC_META: Dict[str, Dict[str, Any]] = {
    "d1_psr": {
        "label": "D1_psr",
        "applicable_room_types": "all",
        "unit": "%",
        "better": "↑",
    },
    "d1_rta": {
        "label": "D1_rta",
        "applicable_room_types": "all",
        "unit": "%",
        "better": "↑",
    },
}

# ---------------------------------------------------------------------------
# Metric column names (D2-D5 only; stored per scene in the DB)
# ---------------------------------------------------------------------------
#
# D3 has both discrete pass-rate columns (primary, into paper table)
# and continuous mean columns (diagnostic/supplementary).

METRICS: List[str] = [
    "d2_nor", "d2_ibr",
    "d3_pa",  "d3_pe_mean",
    "d3_ra",  "d3_re_mean",
    "d3_ba",  "d3_iou_mean",
    "d4_rr",  "d4_nhr",
    # Grid scenes: per-scene mean of desk/block GCR/GSR and chair GCR/GSR when present;
    # library uses only the first group → pool equals block metrics.
    "d5_gcr_pool",  "d5_gsr_pool",
]

# ---------------------------------------------------------------------------
# Per-metric metadata (D2-D5)
# ---------------------------------------------------------------------------
#
#   label                 : display name used in output JSON / CSV / plots
#   applicable_room_types : "all" | list[str]
#   is_ratio              : True  -> single mean across scenes (e.g. RTA is a scene-level 0/1)
#                           False -> {mean, median, min, max} across scenes
#   unit                  : "%" -> raw value in [0,1], display as XX.X%
#                           "m" -> metres
#                           "deg" -> degrees
#                           None -> dimensionless, display as 4 d.p. (e.g. IoU)
#   better                : "↑" -> higher is better  |  "↓" -> lower is better
#   threshold             : the pass/fail threshold used during computation (informational)

METRIC_META: Dict[str, Dict[str, Any]] = {
    # D2: Physical Plausibility — applies to all scenes
    "d2_nor": {
        "label": "D2_nor",
        "applicable_room_types": "all",
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
    },
    "d2_ibr": {
        "label": "D2_ibr",
        "applicable_room_types": "all",
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
    },
    # D3: Geometric Accuracy — non-grid scenes only
    # Discrete pass rates (primary, into paper table):
    #   pass = matched pair is within the threshold
    "d3_pa": {
        "label": "D3_pa",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
        "threshold": "0.3 m",
    },
    "d3_ra": {
        "label": "D3_ra",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
        "threshold": "30 deg",
    },
    "d3_ba": {
        "label": "D3_ba",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
        "threshold": "IoU >= 0.15",
    },
    # Continuous means (supplementary/diagnostic)
    "d3_pe_mean": {
        "label": "D3_pe_mean",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "m",
        "better": "↓",
    },
    "d3_re_mean": {
        "label": "D3_re_mean",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "°",
        "better": "↓",
    },
    "d3_iou_mean": {
        "label": "D3_iou_mean",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": None,
        "better": "↑",
    },
    # D4: Object Recognition — non-grid scenes only
    "d4_rr": {
        "label": "D4_rr",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
    },
    "d4_nhr": {
        "label": "D4_nhr",
        "applicable_room_types": NON_GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
    },
    # D5: Grid Layout — grid scenes only (one summary metric each; no desk/chair split).
    "d5_gcr_pool": {
        "label": "D5_gcr",
        "applicable_room_types": GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
        "note": (
            "Mean of desk/block and chair GCR when both exist; library = block group only."
        ),
    },
    "d5_gsr_pool": {
        "label": "D5_gsr",
        "applicable_room_types": GRID_ROOM_TYPES,
        "is_ratio": False,
        "unit": "%",
        "better": "↑",
        "note": (
            "Mean of desk/block and chair GSR when both exist; library = block group only."
        ),
    },
}

assert set(METRICS) == set(METRIC_META), (
    "METRICS list and METRIC_META keys are out of sync"
)

# ---------------------------------------------------------------------------
# Rate-metric helpers
# ---------------------------------------------------------------------------

def rate_metric_keys_for_room(room_type: str, *, include_d1: bool = False) -> List[str]:
    """Return ordered rate-metric keys applicable to *room_type*.

    Rate metrics: unit="%" and better="up".
    D1 metrics are included only when ``include_d1=True`` (for aggregate-level overall).
    D3/D4 are excluded for grid rooms; D5 is excluded for non-grid rooms.
    """
    keys: List[str] = []
    if include_d1:
        for key, meta in D1_METRIC_META.items():
            apt = meta["applicable_room_types"]
            if apt == "all" or room_type in apt:
                keys.append(key)
    for key, meta in METRIC_META.items():
        if meta.get("unit") != "%" or meta.get("better") != "↑":
            continue
        apt = meta["applicable_room_types"]
        if apt == "all" or room_type in apt:
            keys.append(key)
    return keys
