"""Shared preprocessing defaults (no Blender dependency). Imported by other modules in this package."""

from __future__ import annotations

BPY_PYTHON: str | None = None
DEFAULT_LOG_FILENAME = "preprocess_last.log"
PREPROCESS_LOG_SEP = "#############################################"
GT_JSON_NAME = "GT.json"
# In-view asset manifest for eval / inference; distinct from Infinigen ``metadata.json``.
OBJECTS_IN_VIEW_JSON = "objects_in_view.json"
# Overlap summary lives on ``objects_in_view.json`` top level (no standalone furniture_overlap.json).
FURNITURE_OVERLAP_PAIR_COUNT_KEY = "furniture_overlap_pair_count"
FURNITURE_OVERLAP_VOLUME_TOTAL_KEY = "furniture_overlap_volume_m3_total"
# Legacy sidecar; removed by the new pipeline when present; still readable for old scenes.
FURNITURE_OVERLAP_JSON = "furniture_overlap.json"
# Under INFINIGEN/SCENES_PROCESSED, sibling to accepted scenes: whole scene dirs moved here on overlap reject.
PROCESSED_REJECT_SUBDIR = "rejected_furniture_overlap"
# Under ``--scenes-root`` (e.g. data/scenes_raw): invalid scenes moved here by ``filter_scenes.py``.
FILTER_INVALID_SUBDIR = "invalid"
# ``filter_scenes.py`` append-only logs under ``--scenes-root``.
FILTER_SCENES_REPORT_CSV = "filter_scenes_report.csv"
FILTER_SCENES_BATCHES_JSONL = "filter_scenes_batches.jsonl"
INSTANCE_SEGMENTATION_PNG = "instance_segmentation.png"
METADATA_JSON = "metadata.json"

# ---------------------------------------------------------------------------
# inview.py filter thresholds (second-stage preprocessing)
# ---------------------------------------------------------------------------

# Filter 2: Aspect Ratio — max(Δx/Δy, Δy/Δx) of the object's 2D bounding box
# in instance map pixel space.  Objects with extreme perspective distortion
# (e.g. a shelf seen nearly edge-on) will have a very high ratio.
# Range to test: 10–15.  Change here and re-run inview.py.
ASPECT_RATIO_MAX: float = 10.0

# Filter 3: Truncation — ratio of actual instance-map pixels (occluded) to
# theoretically projected pixels (mesh silhouette, no occlusion, unbounded).
# Objects below this threshold are heavily truncated at the camera edge.
TRUNCATION_RATIO_MIN: float = 0.25

# inview.py batch log filename (written alongside objects_in_view.json in scenes-root)
INVIEW_BATCH_LOG_JSONL = "inview_batch.jsonl"