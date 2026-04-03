"""预处理共享默认值（无 Blender 依赖）。库内模块自此处 ``import``。"""

from __future__ import annotations

BPY_PYTHON: str | None = None
DEFAULT_LOG_FILENAME = "preprocess_last.log"
PREPROCESS_LOG_SEP = "#############################################"
GT_JSON_NAME = "GT.json"
INFO_JSON_FILENAME = "info.json"
CANONICAL_RENDER_PNG = "Image_0_0_0001_0.png"
PUBLISH_RENDER_PNG = "image.png"
