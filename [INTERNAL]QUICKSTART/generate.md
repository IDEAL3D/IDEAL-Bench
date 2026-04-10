# 场景生成（`generation/generate.py`）

批量生成矩形单房间：入口脚本调用 `generation/generate_lib/core.py`，房型为 `configs/constant.py` 中的 canonical 列表。约束与限制见仓库根 `docs/LIMITATION.md`。

## 远程长跑（SSH / tmux）

长时间生成建议使用 **tmux**（或 `screen`），避免关闭笔记本、断 VPN 或结束 SSH 会话时进程被挂断。

**约定**：下文 **prefix** 默认为 **Ctrl+b**（先按下再松开），随后单独按第二个键——**不要**在按第二个键时仍夹着 **Ctrl**（否则易误按成 **Ctrl+c**，中断前台 Python，出现 `KeyboardInterrupt`）。在 **Mac** 上尤其注意：**prefix 松开后再按字母**，例如新建窗口是 **prefix 然后 `c`**，不是 **Ctrl+c**。

### 单会话（一个任务）

1. SSH 登录到**运行生成的机器**，打开终端（或 IDE Remote-SSH 集成终端）。
2. 创建并进入会话：`tmux new -s infinigen`（再次进入：`tmux attach -t infinigen`）。


3. 在会话内加载 **conda** 并进入 `generation`（新 tmux 窗若提示 `Run 'conda init' before 'conda activate'`，先 `source` 再 `activate`）：
   ```bash
   # 仓库根目录为 IDEAL/（将下面第一行换成你本机的 IDEAL 路径）
   cd /path/to/IDEAL
   source /path/to/miniconda/etc/profile.d/conda.sh
   conda activate infinigen
   cd generation
   ```
   亦可本账号执行一次 `conda init bash` 后重开 shell，便于在各 tmux 窗内直接使用 `conda activate`。



4. 启动任务，例如（**GPU** 见下文「GPU 与 CUDA」）：
   ```bash
   python generate.py --room-type homestudio --num-scenes 20 --gpu auto
   ```
   若集群上需固定某张卡，可改为 `--gpu 2` 或事先 `export IDEAL_GPU=2`。
5. 与终端脱钩：**prefix** 后按 **d**（detach）；任务仍在服务器运行。
6. 恢复：`tmux attach -t infinigen`；列出会话：`tmux ls`。

### 多 GPU 并行（推荐：多个独立会话）

两路生成各用一张卡时，**不必**共用一个会话名；用 **两个终端** 各起一个 tmux **更好区分**，也避免误键打断另一路：

```text
# 终端 A
tmux new -s gpu2
source /path/to/miniconda/etc/profile.d/conda.sh && conda activate infinigen
cd /path/to/IDEAL/generation
python generate.py ... --gpu 2 --start-seed <a>

# 终端 B
tmux new -s gpu3
source /path/to/miniconda/etc/profile.d/conda.sh && conda activate infinigen
cd /path/to/IDEAL/generation
python generate.py ... --gpu 3 --start-seed <b>
```

恢复：`tmux attach -t gpu2` / `tmux attach -t gpu3`。并行时请使用 **不同 `--start-seed`**（或不同 `--scenes-root`），避免写入同一 `*_s{seed}/` 目录。

若坚持 **单会话多窗口**：**prefix** 后按 **`c`** 新建窗口，**prefix** 后按 **`0`–`9`** 或 **`n`** / **`p`** 切换。注意：两个客户端同时 `attach` **同一个**会话时界面会联动，不适合当作两块独立监视屏。

### 其它（可选）

- **触控板滚动出现 `^[[B` 等**：在 `~/.tmux.conf` 中加 `set -g mouse on` 并 `tmux source-file ~/.tmux.conf`，由 tmux 处理滚轮。
- **结束会话**（会终止其中所有进程）：`tmux kill-session -t <name>`。

原始输出建议落在本机或 NFS **持久路径**下的 `IDEAL/data/scenes_raw`（见 `configs/data_paths.py`），勿依赖仅 VPN 挂载才可见的目录。

## `data/scenes_raw` 与 manifests

`generate.py` **始终**将场景写入仓库内 **`data/scenes_raw`**（`SCENES_RAW_ROOT`），并在 `data/scenes_raw/manifests/` 维护：

- `generation_batches.jsonl`：每批一条；字段含 `num_generated`、`end_seed_inclusive` 等，开始可为 null，随进度更新。
- 各房型 `*_scenes.csv`：场景清单。

