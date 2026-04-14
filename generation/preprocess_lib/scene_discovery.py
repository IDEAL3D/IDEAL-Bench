"""Discover ``scene.blend`` paths under a batch root (flat or one room-type nesting level)."""

from __future__ import annotations

import os
from typing import List

from .defaults import FILTER_INVALID_SUBDIR

_TOP_SKIP = frozenset(
    {"manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"}
) | {FILTER_INVALID_SUBDIR}

_INNER_SKIP = frozenset(
    {"invalid", "manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"}
)


def discover_scene_blend_paths(scenes_root: str) -> List[str]:
    """Return sorted ``scene.blend`` paths.

    Resolution order (first non-empty wins):

    1. **Flat:** ``<root>/<scene_id>/scene.blend``
    2. **Nested:** ``<root>/<room_or_group>/<scene_id>/scene.blend`` (one extra level only)
    3. **Single file:** ``<root>/scene.blend``

    Skips ``invalid``, ``manifests``, ``DEPRECATED``, dot dirs. Same ``scenes-root`` is used for
    logs and ``invalid/`` moves in ``filter_scenes.py`` regardless of nesting depth.
    """
    root = os.path.abspath(os.path.expanduser(scenes_root))
    if not os.path.isdir(root):
        return []

    # --- Level 1: root/<scene_id>/scene.blend ---
    out1: List[str] = []
    for name in sorted(os.listdir(root)):
        if name in _TOP_SKIP or name.startswith("."):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        bp = os.path.join(d, "scene.blend")
        if os.path.isfile(bp):
            out1.append(bp)
    if out1:
        return out1

    # --- Level 2: root/<category>/<scene_id>/scene.blend ---
    out2: List[str] = []
    for name in sorted(os.listdir(root)):
        if name in _TOP_SKIP or name.startswith("."):
            continue
        d1 = os.path.join(root, name)
        if not os.path.isdir(d1):
            continue
        try:
            sub = sorted(os.listdir(d1))
        except OSError:
            continue
        for name2 in sub:
            if name2 in _INNER_SKIP or name2.startswith("."):
                continue
            d2 = os.path.join(d1, name2)
            if not os.path.isdir(d2):
                continue
            bp = os.path.join(d2, "scene.blend")
            if os.path.isfile(bp):
                out2.append(bp)
    if out2:
        return sorted(out2)

    direct = os.path.join(root, "scene.blend")
    if os.path.isfile(direct):
        return [direct]
    return []
