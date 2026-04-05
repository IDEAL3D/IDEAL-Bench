"""IDEAL 内魔改 Infinigen 子模块根路径（``INFINIGEN_SUBMODULE_ROOT``，内含 ``infinigen_examples`` 等）。"""

from __future__ import annotations

from pathlib import Path

# IDEAL 仓库根（configs/ 的父目录）
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# 与 IDEAL 同级的 INFINIGEN 仓库根：场景输出 SCENES_SKETCH / SCENES_RAW 放在此树下（非 IDEAL 仓库内）
INFINIGEN_REPO_ROOT: Path = REPO_ROOT.parent / "INFINIGEN"
SCENES_SKETCH_ROOT: Path = INFINIGEN_REPO_ROOT / "SCENES_SKETCH"
SCENES_RAW_ROOT: Path = INFINIGEN_REPO_ROOT / "SCENES_RAW"

# 本管线统一使用的 Infinigen 克隆（Git 子模块路径见下；如需更名请同步改此处字符串与 .gitmodules）
INFINIGEN_SUBMODULE_ROOT: Path = REPO_ROOT / "infinigen-ideal"
