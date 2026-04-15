# IDEAL 推理模型测试清单（VLM）

更新时间：2026-04-12

说明：
- 本清单面向 IDEAL 的单图场景理解与结构化 JSON 输出任务。
- 闭源部分优先列出可直接通过 API 调用的主流模型。
- 开源部分列出社区主流可部署 VLM（通常通过 vLLM / SGLang / TGI / 自建服务提供接口）。
- 发布日期以“官方发布/公开可用时间”为准，个别模型可能存在预览版与 GA 时间差。

## 1) 闭源主流 VLM API（优先）

| 厂商 | 模型版本（示例） | 发布时间 | 能力定位（简） | API 文档 | 价格(Input/Output per 1M tokens) |
|---|---|---|---|---|---|
| OpenAI | gpt-5.4 pro | 2026-03 | 最新代多模态推理、结构化输出强 | https://developers.openai.com/api/docs/models/gpt-5.4-pro| 30$/180$ |
| OpenAI | gpt-5.4 | 2026-03 | 最新代多模态推理、结构化输出强 | https://developers.openai.com/api/docs/models/gpt-5.4| 2.5$/15$ |
| OpenAI | gpt-4.1 | 2025-04 | smartest model without reasoning | https://developers.openai.com/api/docs/models/gpt-4.1 | 2$/8$ |
| Anthropic | claude-4.6-opus | 2026-02 | 高质量理解与复杂约束遵循 | https://docs.anthropic.com/en/docs/about-claude/models | 5$/25$ |
| Anthropic | claude-4.6-sonnet | 2026-02 | 性价比版本，推理与速度平衡 | https://docs.anthropic.com/en/docs/about-claude/models | 3$/15$ |
| Google | gemini-3.1-pro | 2026-02 | 强推理与多模态理解 | https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview?hl=zh-cn | 2$/12$ |
| Google | gemini-3-flash | 2025-12 | 低延迟高吞吐 | https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview?hl=zh-cn | 0.5$/3$ |
| xAI | grok-4.20 | 2026-02 | 新一代 Grok 系列推理能力 | https://docs.x.ai/developers/models| 2$/6$ |

## 2) 开源主流 VLM（自部署/第三方托管）

| 系列 | 模型版本（示例） | 发布时间 | 开源地址  | API/部署文档 | 代码文件 |
|---|---|---|---|---|---|
| DeepSeek | DeepSeek-Janus-pro-7B | 2025-02 |https://huggingface.co/deepseek-ai/Janus-Pro-7B| https://docs.vllm.ai | 需按具体 checkpoint 选型 |
| Qwen | Qwen3-VL-30B-A3B-Instruct | 2025-11 | https://huggingface.co/collections/Qwen/qwen3-vl | https://qwen.readthedocs.io | 高质量主力开源候选 |
| Qwen | Qwen3-VL-8B-Instruct | 2025-11 | https://huggingface.co/collections/Qwen/qwen3-vl | https://qwen.readthedocs.io | 低成本高吞吐候选 |
| InternVL | InternVL3.5-30B-A3B | 2025-08 | https://huggingface.co/collections/OpenGVLab/internvl35 | https://internvl.readthedocs.io | inference/internvl35_30b_a3b_api.py (infer_internvl35_30b_a3b) |
| InternVL | InternVL3.5-8B | 2025-08 | https://huggingface.co/collections/OpenGVLab/internvl35| https://internvl.readthedocs.io | inference/internvl35_8b_api.py (infer_internvl35_8b) |

## 3) 建议在 IDEAL 中的测试优先级

### P0（先跑）
- 开源：`Qwen3-VL-30B-A3B-Instruct`、`InternVL3.5-30B-A3B`、`DeepSeek-VL2`
- 闭源：`gpt-5.4 pro`、`claude-4.6-sonnet`、`gemini-3.1-pro`、`grok-4.20`

### P1（扩展）
- 开源：`Qwen3-VL-8B-Instruct`、`InternVL3.5-8B`
- 闭源：`gpt-4.1`、`gemini-3-flash`、`claude-4.6-opus`

### 执行顺序建议（先开源后闭源）
1. 开源大模型基线（Qwen3-VL-30B / InternVL3.5-30B / DeepSeek-VL2）
2. 开源小模型吞吐基线（Qwen3-VL-8B / InternVL3.5-8B）
3. 闭源高质量对照（GPT / Claude / Gemini / Grok）
4. 在同一协议下做成本-精度-稳定性三维对比

## 4) 接口对齐建议（给本项目）

- 统一输入：单张 `image.png` + `objects_in_view.json`（或其 category 列表）+ 系统提示模板。
- 统一输出：严格 JSON（建议绑定 `configs/schema/single_scene_v1.json` 进行后验校验）。
- 统一记录：模型名、版本、温度、top_p、max_tokens、推理时延、失败重试次数。
- 统一评测：协议分开统计（floor / camera），并记录解析失败率（JSON 无法解析/字段缺失）。
- 开源优先阶段建议额外记录：显存占用、tokens/s、平均首 token 延迟（TTFT）。
- 闭源阶段建议额外记录：单次调用成本、429/5xx 重试率、服务稳定性。

## 5) 备注

- 具体模型可用性、命名和计费策略经常更新，实测前请再次核对官方模型列表页。
- 若需要离线可复现基线，建议先固定开源组合：
  - 开源：`Qwen3-VL-30B-A3B-Instruct` + `InternVL3.5-30B-A3B` + `DeepSeek-VL2`
  - 闭源对照：`gpt-4.1` + `claude-4.6-sonnet` + `gemini-3.1-pro`
- 若预算受限，可仅保留 1 个闭源对照（建议 `gpt-4.1` 或 `gemini-3-flash`），其余优先跑开源矩阵。
