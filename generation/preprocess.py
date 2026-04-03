#!/usr/bin/env python3
"""
Box 单房间预处理 CLI 入口。共享常量见 ``preprocess_lib.defaults``；逻辑见 ``preprocess_lib``。

用法::

    cd IDL3D/generation
    python preprocess.py --blend /path/to/scene_folder/scene.blend
    python preprocess.py
    python preprocess.py --final --room-type bedroom
"""

from __future__ import annotations

from preprocess_lib.cli import main

if __name__ == "__main__":
    main()
