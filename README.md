# IDEAL: Indoor Dataset for Evaluating Analysis by 3D Layout Reasoning

<p align="center">
  <a href="[PAPER_URL]">[📄 Paper]</a> &nbsp;|&nbsp;
  <a href="https://github.com/cyn-xyz/IDEAL">[🌐 Project Page]</a> &nbsp;|&nbsp;
  <a href="https://huggingface.co/datasets/cyn-xyz/IDEAL_Scenes_RAW">[🤗 Dataset]</a>
</p>

IDEAL is a synthetic indoor scene dataset and benchmark for evaluating the **3D spatial understanding** of multimodal large language models (MLLMs). Given a single rendered indoor image and a structured system prompt, models are asked to predict the 3D layout of the scene in JSON format. Results are assessed through both **hard geometric metrics** and **soft perceptual metrics** via a Judge VLM.

---

## Overview

The full pipeline consists of five stages:

1. **Scene Synthesis** — Synthetic indoor scenes (10 room types, 1,000 scenes) generated via a modified [Infinigen](https://github.com/princeton-vl/infinigen), each producing a `scene.blend` and a rendered image.
2. **Preprocessing** — Ground truth extraction (`GT.json`, `objects_in_view.json`) with automatic overlap filtering.
3. **Inference** — A rendered image and system prompt are passed to the target MLLM, which outputs a structured scene description (`{model_id}.json`).
4. **Reconstruction** — The predicted layout is written back into the original `.blend` file and re-rendered under the same camera and lighting conditions.
5. **Evaluation** — Hard metrics (overlap rate, out-of-bounds rate, position discrepancy) and soft metrics (spatial impression, depth perception) are computed.

---

## Dataset

The dataset is hosted on Hugging Face: **[[🤗 HF_DATASET_URL]]**

| Split | Scenes | Room Types |
|-------|--------|------------|
| Full  | 1,000  | bedroom, living room, kitchen, bathroom, dining room, home studio, open office, meeting room, classroom, library |

Each scene contains:
- `scene.blend` — Blender scene file
- `image.png` — Rendered image (camera view)
- `GT.json` — Ground truth layout (positions, dimensions, rotations)
- `objects_in_view.json` — Visible asset list and camera metadata

---

## Installation

```bash
git clone --recurse-submodules https://github.com/[YOUR_USERNAME]/IDEAL
cd IDEAL
```

**Full install** (scene generation + inference + reconstruction):
```bash
pip install -r requirements.txt
```

**Minimal install** (inference + reconstruction only, no local scene generation):
```bash
pip install -r requirements-minimal.txt
```

> Requires Linux x86_64. Scene synthesis requires an NVIDIA GPU.

---

## Quick Start

### Run inference on a scene

```bash
python inference/infer.py \
  --scene data/scenes2test/bedroom_s1234/ \
  --protocol floor \
  --model gemini-2.0-flash
```

Predictions are written to `result/floor/{scene_id}/{model_id}.json`.

### Reconstruct and render

```bash
python reconstruction/reconstruct.py --protocol floor
```

### Evaluate (coming soon)

```bash
python benchmarks/evaluation/evaluate.py --protocol floor
```

---

## Repository Structure

```
IDEAL/
├── generation/          # Scene synthesis, filtering, and related scripts
├── inference/           # VLM inference scripts and system prompts
├── reconstruction/      # Layout-to-Blender reconstruction scripts
├── benchmarks/          # Evaluation metrics and leaderboard (WIP)
├── configs/             # Path configs, JSON Schema, constants
├── docs/                # Geometric conventions and design documents
├── data/                # Scene data (managed via HF Dataset)
├── result/              # Inference and reconstruction outputs (local)
└── infinigen-ideal/     # Modified Infinigen (Git submodule)
```

---

## Evaluation Metrics

### Hard Metrics
| Metric | Description |
|--------|-------------|
| Object Overlap Rate | Fraction of objects with geometric overlap |
| Out-of-Bounds Rate | Fraction of objects placed outside room walls |
| Position Discrepancy | Mean positional error vs. ground truth |
| Object Recognition Rate | Fraction of visible objects correctly identified |
| Hallucination Rate | Fraction of scenes with hallucinated objects |

### Soft Metrics (Judge VLM)
A single Judge VLM call receives the ground truth image alongside all reconstructed images and scores each model on:
- **Overall Spatial Impression** (1–5): Does the reconstruction convey the same room type and layout?
- **Spatial Depth and Layering** (1–5): Are foreground, midground, and background naturally distinguishable?

---

## Citation

```bibtex
@article{ideal2025,
  title     = {IDEAL: A Synthetic Indoor Dataset for Evaluating and Analysing Layout},
  author    = {[AUTHORS]},
  year      = {2025}
}
```

---

## License

To be determined upon publication.