中断后进度已落盘，可按 `manifests/` 下说明续跑或手工编辑；`generation_batches.jsonl` 中行内 `status` 可改为 `archived` / `deleted` 等（见同目录 `README.txt`）。

## `generate.py` 常用参数（速查）

| 参数 | 说明 |
|------|------|
| `--room-type` / `--room-types` | 单种或多种房型；省略则默认 `bedroom` |
| `--num-scenes` | 每种房型场景数 |
| `--room-size` | 仅非住宅：`small` / `medium` / `large` / `xlarge` / `auto`（默认 `auto` 时 working_space 每场景三档各 1/3） |
| `--sample` | Cycles 采样数，默认 128 |
| `--render` | 宽度 1280 或 640 |
| `--light-multiplier` | 灯光倍率，默认 5.0 |
| `--gpu` | `CUDA_VISIBLE_DEVICES`：单卡或多卡（如 `2` 或 `0,1`）；`auto` 用 `nvidia-smi` 选最闲卡；省略时依次看 `IDEAL_GPU`、已有环境变量 `CUDA_VISIBLE_DEVICES`，再否则等同 `auto`（无 GPU 查询能力时回退 `0`） |
| `--start-seed` | 起始 seed；多房型时可控制复现 |
| `--overrides` | 传给 Infinigen 的 gin 覆盖（高级） |

入口固定 **coarse + render** 且 **去噪开启**。单阶段、自定义 `--task` 或 I/O 路径请直接使用 `generate_lib/core.py`。

## `core.py`（后端）补充说明

- **输出目录**：`generate.py` 与直接调用 `core.py` 时默认父目录均为 `data/scenes_raw`（`generate.py` 通过 `--scenes-root` 传入 `core`）；`core.py` 可用 `--scenes-root`、`--output_folder`（单场景）等覆盖。
- **住宅类 6 种**：不在 CLI 固定档位；厨房/卫浴/餐厅等为脚本内固定档，卧室/客厅等为 seed 驱动的 small/medium 等（见源码 `resolve_living_space_tier`）。
- **working_space 四类**：`--room-size` 未指定时为每场景 small/medium/large 各 1/3。
- **灯光 `--light-multiplier`**：粗略对应「功率倍率」；常用约 3–10，默认 5.0。也可用 `--overrides` 改曝光等（如 `configure_render_cycles.exposure`）。

脚本内 **FIXED_WALL_THICKNESS_M**（`generate.py` 与 `core.py` 顶部常量）默认固定墙厚/地板挤出；设为 `None` 则沿用 Infinigen 随机墙厚。

## GPU 与 CUDA（`generate.py` / `core.py` / `preprocess.py`）

- **统一逻辑**在 `configs/gpu_select.py` 的 `resolve_cuda_visible_devices`：`generate.py`、`generate_lib/core.py`、`preprocess_lib/cli.py`（预处理入口）共用同一套优先级（`--gpu` → `IDEAL_GPU` → 已存在的 `CUDA_VISIBLE_DEVICES` → `auto` / 回退 `0`）。
- **预处理**：`python preprocess.py ...` 支持 **`--gpu`**，会在进程开始时设置 `CUDA_VISIBLE_DEVICES`，batch 子进程继承同一选择，避免每场景重新挑卡。
- **仅打印/导出环境变量**（不跑生成）：仓库根执行  
  `python '[INTERNAL]tools/gpu_selecting.py'`  
  或 `eval $(python '[INTERNAL]tools/gpu_selecting.py' --export)`。

对外发布若希望**完全交给用户环境**、不在代码里改 `CUDA_VISIBLE_DEVICES`，解耦成本较低：逻辑集中在 **`configs/gpu_select.py`** 与上述三处调用；上线前可改为「不调用 `resolve_*`、不注入 `CUDA_VISIBLE_DEVICES`」，仅保留用户 shell / 调度系统里已设置的变量（并同步删掉或绕开 `generate_lib/core.py` 里 `_run` 对 `env["CUDA_VISIBLE_DEVICES"]` 的赋值）。**`[INTERNAL]tools/gpu_selecting.py`** 可作为可选工具保留或删除。

## 与预处理的关系

预处理入口为 `generation/preprocess.py`，工作目录为 `SCENES_PROCESSED` 等；不由 `generate.py` 直接写入。预处理与生成共用上一节的 GPU 解析规则（`--gpu` / `IDEAL_GPU` / `auto` 等）。
