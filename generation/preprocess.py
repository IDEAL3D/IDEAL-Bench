#!/usr/bin/env python3
"""
CLI entry point for single-room box-scene preprocessing.

Delegates to ``preprocess_lib`` (``GT.json``, ``objects_in_view.json``, overlap summaries).
Requires a Python interpreter with Blender's ``bpy`` for per-scene work; the parent
process uses ``preprocess_lib.core`` without importing ``bpy``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GENERATION_DIR = Path(__file__).resolve().parent
if str(_GENERATION_DIR) not in sys.path:
    sys.path.insert(0, str(_GENERATION_DIR))

from preprocess_lib.cli import main

if __name__ == "__main__":
    main()
