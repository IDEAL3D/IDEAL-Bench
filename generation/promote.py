#!/usr/bin/env python3
"""
将 SCENES_PROCESSED 根目录下的场景目录**移动**到 IDL3D/data/scenes_v1/<类别>/<scene_id>/。

- 场景目录名形如 ``bedroom_s955131720``（与 generate 的 ``{房型去连字符}_s{seed}`` 一致）：房型前缀决定目标子文件夹。
- 默认跳过 ``manifests/``、``DEPRECATED/``、``0_DEPRECATED/``、非目录、以及无法解析的名称。
- 少数前缀可映射到更短的数据集类别名（见 ``PREFIX_TO_CATEGORY``）；未映射则**原样**作为子文件夹名。

每次成功 promote（非 dry-run）会在 ``<dest>/manifests/`` 下追加：

- ``promote_batches.jsonl``：一行一条批次（promote_batch_id、数量、首尾 scene_id、时间、源/目的根路径）。
- ``promote_scenes.csv``：每场景一行（全局递增 ``index``、promote_batch_id、scene_id）。

用法::

    cd IDL3D/generation
    conda activate infinigen
    
    python promote.py --dry-run # 查看将要移动的场景,不会实际移动
    python promote.py # 移动场景
    python promote.py --overwrite # 如果目标场景目录存在,则删除它然后移动
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_PROCESSED = REPO_ROOT / "SCENES_PROCESSED"
DEFAULT_SCENES_V1 = REPO_ROOT / "data" / "scenes_v1"

PREFIX_TO_CATEGORY: dict[str, str] = {
    "openoffice": "office",
}

SKIP_TOP_LEVEL_NAMES = frozenset(
    {"manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"}
)


def parse_scene_dir_name(name: str) -> tuple[str, str] | None:
    if "_s" not in name:
        return None
    prefix, seed = name.rsplit("_s", 1)
    if not prefix or not seed.isdigit():
        return None
    return prefix, seed


def category_for_prefix(prefix: str) -> str:
    return PREFIX_TO_CATEGORY.get(prefix, prefix)


def iter_scene_dirs(processed_root: Path) -> list[Path]:
    out: list[Path] = []
    if not processed_root.is_dir():
        return out
    for p in sorted(processed_root.iterdir()):
        if not p.is_dir():
            continue
        if p.name in SKIP_TOP_LEVEL_NAMES or p.name.startswith("."):
            continue
        if parse_scene_dir_name(p.name) is None:
            continue
        out.append(p)
    return out


def _next_promote_batch_id(manifests_dir: Path) -> str:
    prefix = "promote_batch_"
    p = manifests_dir / "promote_batches.jsonl"
    max_n = 0
    if p.is_file():
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = rec.get("promote_batch_id", "")
                if isinstance(bid, str) and bid.startswith(prefix):
                    suf = bid[len(prefix) :]
                    if suf.isdigit():
                        max_n = max(max_n, int(suf))
    return f"{prefix}{max_n + 1:03d}"


def _next_global_index(csv_path: Path) -> int:
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        return 1
    max_i = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "index" not in reader.fieldnames:
            return 1
        for row in reader:
            try:
                max_i = max(max_i, int(str(row.get("index", "0")).strip() or 0))
            except ValueError:
                pass
    return max_i + 1


def _append_promote_batches_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_promote_scenes_csv(path: Path, rows: list[tuple[int, str, str]]) -> None:
    """rows: (index, promote_batch_id, scene_id)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["index", "promote_batch_id", "scene_id"])
        for idx, bid, sid in rows:
            w.writerow([idx, bid, sid])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move SCENES_PROCESSED scene folders into IDL3D/data/scenes_v1/<category>/."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"SCENES_PROCESSED root (default: {DEFAULT_PROCESSED})",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help=f"scenes_v1 root (default: {DEFAULT_SCENES_V1})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print moves only; do not move.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="If destination scene dir exists, remove it then move.",
    )
    args = parser.parse_args()

    src_root = (args.source or DEFAULT_PROCESSED).resolve()
    dst_root = (args.dest or DEFAULT_SCENES_V1).resolve()

    if not src_root.is_dir():
        print(f"[ERROR] --source is not a directory: {src_root}", file=sys.stderr)
        sys.exit(2)

    scenes = iter_scene_dirs(src_root)
    if not scenes:
        print(f"[promote] no scene directories under {src_root} (skipped manifests/DEPRECATED).")
        sys.exit(0)

    print(f"[promote] source: {src_root}")
    print(f"[promote] dest:   {dst_root}")
    if args.dry_run:
        print("[promote] DRY-RUN — no files moved.\n")

    errors = 0
    moved_scene_ids: list[str] = []

    for scene_dir in scenes:
        prefix, _seed = parse_scene_dir_name(scene_dir.name)
        if prefix is None:
            continue
        category = category_for_prefix(prefix)
        cat_dir = dst_root / category
        target = cat_dir / scene_dir.name

        if target.exists() and not args.overwrite:
            print(
                f"[SKIP] exists (use --overwrite): {target}",
                file=sys.stderr,
            )
            errors += 1
            continue

        if args.dry_run:
            print(f"  {scene_dir.name}  ->  {category}/")
            continue

        cat_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(scene_dir), str(target))
        print(f"[OK] {scene_dir.name} -> {target}")
        moved_scene_ids.append(scene_dir.name)

    if args.dry_run:
        print(f"\n[promote] would move {len(scenes)} scene(s). Run without --dry-run to apply.")
        print(
            "[promote] dry-run: would create/update "
            f"{dst_root / 'manifests' / 'promote_batches.jsonl'} "
            f"and promote_scenes.csv after successful moves."
        )
        sys.exit(0)

    if not moved_scene_ids:
        print("[promote] no scenes moved (all skipped or empty).")
        sys.exit(1 if errors else 0)

    manifests_dir = dst_root / "manifests"
    batches_path = manifests_dir / "promote_batches.jsonl"
    scenes_csv_path = manifests_dir / "promote_scenes.csv"

    promote_batch_id = _next_promote_batch_id(manifests_dir)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    first_sid = moved_scene_ids[0]
    last_sid = moved_scene_ids[-1]

    record = {
        "promote_batch_id": promote_batch_id,
        "num_scenes": len(moved_scene_ids),
        "first_scene_id": first_sid,
        "last_scene_id": last_sid,
        "timestamp": ts,
        "source_root": str(src_root),
        "dest_root": str(dst_root),
    }
    _append_promote_batches_jsonl(batches_path, record)

    start_idx = _next_global_index(scenes_csv_path)
    csv_rows: list[tuple[int, str, str]] = []
    for j, sid in enumerate(moved_scene_ids):
        csv_rows.append((start_idx + j, promote_batch_id, sid))
    _append_promote_scenes_csv(scenes_csv_path, csv_rows)

    print(f"[promote] manifest -> {batches_path} ({promote_batch_id}, n={len(moved_scene_ids)})")
    print(f"[promote] manifest -> {scenes_csv_path} (rows {start_idx}..{start_idx + len(moved_scene_ids) - 1})")

    if errors:
        print(
            f"[WARN] {errors} path(s) were skipped; manifest only lists successful moves.",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
