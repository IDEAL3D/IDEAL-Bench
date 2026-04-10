#!/usr/bin/env python3
"""
Print or export a ``CUDA_VISIBLE_DEVICES`` value (auto pick or explicit).

Uses ``configs.gpu_select.resolve_cuda_visible_devices`` (same logic as ``generate.py`` / ``preprocess.py``).

Examples (from IDEAL repo root; quote paths because of ``[`` ``]``)::

    python '[INTERNAL]tools/gpu_selecting.py'
    python '[INTERNAL]tools/gpu_selecting.py' --gpu 2
    python '[INTERNAL]tools/gpu_selecting.py' --gpu auto
    eval $(python '[INTERNAL]tools/gpu_selecting.py' --export)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.gpu_select import resolve_cuda_visible_devices  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve CUDA_VISIBLE_DEVICES (auto via nvidia-smi or explicit GPU id(s))."
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help="Same as generation/preprocess --gpu: index(es) or 'auto'. Default: env IDEAL_GPU / inherit / auto.",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Print shell: export CUDA_VISIBLE_DEVICES=... (for eval)",
    )
    args = parser.parse_args()
    val = resolve_cuda_visible_devices(args.gpu)
    if args.export:
        # shell-safe single line
        print(f'export CUDA_VISIBLE_DEVICES="{val}"')
    else:
        print(val)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
