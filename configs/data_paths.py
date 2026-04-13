"""Central path constants: ``IDEAL/data/`` dataset trees and Infinigen locations.

- **Dataset outputs** (local vs HF mirror): ``SCENES_*_LOCAL`` / ``SCENES_*_HF`` under ``DATA_ROOT``.
- **Infinigen code**: ``INFINIGEN_SUBMODULE_ROOT`` (``infinigen-ideal/``); subprocess cwd for ``infinigen_examples``.
- **Generated scenes (canonical)**: ``SCENES_RAW_ROOT`` == ``SCENES_RAW_LOCAL`` → ``IDEAL/data/scenes_raw``
  (manifests live in ``data/scenes_raw/manifests/``). Preprocess dev sync may still use a sibling ``INFINIGEN/``
  tree for ``SCENES2TEST`` / ``SCENES_PROCESSED`` (sibling ``INFINIGEN/`` checkout).

Git: ``data/`` is tracked via ``data/.gitkeep``; other contents under ``data/`` are ignored — root ``.gitignore``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
DATA_ROOT: Path = REPO_ROOT / "data"

# --- Local / HF dataset roots (under IDEAL/data) ---

SCENES_RAW_LOCAL: Path = DATA_ROOT / "scenes_raw"
SCENES_RAW_HF: Path = DATA_ROOT / "scenes_raw_hf"
SCENES_PROCESSED_LOCAL: Path = DATA_ROOT / "scenes_processed"
SCENES_PROCESSED_HF: Path = DATA_ROOT / "scenes_processed_hf"

# --- Infinigen submodule (code + ``python -m infinigen_examples`` cwd) ---

INFINIGEN_SUBMODULE_ROOT: Path = REPO_ROOT / "infinigen-ideal"

# --- Canonical raw scene output (same path as ``SCENES_RAW_LOCAL``; shared name for imports) ---

SCENES_RAW_ROOT: Path = SCENES_RAW_LOCAL
