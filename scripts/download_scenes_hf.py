#!/usr/bin/env python3
"""Pull IDEAL scene assets from Hugging Face into ``IDEAL/data/scenes``.

Default dataset repo: ``cyn-xyz/ideal_scenes_preprocessed`` (see
https://huggingface.co/datasets/cyn-xyz/ideal_scenes_preprocessed).

Requires::

    pip install huggingface_hub

The Hugging Face *dataset* tree is mirrored under ``--destination``. If your
checkpoint layout differs (e.g. an extra wrapping directory), merge or symlink
until you have::

    data/scenes/<room_type>/<scene_id>/{GT.json, objects_in_view.json, image.png, …}

Authenticate with ``huggingface-cli login`` when the snapshot is gated.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


DEFAULT_HF_REPO = "cyn-xyz/ideal_scenes_preprocessed"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(
        description=f"Download {DEFAULT_HF_REPO} (or --repo-id) into data/scenes."
    )
    p.add_argument(
        "--repo-id",
        default=DEFAULT_HF_REPO,
        help="HF dataset repo id",
    )
    p.add_argument(
        "--revision",
        default=None,
        help="Branch, tag, or commit (default: HF default revision)",
    )
    p.add_argument(
        "--destination",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Scene root (default: {_repo_root() / 'data' / 'scenes'})",
    )
    p.add_argument(
        "--token",
        default=None,
        help="HF token (optional; otherwise env HUGGINGFACE_HUB_TOKEN or cached login)",
    )
    args = p.parse_args()

    dest = (
        Path(args.destination).expanduser().resolve()
        if args.destination
        else _repo_root() / "data" / "scenes"
    )
    dest.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "Missing dependency: huggingface_hub\n"
            "  pip install huggingface_hub",
            file=sys.stderr,
        )
        return 2

    print(f"snapshot_download(repo_id={args.repo_id!r}, repo_type=dataset, local_dir={dest})")
    path = snapshot_download(
        repo_id=args.repo_id,
        repo_type="dataset",
        local_dir=str(dest),
        revision=args.revision,
        token=args.token,
    )
    print(f"done: files under {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
