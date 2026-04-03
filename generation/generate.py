#!/usr/bin/env python
"""
INFINIGEN 统一生成与渲染入口。

仅调用 ``generate_lib/core.py``（矩形单房间；地板 XY 以世界原点为中心，见 ``../docs/LIMITATION.md``）。
不规则房间管线已弃用，说明见 ``../docs/LIMITATION.md``。
**房型**：canonical 见 ``configs.constant.ROOM_TYPES``（snake_case）；对内映射到 Infinigen 串，共 **10** 种。

输出目录命名:
  <INFINIGEN>/SCENES_SKETCH/{canonical_room_type}_s{seed}/  默认（临时试跑、调参、改场景；非正式数据集）
  <INFINIGEN>/SCENES_RAW/…                      原始生成输出（未预处理），仅在使用 --scenes-raw 时
  <IDL3D>/SCENES_PROCESSED/                     预处理暂存（脚本自建目录，不由 generate.py 写入）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
--scenes-raw 推荐跑法（SSH 到学校服务器 / Cursor Remote-SSH）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
目标：VPN 断、关笔记本、关 Cursor 后，**服务器上**生成与 manifest 仍继续；数据写在服务器可访问的
持久目录（如 <INFINIGEN>/SCENES_RAW，勿依赖仅 VPN 挂载才可见的路径）。

1. 打开**已连上远程的终端**（本机 ssh 登录后，或 Cursor 集成终端在 Remote-SSH 下均可；无需再敲 ssh）。
2. **先**创建 tmux 会话（进程挂在 tmux 里，断 SSH 不随客户端一起死）::

     tmux new -s infinigen # 创建一个名为 infinigen 的 tmux 会话
     tmux attach -t infinigen # 附加到名为 infinigen 的 tmux 会话


3. **在 tmux 窗口内**激活 Conda 与工作目录（新开 tmux 常需在此再 activate 一次）::

     source /scratch2/f0080vw/miniconda/etc/profile.d/conda.sh
     conda activate infinigen
     cd <REPO>/generation

4. 启动 RAW 批量（示例）::

     python generate.py --room-type homestudio --num-scenes 20 --scenes-raw
 
5. 与终端「脱钩」但**不停止任务**：按 Ctrl+b，松开后按 d（detach）。之后可安全关 Cursor / 断 VPN。
6. 再次登录后恢复会话::

     tmux attach -t infinigen

   查看有哪些会话：``tmux ls``。

**切勿**在未进入 tmux 时于远程终端里直接长跑 ``python generate.py ... --scenes-raw`` 后关 Cursor，
否则 SSH 会话结束可能导致进程被挂断。

本脚本在 --scenes-raw 下会：先写 ``SCENES_RAW/manifests/generation_batches.jsonl`` 一条批次（进度字段初为 null、
``status`` 默认为 ``pending_preprocess``）、每成一景更新该条并追加 ``<room_type>_scenes.csv``；中断时进度已落盘，可再 attach 继续。
``generation_batches.jsonl`` 的 ``status`` 还可手改为 ``archived`` / ``deleted``（见 manifests/README.txt）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
常用参数速查
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  单场景输出（由 ``generate_lib.core`` 收尾）：``scene.blend``、``color.png``、``instance_segmentation.png``（uint16 灰度）、``metadata.json``（含相机与 object_index 映射）。

  脚本顶部 FIXED_WALL_THICKNESS_M       默认 0.25（米），固定墙厚/地板挤出；设为 None 恢复随机
  --room-type <type>                  单种房型（默认 bedroom）
  --room-types <t1> <t2> ...          多种房型，每种生成 --num-scenes 个
  --batch "t1:n1,t2:n2,..."           批量：每种指定不同数量（与 --room-types 二选一）
  --num-scenes <N>                    每种房型生成多少个（默认 1）
  --room-size {small,medium,large,xlarge,auto}  仅非住宅类；默认 auto（working_space 四类每场景三档各 1/3）
  --room-size-mix "tier:n,..."         仅非住宅类且单 --room-type 时可用；与 --batch / --room-types 不兼容
  --render-samples / --sample <N>     渲染采样数（默认 128，如 --sample 256）
  --render <W>                        输出宽度：1280→1280×720（默认），640→640×360（可与 --sample 同时用）
  --denoise / --no-denoise            默认开启
  --light-multiplier <F>              默认 5.0
  --gpu <id>                          默认 get_render_gpu()（当前 2）
  --task <...>                        默认 coarse render
  --start-seed <int>                  默认随机；批量时同一房型内依次 +1，不同房型独立随机
  --scenes-raw                        场景写入 <INFINIGEN>/SCENES_RAW，并写 manifests（见下）
  --overrides <gin overrides...>      额外 gin 覆盖

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
示例
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
cd /scratch2/f0080vw/IDL3D/generation
conda activate infinigen

# 住宅类全部生成
python generate.py --room-types bedroom homestudio livingroom kitchen bathroom diningroom --num-scenes 4

# 办公/教学类全部生成
python generate.py --room-types office classroom meetingroom library --num-scenes 3

# 单个房型生成 改分辨率 改采样数
python generate.py --room-type bathroom --num-scenes 5 --render 640 --sample 256
python generate.py --room-type homestudio --num-scenes 3 --start-seed 42


# 非住宅类：可指定 --room-size；单房型、多场景若要按档混合，仍可用 --room-size-mix（仅非住宅，如 library）
python generate.py --room-type office --num-scenes 4 --room-size large
python generate.py --room-type classroom --num-scenes 4 --room-size medium
python generate.py --room-type library --num-scenes 4 --room-size large
python generate.py --room-types office library classroom --num-scenes 4
python generate.py --room-type meetingroom --num-scenes 5 --render 640

# 原始场景写入 SCENES_RAW（自动生成 manifests/generation_batches.jsonl 与各房型 _scenes.csv）
python generate.py --room-type bedroom --num-scenes 20 --scenes-raw


"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import secrets
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from configs.constant import (
    LIVING_SPACE_ROOM_TYPES,
    ROOM_TYPES,
    WORKING_SPACE_ROOM_TYPES,
)
from configs.infinigen_paths import (
    INFINIGEN_IDL3D_ROOT,
    SCENES_RAW_ROOT,
    SCENES_SKETCH_ROOT,
)

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATE_LIB_DIR = SCRIPT_DIR / "generate_lib"

# SCENES_RAW/manifests：生成批次登记（与 preprocess 的 preprocess_batches.jsonl 区分）
GENERATION_BATCHES_JSONL = "generation_batches.jsonl"
_LEGACY_RAW_BATCHES_JSONL = "batches.jsonl"

# generation_batches.jsonl 每行批次可选 status（英文，人工或流程更新）：
#   pending_preprocess — 生成已完成（或进行中），尚未视为预处理/归档闭环
#   archived — 已纳入正式管线（如已预处理并发布/备份）
#   deleted — 整批废弃，不参与后续预处理（磁盘与 CSV 宜同步清理）
GENERATION_BATCH_STATUS_PENDING_PREPROCESS = "pending_preprocess"
GENERATION_BATCH_STATUS_ARCHIVED = "archived"
GENERATION_BATCH_STATUS_DELETED = "deleted"
GENERATION_BATCH_STATUSES = frozenset(
    {
        GENERATION_BATCH_STATUS_PENDING_PREPROCESS,
        GENERATION_BATCH_STATUS_ARCHIVED,
        GENERATION_BATCH_STATUS_DELETED,
    }
)


def _generation_batches_jsonl_path(manifests_dir: Path) -> Path:
    """RAW 侧生成批次清单路径；若仅存旧名 ``batches.jsonl`` 则一次性重命名为 ``generation_batches.jsonl``。"""
    new_p = manifests_dir / GENERATION_BATCHES_JSONL
    old_p = manifests_dir / _LEGACY_RAW_BATCHES_JSONL
    if new_p.is_file():
        return new_p
    if old_p.is_file():
        old_p.rename(new_p)
    return new_p

# 固定墙厚（米），通过 gin 覆盖 ``RoomConstants.global_params.wall_thickness``。
# Infinigen 默认在 0.2–0.3 m 间随机。地板在网格里由 ``BlueprintSolidifier.make_room`` 第二次
# SOLIDIFY 挤出，厚度为 **wall_thickness/2**（与墙厚绑定，非独立参数）。
# 例如 0.25 m 墙厚 → 地板挤出约 **0.125 m**；室内可走面高度仍由完整房间网格决定。
# 设为 None 则保持 Infinigen 默认随机墙厚。
FIXED_WALL_THICKNESS_M: float | None = 0.25


def _gin_wall_thickness_override_args() -> list[str]:
    if FIXED_WALL_THICKNESS_M is None:
        return []
    return [f"RoomConstants.global_params.wall_thickness={float(FIXED_WALL_THICKNESS_M)}"]


# 与 generate_lib.core 一致：非住宅类可选档位；住宅类由 core 内部随机/固定（不在此选档）
# auto 仅 generate.py 使用：working_space 四类不向 core 传 --room-size，由其每场景三档各 1/3 随机
ROOM_SIZE_PRESETS = ("small", "medium", "large", "xlarge", "auto")


def get_render_gpu() -> str:
    """选择利用率最低的 GPU。

    优先用 nvidia-smi 查询每块 GPU 的当前显存使用量，选择最空闲的那块。
    若 nvidia-smi 不可用（非 NVIDIA 环境），回退到默认 GPU 2。
    """
    import shutil
    import subprocess

    if not shutil.which("nvidia-smi"):
        return "2"
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        best_idx, best_used = "2", float("inf")
        for line in out.strip().splitlines():
            idx, used = line.split(",")
            used_mb = float(used.strip())
            if used_mb < best_used:
                best_used = used_mb
                best_idx = idx.strip()
        print(f"[GPU] 自动选择 GPU {best_idx}（显存占用 {best_used:.0f} MiB 最低）")
        return best_idx
    except Exception:
        return "2"


def _try_infinigen_git_sha() -> str | None:
    """``infinigen-idl3d`` 仓库 HEAD，不可用时返回 None。"""
    sub = INFINIGEN_IDL3D_ROOT
    if not (sub / ".git").exists():
        return None
    try:
        return subprocess.check_output(
            ["git", "-C", str(sub), "rev-parse", "HEAD"],
            text=True,
            timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _next_room_batch_id(manifests_dir: Path, room_type: str) -> str:
    """下一批次 ID：{room_type}_batch_{NNN}（如 bedroom_batch_003），按同前缀已存在编号 +1。"""
    prefix = f"{room_type}_batch_"
    max_n = 0
    p = _generation_batches_jsonl_path(manifests_dir)
    if not p.is_file():
        return f"{prefix}001"
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = rec.get("batch_id", "")
            if not isinstance(bid, str) or not bid.startswith(prefix):
                continue
            suffix = bid[len(prefix) :]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:03d}"


def _manifests_dir() -> Path:
    d = SCENES_RAW_ROOT / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _room_type_scenes_csv_path(manifests_dir: Path, room_type: str) -> Path:
    """每房型一个场景清单：{room_type}_scenes.csv（如 bedroom_scenes.csv）。"""
    return manifests_dir / f"{room_type}_scenes.csv"


def _append_csv_rows_for_batch(
    manifests_dir: Path,
    room_type: str,
    batch_id: str,
    scene_ids: list[str],
) -> Path:
    """向 {room_type}_scenes.csv 追加多行；列顺序 batch_scene_index, batch_id, scene_id。"""
    csv_path = _room_type_scenes_csv_path(manifests_dir, room_type)
    csv_header = ["batch_scene_index", "batch_id", "scene_id"]
    write_header = not csv_path.is_file() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(csv_header)
        for i, sid in enumerate(scene_ids):
            w.writerow([i + 1, batch_id, sid])
    return csv_path


def _append_jsonl_line(manifests_dir: Path, record: dict) -> None:
    batches_path = _generation_batches_jsonl_path(manifests_dir)
    with batches_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_scenes_raw_batch_pending_line(
    *,
    batch_id: str,
    room_type: str,
    num_requested: int,
    batch_start_seed: int,
    argv_for_record: list[str],
) -> None:
    """--scenes-raw 开始时追加一条批次记录；num_generated / end_seed_inclusive 均为 null，随后每成一景重写。"""
    manifests_dir = _manifests_dir()
    sha = _try_infinigen_git_sha()
    record: dict = {
        "batch_id": batch_id,
        "room_type": room_type,
        "num_requested": num_requested,
        "num_generated": None,
        "start_seed": batch_start_seed,
        "end_seed_inclusive": None,
        "scenes_root": "SCENES_RAW",
        "generate_argv": argv_for_record,
        "status": GENERATION_BATCH_STATUS_PENDING_PREPROCESS,
    }
    if sha:
        record["infinigen_submodule_git_sha"] = sha
    _append_jsonl_line(manifests_dir, record)
    print(
        f"[generate.py] RAW manifest：已登记批次行 {batch_id}（num_generated/end_seed_inclusive=null，"
        f"每成一景更新一次）"
    )


def _append_scenes_raw_csv_row_only(
    *,
    batch_id: str,
    room_type: str,
    scene_id: str,
    scene_index: int,
    num_requested: int,
) -> None:
    """每成功一个场景向 {room_type}_scenes.csv 追加一行。"""
    manifests_dir = _manifests_dir()
    csv_path = _room_type_scenes_csv_path(manifests_dir, room_type)
    csv_header = ["batch_scene_index", "batch_id", "scene_id"]
    write_header = not csv_path.is_file() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(csv_header)
        w.writerow([scene_index + 1, batch_id, scene_id])
    print(
        f"[generate.py] RAW manifest：{csv_path.name} {scene_index + 1}/{num_requested} → {scene_id}"
    )


def _atomic_write_generation_batches_jsonl(manifests_dir: Path, lines: list[str]) -> None:
    """整文件重写 generation_batches.jsonl；先写 .tmp 再 replace，降低写半截风险。"""
    p = _generation_batches_jsonl_path(manifests_dir)
    text = "\n".join(lines) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _update_scenes_raw_batch_progress(
    manifests_dir: Path,
    batch_id: str,
    num_generated: int,
    end_seed_inclusive: int,
    num_requested: int,
) -> None:
    """重写该 batch_id 所在行：更新 num_generated 与 end_seed_inclusive（当前已完成的最后一个 seed）。"""
    p = _generation_batches_jsonl_path(manifests_dir)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    raw_lines = p.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    found = False
    for line in raw_lines:
        if not line.strip():
            out_lines.append(line)
            continue
        rec = json.loads(line)
        if rec.get("batch_id") == batch_id:
            rec["num_generated"] = num_generated
            rec["end_seed_inclusive"] = end_seed_inclusive
            if "status" not in rec:
                rec["status"] = GENERATION_BATCH_STATUS_PENDING_PREPROCESS
            out_lines.append(json.dumps(rec, ensure_ascii=False))
            found = True
        else:
            out_lines.append(line)
    if not found:
        raise RuntimeError(
            f"{GENERATION_BATCHES_JSONL} 中未找到 batch_id={batch_id!r} 的批次行"
        )
    _atomic_write_generation_batches_jsonl(manifests_dir, out_lines)
    print(
        f"[generate.py] RAW manifest：{batch_id} 进度 {num_generated}/{num_requested}，"
        f"end_seed_inclusive={end_seed_inclusive}"
    )


def _build_argv_box(args: argparse.Namespace) -> list[str]:
    """根据统一参数构建 generate_lib/core.py 的 argv（不含脚本路径）。"""
    argv = [
        "--room-type",
        args.room_type,
        "--num-scenes",
        str(args.num_scenes),
        "--render-samples",
        str(args.render_samples),
        "--render-width",
        str(getattr(args, "render_width", 1280)),
        "--light-multiplier",
        str(args.light_multiplier),
        "--gpu",
        args.gpu or get_render_gpu(),
    ]
    if args.no_denoise:
        argv.append("--no-denoise")
    if args.room_type in LIVING_SPACE_ROOM_TYPES:
        pass
    elif args.room_size_mix:
        argv.extend(["--room-size-mix", args.room_size_mix])
    elif args.room_size == "auto":
        if args.room_type not in WORKING_SPACE_ROOM_TYPES:
            argv.extend(["--room-size", "medium"])
    else:
        argv.extend(["--room-size", args.room_size])
    if args.start_seed is not None:
        argv.extend(["--start-seed", str(args.start_seed)])
    if args.task != ["coarse", "render"]:
        argv.extend(["--task", *args.task])
    merged_ov = _gin_wall_thickness_override_args() + list(args.overrides or [])
    if merged_ov:
        argv.extend(["--overrides", *merged_ov])
    if getattr(args, "scenes_root_for_box", None) is not None:
        argv.extend(["--scenes-root", str(args.scenes_root_for_box)])
    if args.output_folder is not None:
        argv.extend(["--output_folder", str(args.output_folder)])
    if args.input_folder is not None:
        argv.extend(["--input_folder", str(args.input_folder)])
    if args.task_uniqname is not None:
        argv.extend(["--task_uniqname", args.task_uniqname])
    if args.debug is not None:
        argv.extend(["--debug", *args.debug])
    return argv


def _parse_batch(batch_str: str) -> list[tuple[str, int]]:
    """解析 --batch 字符串，返回 [(room_type, count), ...] 列表（canonical）。"""
    result = []
    for item in batch_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise argparse.ArgumentTypeError(
                f"--batch 格式错误 '{item}'，应为 'room_type:count'，例如 'livingroom:3'"
            )
        rtype, _, count_str = item.partition(":")
        rtype = rtype.strip()
        if rtype not in ROOM_TYPES:
            raise argparse.ArgumentTypeError(
                f"未知房型 '{rtype}'，可选: {', '.join(ROOM_TYPES)}"
            )
        try:
            count = int(count_str.strip())
            assert count > 0
        except (ValueError, AssertionError):
            raise argparse.ArgumentTypeError(
                f"数量必须是正整数，得到 '{count_str}'"
            )
        result.append((rtype, count))
    return result


def _run_one_room_type(
    args: argparse.Namespace,
    room_type: str,
    num_scenes: int,
    *,
    raw_manifest: bool,
) -> None:
    """针对单个房型运行生成，num_scenes 个场景。

    raw_manifest 为 True（--scenes-raw）时：若未指定 --start-seed，
    则在本函数内生成与 generate_lib.core 相同分布的随机起始 seed 并传入，以便写 manifest。

    --scenes-raw 下：启动时追加一条 generation_batches.jsonl（num_generated/end_seed_inclusive 均为 null），
    每成功一个场景追加 CSV 并重写该批次行，使 num_generated 与 end_seed_inclusive 与进度一致；
    整批结束时最后一次更新即末场景。中断则保留当时进度（num_generated < num_requested）。
    """
    run_args = copy.copy(args)
    run_args.room_type = room_type
    run_args.num_scenes = num_scenes
    if raw_manifest:
        if args.start_seed is None:
            run_args.start_seed = secrets.randbelow(2**31 - 1)
    else:
        if args.start_seed is None:
            run_args.start_seed = None

    script = GENERATE_LIB_DIR / "core.py"

    if raw_manifest:
        assert run_args.start_seed is not None
        batch_start = int(run_args.start_seed)
        folder = room_type
        argv_for_record = sys.argv.copy()
        manifests_dir = _manifests_dir()
        batch_id = _next_room_batch_id(manifests_dir, room_type)

        _append_scenes_raw_batch_pending_line(
            batch_id=batch_id,
            room_type=room_type,
            num_requested=num_scenes,
            batch_start_seed=batch_start,
            argv_for_record=argv_for_record,
        )

        try:
            for i in range(num_scenes):
                one = copy.copy(run_args)
                one.num_scenes = 1
                one.start_seed = batch_start + i
                argv = _build_argv_box(one)
                cmd = [sys.executable, str(script)] + argv
                print(f"\n{'=' * 60}")
                print(
                    f"[generate.py] room-type={room_type}  场景 {i + 1}/{num_scenes}  "
                    f"(seed={batch_start + i})"
                )
                print(f"{'=' * 60}")
                subprocess.run(cmd, check=True)

                sid = f"{folder}_s{batch_start + i}"
                _append_scenes_raw_csv_row_only(
                    batch_id=batch_id,
                    room_type=room_type,
                    scene_id=sid,
                    scene_index=i,
                    num_requested=num_scenes,
                )
                _update_scenes_raw_batch_progress(
                    manifests_dir,
                    batch_id,
                    num_generated=i + 1,
                    end_seed_inclusive=batch_start + i,
                    num_requested=num_scenes,
                )
        except KeyboardInterrupt:
            print(
                "\n[generate.py] 已中断：generation_batches.jsonl 与 CSV 已反映截至上一场景的进度；"
                "若 num_generated < num_requested 可补跑剩余场景，或按 manifests/README.txt 手工编辑批次记录。\n",
                file=sys.stderr,
            )
            raise

        return

    argv = _build_argv_box(run_args)
    cmd = [sys.executable, str(script)] + argv
    print(f"\n{'=' * 60}")
    print(f"[generate.py] room-type={room_type}  num={num_scenes}")
    print(f"{'=' * 60}")
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="INFINIGEN 统一入口：调用 generate_lib.core 生成矩形单房间场景。",
    )

    # ── 房型参数（三选一）────────────────────────────────────────────
    type_group = parser.add_mutually_exclusive_group()
    type_group.add_argument(
        "--room-type",
        type=str,
        default=None,
        choices=list(ROOM_TYPES),
        help="单种房型（canonical，默认 bedroom）",
    )
    type_group.add_argument(
        "--room-types",
        nargs="+",
        metavar="ROOM_TYPE",
        choices=list(ROOM_TYPES),
        default=None,
        help="多种房型，每种生成 --num-scenes 个",
    )
    type_group.add_argument(
        "--batch",
        type=str,
        default=None,
        metavar="'t1:n1,t2:n2,...'",
        help="批量：如 'livingroom:3,kitchen:2,diningroom:4'",
    )
    # ────────────────────────────────────────────────────────────────

    parser.add_argument(
        "--num-scenes",
        type=int,
        default=1,
        help="每种房型生成场景数（默认 1；--batch 模式下由 batch 字符串各自指定）",
    )
    parser.add_argument(
        "--room-size",
        type=str,
        default="auto",
        choices=list(ROOM_SIZE_PRESETS),
        help=(
            "房间尺寸档位（仅非住宅类；住宅类不传）。默认 **auto**：working_space 四类不向 core 传档，"
            "由其在**每个场景** small/medium/large 间各 1/3 随机（整批非同一档）。"
            "显式指定 small/medium/large/xlarge 时该房型下所有场景固定为该档。"
        ),
    )
    parser.add_argument(
        "--room-size-mix",
        type=str,
        default=None,
        help="混合档位仅非住宅类单房型有效，如 small:2,medium:3",
    )
    parser.add_argument(
        "--render-samples",
        "--sample",
        type=int,
        default=128,
        metavar="N",
        dest="render_samples",
        help="渲染采样数（默认 128；可写 --sample 256）",
    )
    parser.add_argument(
        "--render",
        type=int,
        choices=(1280, 640),
        default=1280,
        metavar="W",
        dest="render_width",
        help="输出图像宽度：1280→1280×720（默认），640→640×360",
    )
    parser.add_argument(
        "--denoise", action="store_true", default=True, help="启用去噪（默认开启）"
    )
    parser.add_argument(
        "--no-denoise", action="store_true", default=False, help="关闭去噪"
    )
    parser.add_argument(
        "--light-multiplier",
        type=float,
        default=5.0,
        help="灯光功率倍率（默认 5.0）",
    )
    parser.add_argument(
        "--gpu",
        type=str,
        default=None,
        help=f"指定 GPU 编号（如 --gpu 2）。不指定时默认 {get_render_gpu()}",
    )
    parser.add_argument(
        "--task",
        nargs="+",
        default=["coarse", "render"],
        choices=[
            "coarse",
            "populate",
            "fine_terrain",
            "ground_truth",
            "render",
            "mesh_save",
            "export",
        ],
        help="任务阶段（默认 coarse+render）",
    )
    parser.add_argument(
        "--start-seed",
        type=int,
        default=None,
        help="起始 seed（批量多房型时各房型独立随机，除非指定此参数）",
    )
    parser.add_argument(
        "--scenes-raw",
        action="store_true",
        help=(
            f"批量场景写入 {SCENES_RAW_ROOT}：{GENERATION_BATCHES_JSONL} 先追加一条批次"
            f"（num_generated/end_seed=null），每成一景更新该行并追加 CSV；"
            f"中断时 num_generated < num_requested（可补跑或按 manifests/README.txt 手工维护）。"
            f"不加此开关则默认 {SCENES_SKETCH_ROOT}"
        ),
    )
    parser.add_argument("--overrides", nargs="+", default=[], help="额外 gin 覆盖参数")
    parser.add_argument("--output_folder", type=Path, default=None, help="输出目录")
    parser.add_argument(
        "--input_folder",
        type=Path,
        default=None,
        help="输入目录（仅 render 单独跑时用）",
    )
    parser.add_argument("--task_uniqname", type=str, default=None)
    parser.add_argument("--debug", type=str, nargs="*", default=None, help="调试模式")
    args = parser.parse_args()

    if args.no_denoise:
        args.denoise = False
    if args.gpu is None:
        args.gpu = get_render_gpu()

    scenes_root_for_box: Path | None = None
    if args.scenes_raw:
        scenes_root_for_box = SCENES_RAW_ROOT.resolve()
    args.scenes_root_for_box = scenes_root_for_box
    if scenes_root_for_box is not None:
        print(f"[generate.py] 场景父目录: {scenes_root_for_box}")

    res_label = "640×360" if args.render_width == 640 else "1280×720"
    print(f"[generate.py] 渲染输出分辨率: {res_label}")

    # ── 构建 (room_type, count) 任务列表 ──────────────────────────
    if args.batch is not None:
        try:
            task_list = _parse_batch(args.batch)
        except argparse.ArgumentTypeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        if args.room_size_mix:
            print("error: --room-size-mix 与 --batch 不兼容。", file=sys.stderr)
            sys.exit(1)
    elif args.room_types is not None:
        task_list = [(rt, args.num_scenes) for rt in args.room_types]
        if args.room_size_mix:
            print(
                "error: --room-size-mix 与 --room-types 不兼容（请用 --room-type 单一房型）。",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        room_type = args.room_type or "bedroom"
        args.room_type = room_type
        task_list = [(room_type, args.num_scenes)]

    total = sum(n for _, n in task_list)
    print(f"[generate.py] 共 {len(task_list)} 种房型，合计 {total} 个场景：")
    for rt, n in task_list:
        print(f"  {rt}: {n} 个")

    raw_manifest = bool(args.scenes_raw)

    for room_type, count in task_list:
        _run_one_room_type(args, room_type, count, raw_manifest=raw_manifest)

    print(f"\n[generate.py] 全部完成（{total} 个场景）。")


if __name__ == "__main__":
    main()
