# IDEAL Inference Model Test List (VLM)

Last updated: 2026-04-12

Notes:
- This list is for IDEAL single-image scene understanding and structured JSON output tasks.
- The closed-source section prioritizes mainstream models that can be called directly via API.
- The open-source section lists mainstream deployable VLMs from the community (typically via vLLM / SGLang / TGI / self-hosted services).
- Release date uses official release/public availability time; some models may have preview vs GA date differences.

## 1) Mainstream Closed-Source VLM APIs (Priority)

| Vendor | Model Version (Example) | Release Date | Capability Positioning (Brief) | API Docs | Price (Input/Output per 1M tokens) |
|---|---|---|---|---|---|
| OpenAI | gpt-5.4 pro | 2026-03 | Latest-generation multimodal reasoning, strong structured output | https://developers.openai.com/api/docs/models/gpt-5.4-pro| 30$/180$ |
| OpenAI | gpt-5.4 | 2026-03 | Latest-generation multimodal reasoning, strong structured output | https://developers.openai.com/api/docs/models/gpt-5.4| 2.5$/15$ |
| OpenAI | gpt-4.1 | 2025-04 | smartest model without reasoning | https://developers.openai.com/api/docs/models/gpt-4.1 | 2$/8$ |
| Anthropic | claude-4.6-opus | 2026-02 | High-quality understanding and complex constraint following | https://docs.anthropic.com/en/docs/about-claude/models | 5$/25$ |
| Anthropic | claude-4.6-sonnet | 2026-02 | Cost-effective version, balanced reasoning and speed | https://docs.anthropic.com/en/docs/about-claude/models | 3$/15$ |
| Google | gemini-3.1-pro | 2026-02 | Strong reasoning and multimodal understanding | https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview?hl=zh-cn | 2$/12$ |
| Google | gemini-3-flash | 2025-12 | Low latency and high throughput | https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview?hl=zh-cn | 0.5$/3$ |
| xAI | grok-4.20 | 2026-02 | New-generation Grok family reasoning capability | https://docs.x.ai/developers/models| 2$/6$ |

## 2) Mainstream Open-Source VLMs (Self-Hosted / Third-Party Hosted)

| Family | Model Version (Example) | Release Date | Open-Source URL  | API/Deployment Docs | Code File |
|---|---|---|---|---|---|
| DeepSeek | DeepSeek-Janus-pro-7B | 2025-02 |https://huggingface.co/deepseek-ai/Janus-Pro-7B| https://github.com/deepseek-ai/Janus | inference/deepseek_janus_pro_api.py (infer_deepseek_janus_pro) |
| Qwen | Qwen3-VL-30B-A3B-Instruct | 2025-11 | https://huggingface.co/collections/Qwen/qwen3-vl | https://qwen.readthedocs.io | High-quality primary open-source candidate |
| Qwen | Qwen3-VL-8B-Instruct | 2025-11 | https://huggingface.co/collections/Qwen/qwen3-vl | https://qwen.readthedocs.io | Low-cost high-throughput candidate |
| LLaVA | LLaVA-Next v1.6 Mistral 7B | 2025-01 | https://huggingface.co/llava-hf/llava-v1.6-mistral-7b-hf | https://huggingface.co/docs/transformers/main/model_doc/llava_next | inference/llava_next_mistral_7b_api.py (infer_llava_next_mistral_7b) |
| InternVL | InternVL3.5-30B-A3B | 2025-08 | https://huggingface.co/collections/OpenGVLab/internvl35 | https://internvl.readthedocs.io | inference/internvl35_30b_a3b_api.py (infer_internvl35_30b_a3b) |
| InternVL | InternVL3.5-8B | 2025-08 | https://huggingface.co/collections/OpenGVLab/internvl35| https://internvl.readthedocs.io | inference/internvl35_8b_api.py (infer_internvl35_8b) |
| Google | Gemma 4 31B IT | 2026-04 | https://huggingface.co/google/gemma-4-31B-it | https://ai.google.dev/gemma/docs | inference/gemma4_31b_it_api.py (infer_gemma4_31b_it) |

## 3) Recommended Test Priority in IDEAL

### P0 (Run First)
- Open-source: `Qwen3-VL-30B-A3B-Instruct`, `InternVL3.5-30B-A3B`, `DeepSeek-VL2`
- Closed-source: `gpt-5.4 pro`, `claude-4.6-sonnet`, `gemini-3.1-pro`, `grok-4.20`

### P1 (Expansion)
- Open-source: `Qwen3-VL-8B-Instruct`, `InternVL3.5-8B`
- Closed-source: `gpt-4.1`, `gemini-3-flash`, `claude-4.6-opus`

### Suggested Execution Order (Open-Source First, Then Closed-Source)
1. Open-source large-model baselines (Qwen3-VL-30B / InternVL3.5-30B / DeepSeek-VL2)
2. Open-source small-model throughput baselines (Qwen3-VL-8B / InternVL3.5-8B)
3. Closed-source high-quality comparison (GPT / Claude / Gemini / Grok)
4. Perform 3-way comparison of cost, accuracy, and stability under the same protocol

## 4) Interface Alignment Recommendations (For This Project)

- Unified input: single `image.png` + `objects_in_view.json` (or its category list) + system prompt template.
- Unified output: strict JSON (recommend post-validation with `configs/schema/single_scene_v1.json`).
- Unified logging: model name, version, temperature, top_p, max_tokens, inference latency, retry count.
- Unified evaluation: report separately by protocol (floor / camera), and track parsing failure rate (JSON parse errors / missing fields).
- For open-source phase, additionally record: VRAM usage, tokens/s, and average TTFT.
- For closed-source phase, additionally record: per-call cost, 429/5xx retry rate, and service stability.

## 5) Remarks

- Model availability, naming, and pricing change frequently; re-check official model listing pages before testing.
- If you need an offline reproducible baseline, start with fixed combinations:
  - Open-source: `Qwen3-VL-30B-A3B-Instruct` + `InternVL3.5-30B-A3B` + `DeepSeek-VL2`
  - Closed-source comparison: `gpt-4.1` + `claude-4.6-sonnet` + `gemini-3.1-pro`
- If budget is limited, keep only one closed-source comparator (recommend `gpt-4.1` or `gemini-3-flash`) and prioritize the open-source matrix for the rest.
