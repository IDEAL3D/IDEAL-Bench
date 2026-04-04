"""预处理共享默认值（无 Blender 依赖）。库内模块自此处 ``import``。"""

from __future__ import annotations

BPY_PYTHON: str | None = None
DEFAULT_LOG_FILENAME = "preprocess_last.log"
PREPROCESS_LOG_SEP = "#############################################"
GT_JSON_NAME = "GT.json"
# 镜头内资产侧车（评测 / 推理）；不与 Infinigen ``metadata.json`` 混淆
OBJECTS_IN_VIEW_JSON = "objects_in_view.json"
# 重叠摘要写在 objects_in_view.json 顶层（不再写单独的 furniture_overlap.json）
FURNITURE_OVERLAP_PAIR_COUNT_KEY = "furniture_overlap_pair_count"
FURNITURE_OVERLAP_VOLUME_TOTAL_KEY = "furniture_overlap_volume_m3_total"
# 遗留侧文件：新管线会删除；仍可从旧场景读取
FURNITURE_OVERLAP_JSON = "furniture_overlap.json"
# INFINIGEN/SCENES_PROCESSED 下与合格场景平级：本批筛出的家具重叠场景整目录移入此文件夹
PROCESSED_REJECT_SUBDIR = "rejected_furniture_overlap"
INSTANCE_SEGMENTATION_PNG = "instance_segmentation.png"
METADATA_JSON = "metadata.json"
