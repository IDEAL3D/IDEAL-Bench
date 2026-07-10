# IDEAL-Bench: Indoor Dataset for Evaluating Analysis by 3D Layout Reasoning



> **IDEAL-Bench** is an evaluation suite that requires VLMs to predict structured 3D layouts on photorealistic indoor scenes across 10 room types, scored along five numerical dimensions (scene validity, physical plausibility, geometric accuracy, object recognition, and grid layout) and a perceptual render-and-compare protocol. 
>
> Built on **IDEAL-Scenes** — 1,000 procedurally generated, re-renderable Blender scenes across 10 indoor room types, with programmatically extracted ground-truth layouts.

---

## Environment setup

```bash
# 1. Python (3.11.x per infinigen-ideal/pyproject.toml)
conda create -n ideal python=3.11 -y
conda activate ideal
git submodule update --init --recursive infinigen-ideal
pip install -r requirements.txt   # installs bpy==4.2.x; use same env for scripts that import bpy

# 2. Optional: system Blender app (e.g. snap on Linux) for manual .blend work — not a substitute for pip bpy
# snap install blender --classic
# https://www.blender.org/download/
```

### API keys

Credentials are not shipped with the repo; add them locally. For `inference/infer.py`, use `api/secrets/api_keys.csv` by default or pass `--config` with another CSV; each row has columns `model`, `provider`, `api_key`, `base_url`. For `benchmarks/evaluation/run_judge_vlm_claude.py`, use `ANTHROPIC_API_KEY`, `--api-key`, `--api-key-file`, or `IDEAL_ANTHROPIC_API_KEY_FILE`. Other scripts under `inference/*_api.py` typically use `--api-key` or provider env vars (`OPENAI_API_KEY`, `GEMINI_API_KEY`, etc.).

---

## Dataset (IDEAL-Scenes)

Download IDEAL-Scenes (1,000 scenes, ~56 GB) from Hugging Face (default repo in the script: `cyn-xyz/ideal_scenes_preprocessed`, on disk under `data/scenes/`):

```bash
# repo root; needs: pip install huggingface_hub (and huggingface-cli login if the snapshot is gated)
python scripts/download_scenes_hf.py
# optional: --repo-id … --destination path/to/scenes --revision …
```

**Layout:** `data/scenes/<room_type>/<scene_id>/` (while generating from scratch, the same leaf layout also appears under `data/scenes_raw/` until you publish or copy elsewhere).

**Typical files per scene**

- `scene.blend` — Blender source (may be absent in preprocessed HF drops)
- `GT.json` — layout ground truth
- `objects_in_view.json` — in-view object subset
- `metadata.json` — camera intrinsics / extrinsics
- `color.png` — reference RGB
- `instance_segmentation.png` — uint16 instance map

**Regenerate locally** (writes to `data/scenes_raw/`; both filters default to that root):

```bash
# Synthesize + render + export GT sidecars
python generation/generate.py --room-type bedroom --num-scenes 100
# Stage 1 — in-view chain + overlap; failures → invalid/
python generation/filter_scenes.py
# Stage 2 — visibility / aspect / truncation → objects_in_view.json
python generation/filter_inview.py
```

---

## Predictions (inference)

**On-disk layout** (used by `benchmarks/evaluation` and `benchmarks/reconstruction`):

```
result/inference/inference_result/<room_type>/<scene_id>/<model>.json
```

**Run inference yourself** — `inference/infer.py` writes the layout above (system prompt: `inference/prompts/system_prompt_room_centered.md`). Other drivers under `inference/` should match this tree when batching; point scene roots at `data/scenes` (or your layout).

**Use released predictions** — download `inference_result.zip` from the paper supplementary materials and extract under `result/inference/` so `inference_result/…` matches the layout above.

```bash
unzip inference_result.zip -d result/inference   # adjust if the archive top-level differs
```

---

## Benchmarks

### Numerical evaluation (D1–D5)

From the IDEAL repo root:

```bash
# Single scene — upserts one row in results.db only
python benchmarks/evaluation/evaluate.py \
    --gt    data/scenes/bedroom/bedroom_s14704319/GT.json \
    --pred  result/inference/inference_result/bedroom/bedroom_s14704319/gemini-2.5-pro.json \
    --model gemini-2.5-pro

# Batch — all models under result/inference/inference_result/ (default); --output adds per_model/*.csv
python benchmarks/evaluation/evaluate.py --batch --all-models --output
```

**Artifacts under `result/evaluation/`**

Single-scene mode:

- `results.db` — default; override with `--db`.

Batch mode:

- `results.db`
- `summary.json`
- `per_roomtype/<room>.json`
- `per_model/<model>.csv` — only with `--output`.

### Judge VLM (`run_judge_vlm_claude.py`)

Defaults: read scenes under **`result/judge_vlm/input/`**, write runs under **`result/judge_vlm/output/`** (each run creates `run_<timestamp>/…`).

### Scene reconstruction

Use **`reconstruct.py` only** (do not invoke `compose.py` / `render.py` by hand unless you are developing those modules). By default it reads predictions from `result/inference/inference_result/` and GT scene folders under `data/scenes/`.

```bash
python benchmarks/reconstruction/reconstruct.py --model-id gemini-2.5-pro
# omit --model-id to run every discovered model stem
```

Outputs are written next to each prediction JSON; see the docstring in `benchmarks/reconstruction/reconstruct.py` for filenames and options.

---

## Repository layout

```
.
├── benchmarks/
│   ├── common/           # config helpers, matching, camera transform
│   ├── evaluation/       # evaluate.py, numerical metrics, exports
│   └── reconstruction/ # entry: reconstruct.py (internal compose/render modules)
├── configs/              # paths, constants, GPU helpers
├── data/
│   ├── scenes/           # IDEAL-Scenes (e.g. HF mirror)
│   └── scenes_raw/       # default output of generation/
├── generation/           # procedural pipeline + preprocess filters
├── inference/            # prompts/; add inference drivers here as needed
├── infinigen-ideal/      # git submodule (Infinigen)
├── result/
│   ├── inference/inference_result/  # predictions (bulk usually gitignored; see .gitignore)
│   ├── judge_vlm/input/             # default judge-VLM scene folders (GT.png + candidate PNGs)
│   ├── judge_vlm/output/            # default judge-VLM runs
│   ├── evaluation/                    # evaluate.py outputs
│   └── reconstruction/                # optional local reconstruction artifacts
├── scripts/              # e.g. download_scenes_hf.py
├── requirements.txt
└── README.md
```

---

## Citation

```bibtex
@misc{cai2026idealbenchindoordatasetevaluation,
      title={IDEAL-Bench: Indoor Dataset and Evaluation suite for Analyzing 3D Layout reasoning},
      author={Yuening Cai and Junwei Zhou and Youran Qu and Yu-Wing Tai},
      year={2026},
      eprint={2607.03614},
      archivePrefix={arXiv},
      primaryClass={cs.CV},
      url={https://arxiv.org/abs/2607.03614},
  }
```

---

## Acknowledgement

Built on [Infinigen](https://github.com/princeton-vl/infinigen) for procedural scene generation and [Blender](https://www.blender.org/) for rendering.
