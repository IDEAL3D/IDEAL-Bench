"""
发现场景、子进程循环、manifest、汇总日志（父进程不导入 bpy）。

与 ``pipeline`` 分工：本模块负责「怎么跑、跑哪些、跑完写什么」。
"""

from __future__ import annotations

import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .defaults import (
    BPY_PYTHON,
    DEFAULT_LOG_FILENAME,
    FURNITURE_OVERLAP_JSON,
    FURNITURE_OVERLAP_PAIR_COUNT_KEY,
    FURNITURE_OVERLAP_VOLUME_TOTAL_KEY,
    GT_JSON_NAME,
    OBJECTS_IN_VIEW_JSON,
    PREPROCESS_LOG_SEP,
    PROCESSED_REJECT_SUBDIR,
)

_REPO_ROOT_FOR_CONFIG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT_FOR_CONFIG not in sys.path:
    sys.path.insert(0, _REPO_ROOT_FOR_CONFIG)
from configs.constant import ROOM_TYPES

ROOM_TYPES_SET: frozenset[str] = frozenset(ROOM_TYPES)
FINAL_SCOPE_ROOT = "root"


def _generation_dir() -> str:
    """``IDL3D/generation``（``preprocess_lib`` 的父目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _list_png_files_in_scene_dir(scene_dir: str) -> List[str]:
    """场景目录内（非递归）所有 ``.png`` 文件路径，按文件名排序（大小写不敏感）。"""
    if not os.path.isdir(scene_dir):
        return []
    out: List[str] = []
    try:
        for name in os.listdir(scene_dir):
            if not name.lower().endswith(".png"):
                continue
            p = os.path.join(scene_dir, name)
            if os.path.isfile(p):
                out.append(p)
    except OSError:
        return []
    out.sort(key=lambda x: os.path.basename(x).lower())
    return out


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _infinigen_repo_root() -> str:
    """与 IDL3D 同级的 ``INFINIGEN`` 仓库根（SCENES_SKETCH / SCENES_RAW 所在树）。"""
    return os.path.normpath(os.path.join(_repo_root(), "..", "INFINIGEN"))


def _scenes_sketch_root() -> str:
    return os.path.join(_infinigen_repo_root(), "SCENES_SKETCH")


def _scenes2test_root() -> str:
    return os.path.join(_infinigen_repo_root(), "SCENES2TEST")


def _raw_manifests_dir() -> str:
    return os.path.join(_infinigen_repo_root(), "SCENES_RAW", "manifests")


def _processed_root() -> str:
    """预处理工作根：``INFINIGEN/SCENES_PROCESSED``（与 RAW 同仓库）。"""
    return os.path.join(_infinigen_repo_root(), "SCENES_PROCESSED")


def _processed_manifests_dir() -> str:
    d = os.path.join(_processed_root(), "manifests")
    os.makedirs(d, exist_ok=True)
    return d


def _raw_scenes_csv_path(room_type: str) -> str:
    return os.path.join(_raw_manifests_dir(), f"{room_type}_scenes.csv")


def _room_folder_name(room_type: str) -> str:
    """与 generate 一致：目录名房型段（无连字符），用于 ``SCENES_RAW/<folder>/``。"""
    return room_type.replace("-", "")


def _parse_scene_dir_name(name: str) -> Optional[Tuple[str, str]]:
    """``bedroom_s955131720`` -> ``(bedroom, 955131720)``；不符合则 ``None``。"""
    if "_s" not in name:
        return None
    prefix, seed = name.rsplit("_s", 1)
    if not prefix or not seed.isdigit():
        return None
    return prefix, seed


def _raw_scene_flat_dir(raw_root: str, scene_id: str) -> Optional[str]:
    """``--final`` 拷贝/校验：仅 ``<raw_root>/<scene_id>/`` 根目录平铺（不含 ``<raw_root>/<房型>/`` 下已入库副本）。"""
    flat = os.path.join(raw_root, scene_id)
    if os.path.isdir(flat):
        return flat
    return None


_RAW_ROOT_SKIP_DIR_NAMES = frozenset(
    {"manifests", "DEPRECATED", "0_DEPRECATED", "README.txt", ".DS_Store"}
)

# SCENES_PROCESSED 根下列目录不应当作 ``<scene_id>/scene.blend`` 参与批扫描
_PROCESSED_TOPLEVEL_SKIP_DIRS = frozenset(
    {
        "manifests",
        PROCESSED_REJECT_SUBDIR,
        "DEPRECATED",
        "0_DEPRECATED",
    }
)


def _raw_room_subdirs_to_skip_for_flat_scan() -> frozenset[str]:
    """RAW 根下列为各房型子目录（``SCENES_RAW/bedroom`` 等），非 ``scene_id`` 平铺项。"""
    return frozenset(_room_folder_name(rt) for rt in ROOM_TYPES)


def _final_scope_normalized(room_type_arg: str) -> str:
    s = room_type_arg.strip().lower()
    if s == FINAL_SCOPE_ROOT:
        return FINAL_SCOPE_ROOT
    canonical = room_type_arg.strip()
    if canonical in ROOM_TYPES_SET:
        return canonical
    raise ValueError(
        f"无效 --room-type {room_type_arg!r}：用 {FINAL_SCOPE_ROOT!r} 表示 RAW 根目录平铺场景，"
        f"或为 {list(ROOM_TYPES)} 之一以处理 SCENES_RAW/<房型>/ 下子目录。"
    )


def _discover_scene_ids_under_raw(scope: str) -> List[str]:
    """``scope==root``：RAW 根下平铺、且前缀属于 ``ROOM_TYPES`` 的 ``*_s*`` 目录。

    否则：``SCENES_RAW/<房型>/`` 下一层子目录中，合法 ``scene_id`` 且房型前缀与 ``scope`` 一致者。
    """
    raw_root = os.path.join(_infinigen_repo_root(), "SCENES_RAW")
    skip_nested = _raw_room_subdirs_to_skip_for_flat_scan()
    found: set[str] = set()

    if scope == FINAL_SCOPE_ROOT:
        if not os.path.isdir(raw_root):
            return []
        for name in sorted(os.listdir(raw_root)):
            if name in _RAW_ROOT_SKIP_DIR_NAMES or name.startswith("."):
                continue
            if name in skip_nested:
                continue
            p = os.path.join(raw_root, name)
            if not os.path.isdir(p):
                continue
            parsed = _parse_scene_dir_name(name)
            if parsed is None:
                continue
            prefix, _seed = parsed
            if prefix in ROOM_TYPES_SET:
                found.add(name)
        return sorted(found)

    sub = os.path.join(raw_root, _room_folder_name(scope))
    if not os.path.isdir(sub):
        return []
    for name in sorted(os.listdir(sub)):
        if name.startswith("."):
            continue
        p = os.path.join(sub, name)
        if not os.path.isdir(p):
            continue
        parsed = _parse_scene_dir_name(name)
        if parsed is None:
            continue
        prefix, _seed = parsed
        if prefix == scope:
            found.add(name)
    return sorted(found)


def _raw_scene_dir_for_final(scope: str, scene_id: str) -> Optional[str]:
    """``--final`` 拷贝/校验：``root`` 为 ``SCENES_RAW/<scene_id>``，否则 ``SCENES_RAW/<房型>/<scene_id>``。"""
    raw_root = os.path.join(_infinigen_repo_root(), "SCENES_RAW")
    if scope == FINAL_SCOPE_ROOT:
        return _raw_scene_flat_dir(raw_root, scene_id)
    sub = os.path.join(raw_root, _room_folder_name(scope), scene_id)
    if os.path.isdir(sub):
        return sub
    return None


def _load_scene_id_to_batch_csv(room_type: str) -> Dict[str, str]:
    """若存在 ``*_scenes.csv``，返回 scene_id -> batch_id；否则空 dict。"""
    path = _raw_scenes_csv_path(room_type)
    if not os.path.isfile(path):
        return {}
    out: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = (row.get("scene_id") or "").strip()
            if sid:
                out[sid] = (row.get("batch_id") or "").strip()
    return out


def _preprocess_scenes_csv_path(room_type: str, manifests_dir: str | None = None) -> str:
    md = manifests_dir or _processed_manifests_dir()
    return os.path.join(md, f"{room_type}_preprocess_scenes.csv")


def _preprocess_batches_jsonl_path(manifests_dir: str | None = None) -> str:
    md = manifests_dir or _processed_manifests_dir()
    return os.path.join(md, "preprocess_batches.jsonl")


def _next_preprocess_batch_id(manifests_dir: str, room_type: str) -> str:
    prefix = f"{room_type}_preprocess_batch_"
    p = _preprocess_batches_jsonl_path(manifests_dir)
    max_n = 0
    if os.path.isfile(p):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = rec.get("preprocess_batch_id", "")
                if isinstance(bid, str) and bid.startswith(prefix):
                    suf = bid[len(prefix) :]
                    if suf.isdigit():
                        max_n = max(max_n, int(suf))
    return f"{prefix}{max_n + 1:03d}"


def _append_preprocess_batches_jsonl(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_gt_asset_count(scene_dir: str) -> Optional[int]:
    """``GT.json`` → ``scene.room.asset_count``；失败时 ``None``。"""
    path = os.path.join(scene_dir, GT_JSON_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sc = data.get("scene") or {}
        room = sc.get("room") or {}
        ac = room.get("asset_count")
        if ac is None:
            return None
        return int(ac)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _read_info_asset_count_in_view(scene_dir: str) -> Optional[int]:
    """``objects_in_view.json``（或旧 ``info.json``）→ ``objects_in_view_count`` / ``asset_count_in_view``。"""
    for fname in (OBJECTS_IN_VIEW_JSON, "info.json"):
        path = os.path.join(scene_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        v = data.get("objects_in_view_count")
        if v is None:
            v = data.get("asset_count_in_view")
        if v is None:
            continue
        return int(v)
    return None


def _batch_avg_asset_metrics(blend_paths: List[str]) -> Tuple[Optional[float], Optional[float]]:
    """
    返回 ``(avg_gt_asset_count, avg_info_asset_count_in_view)``。
    若任一场景缺少可读字段则返回 ``(None, None)``。
    """
    if not blend_paths:
        return None, None
    n = len(blend_paths)
    sum_gt = 0
    sum_iv = 0
    for bp in blend_paths:
        sd = os.path.dirname(os.path.abspath(bp))
        g = _read_gt_asset_count(sd)
        v = _read_info_asset_count_in_view(sd)
        if g is None or v is None:
            return None, None
        sum_gt += g
        sum_iv += v
    return round(sum_gt / n, 6), round(sum_iv / n, 6)


def _finalize_preprocess_batch_jsonl(
    manifests_dir: str,
    preprocess_batch_id: str,
    *,
    num_processed: int,
    end_seed_inclusive: int,
    num_scenes_with_overlap: int,
    avg_gt_asset_count: Optional[float],
    avg_info_asset_count_in_view: Optional[float],
) -> None:
    p = _preprocess_batches_jsonl_path(manifests_dir)
    if not os.path.isfile(p):
        return
    raw_lines = []
    with open(p, encoding="utf-8") as f:
        raw_lines = f.read().splitlines()
    out: list[str] = []
    for line in raw_lines:
        if not line.strip():
            out.append(line)
            continue
        rec = json.loads(line)
        if rec.get("preprocess_batch_id") == preprocess_batch_id:
            rec["num_processed"] = num_processed
            rec["num_scenes_with_overlap"] = num_scenes_with_overlap
            rec["end_seed_inclusive"] = end_seed_inclusive
            rec["avg_gt_asset_count"] = avg_gt_asset_count
            rec["avg_info_asset_count_in_view"] = avg_info_asset_count_in_view
            # 不写 room_type / scenes_root：房型已含于 preprocess_batch_id 前缀；根目录固定为 SCENES_PROCESSED。
            ordered = {
                "preprocess_batch_id": rec["preprocess_batch_id"],
                "num_to_process": rec.get("num_to_process"),
                "num_processed": rec["num_processed"],
                "num_scenes_with_overlap": rec["num_scenes_with_overlap"],
                "start_seed": rec.get("start_seed"),
                "end_seed_inclusive": rec["end_seed_inclusive"],
                "timestamp": rec.get("timestamp"),
                "avg_gt_asset_count": rec["avg_gt_asset_count"],
                "avg_info_asset_count_in_view": rec["avg_info_asset_count_in_view"],
            }
            out.append(json.dumps(ordered, ensure_ascii=False))
        else:
            out.append(line)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, p)


def _seed_from_scene_id(scene_id: str) -> int:
    if "_s" not in scene_id:
        raise ValueError(f"cannot parse seed from scene_id: {scene_id!r}")
    _, tail = scene_id.rsplit("_s", 1)
    return int(tail)


def _load_raw_scene_rows(
    room_type: str, raw_batch_ids: Optional[List[str]]
) -> List[dict]:
    """从 RAW 房型 CSV 取行。``raw_batch_ids`` 为 None 或空列表时取**全部**行；否则只保留列出的 batch_id。"""
    path = _raw_scenes_csv_path(room_type)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"missing RAW scene list: {path}")
    want: Optional[set[str]] = None
    if raw_batch_ids:
        want = set(raw_batch_ids)
    out: List[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = (row.get("scene_id") or "").strip()
            if not sid:
                continue
            if want is None:
                out.append(row)
            elif row.get("batch_id") in want:
                out.append(row)
    if not out:
        if want is not None:
            raise ValueError(f"no rows in {path} with batch_id in {sorted(want)}")
        raise ValueError(f"no rows in {path}")
    return out


def _append_preprocess_scene_csv_row(
    csv_path: str,
    *,
    batch_scene_index: int,
    preprocess_batch_id: str,
    scene_id: str,
    status: str,
    overlap_count: int,
    raw_batch_id: str,
) -> None:
    header = [
        "batch_scene_index",
        "preprocess_batch_id",
        "scene_id",
        "status",
        "overlap_count",
        "raw_batch_id",
    ]
    write_header = not os.path.isfile(csv_path) or os.stat(csv_path).st_size == 0
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerow(
            [
                batch_scene_index,
                preprocess_batch_id,
                scene_id,
                status,
                overlap_count,
                raw_batch_id,
            ]
        )


def _assert_raw_scenes_ready_for_final(scope: str, scene_ids: List[str]) -> None:
    """``--final`` 写 manifest / 拷贝前检查：含 ``scene.blend`` 与至少一个 ``.png``（文件名不限）。"""
    bad: List[str] = []
    for sid in scene_ids:
        src = _raw_scene_dir_for_final(scope, sid)
        if not src:
            bad.append(f"{sid} (无场景目录)")
            continue
        blend = os.path.join(src, "scene.blend")
        if not os.path.isfile(blend):
            bad.append(f"{sid} (缺 scene.blend @ {src})")
        elif not _list_png_files_in_scene_dir(src):
            bad.append(f"{sid} (缺任意 .png 渲染图 @ {src})")
    if not bad:
        return
    loc = (
        "SCENES_RAW/<scene_id>/（平铺）"
        if scope == FINAL_SCOPE_ROOT
        else f"SCENES_RAW/{_room_folder_name(scope)}/<scene_id>/"
    )
    raise ValueError(
        f"SCENES_RAW 中下列场景未就绪（期望 {loc}，"
        "且同时包含 scene.blend 与至少一个 .png）：\n  "
        + "\n  ".join(bad)
    )


def _copy_raw_scenes_to_processed(
    scene_ids: List[str], *, scope: str, overwrite: bool
) -> None:
    proc_root = _processed_root()
    os.makedirs(proc_root, exist_ok=True)
    for sid in scene_ids:
        src = _raw_scene_dir_for_final(scope, sid)
        if not src:
            raise FileNotFoundError(f"missing RAW scene directory for {sid}")
        dst = os.path.join(proc_root, sid)
        if os.path.exists(dst):
            if not overwrite:
                raise FileExistsError(
                    f"processed scene already exists (use --overwrite-processed): {dst}"
                )
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"[final] copied {sid} -> INFINIGEN/SCENES_PROCESSED/")


def _default_log_path() -> str:
    """与默认批处理目录一致：``INFINIGEN/SCENES2TEST`` 下。"""
    return os.path.join(_scenes2test_root(), DEFAULT_LOG_FILENAME)


def _sync_sketch_to_scenes2test(*, overwrite: bool) -> None:
    """将 ``SCENES_SKETCH/<scene_id>``（含 ``scene.blend`` 的子目录）复制到 ``SCENES2TEST/<scene_id>``。"""
    sketch = _scenes_sketch_root()
    dest_root = _scenes2test_root()
    if not os.path.isdir(sketch):
        print(f"[sync] skip: SCENES_SKETCH not a directory: {sketch}")
        return
    os.makedirs(dest_root, exist_ok=True)
    n_copy = 0
    for name in sorted(os.listdir(sketch)):
        if name.startswith("."):
            continue
        src = os.path.join(sketch, name)
        if not os.path.isdir(src):
            continue
        if not os.path.isfile(os.path.join(src, "scene.blend")):
            continue
        dst = os.path.join(dest_root, name)
        if os.path.exists(dst):
            if not overwrite:
                continue
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"[sync] SCENES_SKETCH/{name} -> SCENES2TEST/{name}")
        n_copy += 1
    print(
        f"[sync] sketch→test: {n_copy} copied"
        + (" (overwrite on)" if overwrite else ", skipped existing dirs")
    )


def _overlap_pair_count_from_info_dir(
    scene_dir: str, semantics_enabled: bool
) -> Optional[int]:
    """从 ``objects_in_view.json`` 顶层字段、或遗留 ``furniture_overlap.json`` / ``info.json`` 读取 pair_count。"""
    if not semantics_enabled:
        return None
    p_ovie = os.path.join(scene_dir, OBJECTS_IN_VIEW_JSON)
    if os.path.isfile(p_ovie):
        try:
            with open(p_ovie, encoding="utf-8") as f:
                ov = json.load(f)
        except (OSError, json.JSONDecodeError):
            ov = None
        if isinstance(ov, dict):
            n = ov.get(FURNITURE_OVERLAP_PAIR_COUNT_KEY)
            if isinstance(n, int) and n >= 0:
                return n

    p_side = os.path.join(scene_dir, FURNITURE_OVERLAP_JSON)
    if os.path.isfile(p_side):
        try:
            with open(p_side, encoding="utf-8") as f:
                fo = json.load(f)
        except (OSError, json.JSONDecodeError):
            fo = {}
        if isinstance(fo, dict) and fo.get("computed") is True:
            return int(fo.get("pair_count", 0))

    p_legacy = os.path.join(scene_dir, "info.json")
    if not os.path.isfile(p_legacy):
        return None
    try:
        with open(p_legacy, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    fo = data.get("furniture_overlap") or {}
    if not isinstance(fo, dict) or fo.get("computed") is not True:
        return None
    return int(fo.get("pair_count", 0))


def _overlap_volume_m3_total_from_scene_dir(scene_dir: str) -> Optional[Any]:
    """从 ``objects_in_view.json`` 读取重叠总体积（若存在）。"""
    p_ovie = os.path.join(scene_dir, OBJECTS_IN_VIEW_JSON)
    if not os.path.isfile(p_ovie):
        return None
    try:
        with open(p_ovie, encoding="utf-8") as f:
            ov = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(ov, dict):
        return None
    return ov.get(FURNITURE_OVERLAP_VOLUME_TOTAL_KEY)


def _write_overlap_scene_logs(
    *,
    entries: List[Dict[str, Any]],
    out_dir: str,
) -> None:
    """写入 ``overlap_scenes.csv``（固定名）；每有一个重叠场景追加 **一行**。

    ``entries`` 每项含 ``preprocess_batch_id``, ``scene_id``, ``overlap_count``,
    ``overlap_volume_m3_total``（可选）。
    """
    if not entries:
        return
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "overlap_scenes.csv")

    header = [
        "preprocess_batch_id",
        "scene_id",
        "overlap_pair_count",
        "overlap_volume_m3_total",
    ]

    data_rows: List[List[str]] = []

    for ent in entries:
        bid = str(ent.get("preprocess_batch_id") or "")
        sid = str(ent.get("scene_id") or "")
        oc = int(ent.get("overlap_count") or 0)
        vol_total = ent.get("overlap_volume_m3_total")
        vol_s = "" if vol_total is None else str(vol_total)
        data_rows.append([bid, sid, str(oc), vol_s])

    need_header = (not os.path.isfile(csv_path)) or os.path.getsize(csv_path) == 0
    mode = "a" if not need_header else "w"
    with open(csv_path, mode, newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(header)
        w.writerows(data_rows)

    print(f"[overlap log] appended {len(data_rows)} row(s) to {csv_path}")


def _append_furniture_overlap_rejects_jsonl(
    manifests_dir: str, entries: List[Dict[str, Any]]
) -> None:
    """逐行追加 ``furniture_overlap_rejects.jsonl``（scene_id + pair_count + 总体积，供审计）。"""
    if not entries:
        return
    os.makedirs(manifests_dir, exist_ok=True)
    path = os.path.join(manifests_dir, "furniture_overlap_rejects.jsonl")
    ts = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            rec = {
                "timestamp": ts,
                "preprocess_batch_id": e.get("preprocess_batch_id"),
                "scene_id": e.get("scene_id"),
                "overlap_count": e.get("overlap_count"),
                "overlap_volume_m3_total": e.get("overlap_volume_m3_total"),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[overlap] appended {len(entries)} line(s) -> {path}")


def _quarantine_overlap_scenes(
    proc_root: str, entries: List[Dict[str, Any]], *, reject_subdir: str
) -> None:
    """将 ``entries`` 中场景目录从 ``proc_root/<scene_id>`` 移至 ``proc_root/<reject_subdir>/``。"""
    if not entries:
        return
    dest_parent = os.path.join(proc_root, reject_subdir)
    os.makedirs(dest_parent, exist_ok=True)
    seen: set[str] = set()
    for e in entries:
        sid = str(e.get("scene_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        src = os.path.join(proc_root, sid)
        if not os.path.isdir(src):
            print(f"[quarantine] skip (not a dir): {src}")
            continue
        dst = os.path.join(dest_parent, sid)
        if os.path.exists(dst):
            raise FileExistsError(
                f"quarantine target already exists: {dst} "
                f"(remove or rename before re-running batch)"
            )
        shutil.move(src, dst)
        print(f"[quarantine] {sid} -> {reject_subdir}/")


def _maybe_append_overlap_log_entry(
    bucket: List[Dict[str, Any]],
    *,
    scene_dir: str,
    scene_id: str,
    preprocess_batch_id: str,
    opc: Optional[int],
) -> None:
    if opc is None or opc <= 0:
        return
    vol = _overlap_volume_m3_total_from_scene_dir(scene_dir)
    bucket.append(
        {
            "preprocess_batch_id": preprocess_batch_id,
            "scene_id": scene_id,
            "overlap_count": opc,
            "overlap_volume_m3_total": vol,
        }
    )


def _append_preprocess_log_block(log_path: str, lines: List[str]) -> None:
    log_dir = os.path.dirname(os.path.abspath(log_path))
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    text = "\n".join(lines) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(text)
    print(f"[preprocess] summary log -> {log_path}")


def _format_preprocess_report(
    *,
    batch_id: str,
    finished_at_iso: str,
    elapsed_sec: float,
    scenes_total: int,
    ok_count: int,
    fail_count: int,
    skipped_count: int = 0,
    overlap_positive: List[Tuple[str, int]],
    failure_entries: List[Tuple[str, str]],
) -> List[str]:
    """写入 ``preprocess_last.log`` 的单次运行块（单场景或批量）；正文为英文、ASCII 标点。"""
    lines = [
        PREPROCESS_LOG_SEP,
        f"- Batch id: {batch_id}",
        f"- Finished at: {finished_at_iso} - Elapsed: {elapsed_sec:.2f}s",
        f"- Scenes total: {scenes_total} - OK: {ok_count} - Failed: {fail_count} - Skipped: {skipped_count}",
        "- Scenes with furniture overlap (pair_count > 0):",
    ]
    if overlap_positive:
        for label, pc in overlap_positive:
            lines.append(f"  - {label} (pair_count={pc})")
    else:
        lines.append("  - (none)")
    lines.append("- Scenes with other failures:")
    if failure_entries:
        for label, reason in failure_entries:
            lines.append(f"  - {label}: {reason}")
    else:
        lines.append("  - (none)")
    lines.append(PREPROCESS_LOG_SEP)
    return lines


def _format_log_single(result: Dict[str, Any], elapsed_sec: float) -> List[str]:
    """``--blend`` 单场景：一块报告，``scenes_total=1``。"""
    ok = bool(result.get("ok"))
    ok_c = 1 if ok else 0
    fail_c = 0 if ok else 1
    overlaps: List[Tuple[str, int]] = []
    if ok:
        opc = result.get("overlap_pair_count")
        if opc is not None and opc > 0:
            overlaps.append((str(result.get("scene_label", "?")), int(opc)))
    failures: List[Tuple[str, str]] = []
    if not ok:
        lab = str(result.get("scene_label", "?"))
        failures.append((lab, str(result.get("error") or "preprocess failed")))
    return _format_preprocess_report(
        batch_id=uuid.uuid4().hex[:16],
        finished_at_iso=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=elapsed_sec,
        scenes_total=1,
        ok_count=ok_c,
        fail_count=fail_c,
        skipped_count=0,
        overlap_positive=overlaps,
        failure_entries=failures,
    )


def _find_bpy_python() -> str:
    if BPY_PYTHON and os.path.isfile(BPY_PYTHON):
        return BPY_PYTHON
    candidate = os.path.normpath(
        os.path.join(
            _generation_dir(), "..", "..", "miniconda", "envs", "infinigen", "bin", "python"
        )
    )
    if os.path.isfile(candidate):
        return candidate
    return sys.executable

def _discover_blends_box(scenes_root: str) -> List[str]:
    """先扫 ``root/<子目录>/scene.blend``；若没有，再试 ``root/scene.blend``（单场景目录）。"""
    out: List[str] = []
    if not os.path.isdir(scenes_root):
        return out
    for name in sorted(os.listdir(scenes_root)):
        if name in _PROCESSED_TOPLEVEL_SKIP_DIRS or name.startswith("."):
            continue
        d = os.path.join(scenes_root, name)
        if not os.path.isdir(d):
            continue
        bp = os.path.join(d, "scene.blend")
        if os.path.isfile(bp):
            out.append(bp)
    if out:
        return out
    direct = os.path.join(scenes_root, "scene.blend")
    if os.path.isfile(direct):
        return [direct]
    return out


def _run_batch(
    scenes_root: str | None,
    extra_argv: List[str] | None,
    *,
    semantics_enabled: bool,
    log_path: Optional[str],
    skip_if_gt: bool,
    blend_paths: Optional[List[str]] = None,
    manifest_ctx: Optional[Dict[str, Any]] = None,
) -> int:
    """返回进程退出码：全部成功为 0，否则为 1。

    当 ``skip_if_gt`` 为 False（用户传了 ``--force``）时，对每个子进程追加 ``--force``，
    否则子进程会因已有 ``GT.json`` 立即退出且仍返回 0，导致假成功。

    若给定 ``blend_paths``，则只处理这些 ``scene.blend`` 路径（顺序即处理顺序），
    不再从 ``scenes_root`` 自动发现。

    ``manifest_ctx`` 非空时，每场景向 ``{room_type}_preprocess_scenes.csv`` 追加一行，
    批末更新 ``preprocess_batches.jsonl`` 中对应批次的 ``num_processed`` / ``end_seed_inclusive``。
    """
    bpy_python = _find_bpy_python()
    generation_dir = _generation_dir()
    root = scenes_root or _scenes2test_root()
    if blend_paths is not None:
        blends = [os.path.abspath(p) for p in blend_paths]
    else:
        blends = _discover_blends_box(root)
    if not blends:
        print(
            f"[WARN] No scene.blend under {root} (expected */scene.blend or ./scene.blend)."
        )
        return 0

    pairs: List[Tuple[str, str]] = []
    if manifest_ctx:
        pairs = list(manifest_ctx["scene_raw_batch_pairs"])
        if len(pairs) != len(blends):
            raise ValueError(
                f"manifest scene_raw_batch_pairs length {len(pairs)} != blends {len(blends)}"
            )

    script = os.path.join(generation_dir, "preprocess.py")
    extra = list(extra_argv or [])
    print(f"[INFO] scenes_root={root}  {len(blends)} scene(s)")
    print(f"[INFO] Python with bpy: {bpy_python}")
    if extra:
        print(f"[INFO] extra args: {extra}")

    t0 = time.perf_counter()
    failures: List[Tuple[str, str, int]] = []
    overlap_positive: List[Tuple[str, int]] = []
    ok_count = 0
    skipped_count = 0
    overlap_scene_count = 0
    loop_completed = False
    overlap_log_entries: List[Dict[str, Any]] = []

    try:
        for i, bp in enumerate(blends, 1):
            label = os.path.basename(os.path.dirname(bp))
            scene_dir = os.path.dirname(os.path.abspath(bp))
            gt_path = os.path.join(scene_dir, GT_JSON_NAME)
            scene_id = label
            raw_batch_id = pairs[i - 1][1] if pairs else ""
            p_csv = (
                manifest_ctx["preprocess_scenes_csv"] if manifest_ctx else ""
            )
            p_bid = manifest_ctx["preprocess_batch_id"] if manifest_ctx else ""
            p_bid_log = p_bid if p_bid else "SCENES2TEST"

            print(f"\n{'─' * 60}\n[{i}/{len(blends)}] {label}\n{'─' * 60}")
            if skip_if_gt and os.path.isfile(gt_path):
                print(f"[SKIP] {GT_JSON_NAME} already exists, skipping scene")
                skipped_count += 1
                opc_skip = _overlap_pair_count_from_info_dir(
                    scene_dir, semantics_enabled
                )
                if opc_skip is not None and opc_skip > 0:
                    overlap_scene_count += 1
                _maybe_append_overlap_log_entry(
                    overlap_log_entries,
                    scene_dir=scene_dir,
                    scene_id=scene_id,
                    preprocess_batch_id=p_bid_log,
                    opc=opc_skip,
                )
                if manifest_ctx:
                    _append_preprocess_scene_csv_row(
                        p_csv,
                        batch_scene_index=i,
                        preprocess_batch_id=p_bid,
                        scene_id=scene_id,
                        status="skipped",
                        overlap_count=int(opc_skip) if opc_skip is not None else 0,
                        raw_batch_id=raw_batch_id,
                    )
                continue
            cmd = [bpy_python, script, "--blend", bp] + extra
            if not skip_if_gt:
                cmd.append("--force")
            cmd.append("--no-log")
            try:
                r = subprocess.run(cmd, check=False)
                code = int(r.returncode or 0)
            except OSError as e:
                print(f"[ERROR] could not run subprocess: {e}")
                failures.append((label, bp, -1))
                opc_fail = _overlap_pair_count_from_info_dir(
                    scene_dir, semantics_enabled
                )
                if opc_fail is not None and opc_fail > 0:
                    overlap_scene_count += 1
                _maybe_append_overlap_log_entry(
                    overlap_log_entries,
                    scene_dir=scene_dir,
                    scene_id=scene_id,
                    preprocess_batch_id=p_bid_log,
                    opc=opc_fail,
                )
                if manifest_ctx:
                    _append_preprocess_scene_csv_row(
                        p_csv,
                        batch_scene_index=i,
                        preprocess_batch_id=p_bid,
                        scene_id=scene_id,
                        status="failed",
                        overlap_count=0,
                        raw_batch_id=raw_batch_id,
                    )
                continue

            if code != 0:
                failures.append((label, bp, code))
                print(f"[ERROR] scene preprocess failed exit={code}")
                opc_fail = _overlap_pair_count_from_info_dir(
                    scene_dir, semantics_enabled
                )
                if opc_fail is not None and opc_fail > 0:
                    overlap_scene_count += 1
                _maybe_append_overlap_log_entry(
                    overlap_log_entries,
                    scene_dir=scene_dir,
                    scene_id=scene_id,
                    preprocess_batch_id=p_bid_log,
                    opc=opc_fail,
                )
                if manifest_ctx:
                    _append_preprocess_scene_csv_row(
                        p_csv,
                        batch_scene_index=i,
                        preprocess_batch_id=p_bid,
                        scene_id=scene_id,
                        status="failed",
                        overlap_count=int(opc_fail) if opc_fail is not None else 0,
                        raw_batch_id=raw_batch_id,
                    )
            else:
                ok_count += 1
                opc = _overlap_pair_count_from_info_dir(scene_dir, semantics_enabled)
                oc = int(opc) if opc is not None else 0
                if opc is not None and opc > 0:
                    overlap_scene_count += 1
                    overlap_positive.append((label, opc))
                    print(f"[WARN] furniture overlap: {label} pair_count={opc}")
                _maybe_append_overlap_log_entry(
                    overlap_log_entries,
                    scene_dir=scene_dir,
                    scene_id=scene_id,
                    preprocess_batch_id=p_bid_log,
                    opc=opc,
                )
                if manifest_ctx:
                    _append_preprocess_scene_csv_row(
                        p_csv,
                        batch_scene_index=i,
                        preprocess_batch_id=p_bid,
                        scene_id=scene_id,
                        status="success",
                        overlap_count=oc,
                        raw_batch_id=raw_batch_id,
                    )

        loop_completed = True
    finally:
        if manifest_ctx:
            seeds = [_seed_from_scene_id(pairs[j][0]) for j in range(len(pairs))]
            avg_gt: Optional[float] = None
            avg_iv: Optional[float] = None
            if loop_completed and len(failures) == 0 and len(blends) > 0:
                avg_gt, avg_iv = _batch_avg_asset_metrics(blends)
            _finalize_preprocess_batch_jsonl(
                manifest_ctx["manifests_dir"],
                manifest_ctx["preprocess_batch_id"],
                num_processed=len(blends),
                end_seed_inclusive=max(seeds),
                num_scenes_with_overlap=overlap_scene_count,
                avg_gt_asset_count=avg_gt,
                avg_info_asset_count_in_view=avg_iv,
            )
            print(
                f"[manifest] updated preprocess_batches.jsonl: "
                f"{manifest_ctx['preprocess_batch_id']} "
                f"num_processed={len(blends)} num_scenes_with_overlap={overlap_scene_count} "
                f"end_seed_inclusive={max(seeds)} "
                f"avg_gt={avg_gt} avg_in_view={avg_iv}"
            )

        if overlap_log_entries:
            out_dir = (
                manifest_ctx["manifests_dir"]
                if manifest_ctx
                else (os.path.dirname(log_path) if log_path else root)
            )
            _write_overlap_scene_logs(
                entries=overlap_log_entries,
                out_dir=out_dir,
            )

        if manifest_ctx and overlap_log_entries:
            md = manifest_ctx["manifests_dir"]
            _append_furniture_overlap_rejects_jsonl(md, overlap_log_entries)
            try:
                _quarantine_overlap_scenes(
                    _processed_root(),
                    overlap_log_entries,
                    reject_subdir=PROCESSED_REJECT_SUBDIR,
                )
            except OSError as ex:
                print(f"[quarantine] ERROR: {ex}", file=sys.stderr)

    elapsed = time.perf_counter() - t0
    ran = len(blends) - skipped_count
    print(
        f"\n[DONE] Batch complete ({len(blends)} dirs, processed {ran}, skipped {skipped_count}, {elapsed:.1f}s)."
    )

    if log_path:
        failure_entries: List[Tuple[str, str]] = []
        for label, bp, code in failures:
            if code < 0:
                failure_entries.append((label, "could not start subprocess"))
            else:
                failure_entries.append((label, f"exit={code}  {bp}"))
        log_batch_id = (
            manifest_ctx["preprocess_batch_id"]
            if manifest_ctx
            else uuid.uuid4().hex[:16]
        )
        lines = _format_preprocess_report(
            batch_id=log_batch_id,
            finished_at_iso=datetime.now(timezone.utc).isoformat(),
            elapsed_sec=elapsed,
            scenes_total=len(blends),
            ok_count=ok_count,
            fail_count=len(failures),
            skipped_count=skipped_count,
            overlap_positive=overlap_positive,
            failure_entries=failure_entries,
        )
        _append_preprocess_log_block(log_path, lines)

    return 0 if not failures else 1
