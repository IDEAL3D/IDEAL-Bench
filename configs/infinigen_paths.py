"""IDL3D 内 Infinigen 源码根目录：``<IDL3D>/infinigen-idl3d``（含 ``infinigen_examples``）。"""

from __future__ import annotations

from pathlib import Path

# IDL3D 仓库根（configs/ 的父目录）
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# 与 IDL3D 同级的 INFINIGEN 仓库根：场景输出 SCENES_SKETCH / SCENES_RAW 放在此树下（非 IDL3D 内）
INFINIGEN_REPO_ROOT: Path = REPO_ROOT.parent / "INFINIGEN"
SCENES_SKETCH_ROOT: Path = INFINIGEN_REPO_ROOT / "SCENES_SKETCH"
SCENES_RAW_ROOT: Path = INFINIGEN_REPO_ROOT / "SCENES_RAW"

# 本数据集管线统一使用的 Infinigen 克隆
INFINIGEN_IDL3D_ROOT: Path = REPO_ROOT / "infinigen-idl3d"
