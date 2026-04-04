#!/usr/bin/env python3
"""
Box 单房间预处理 CLI 入口。逻辑见 ``preprocess_lib``。

用法::

    cd IDL3D/generation
    python preprocess.py --blend /path/to/scene_folder/scene.blend

    默认输出 ``GT.json``、``objects_in_view.json``（内含家具重叠 pair_count / 总体积摘要）。

    # 试跑：默认将 INFINIGEN/SCENES_SKETCH 同步到 SCENES2TEST 后批处理（可用 --no-sync-sketch）
    python preprocess.py

    # 正式：SCENES_RAW → INFINIGEN/SCENES_PROCESSED（重叠场景批末移至 rejected_furniture_overlap/）
    python preprocess.py --final --room-type root
    python preprocess.py --final --room-type bedroom
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
