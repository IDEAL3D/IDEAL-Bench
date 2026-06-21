#!/usr/bin/env python3
"""API inference wrapper for GLM-4.6V.

This wrapper reuses the OpenAI-compatible inference flow implemented in
`gpt4o_api.py` while setting GLM-specific defaults.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from gpt4o_api import (
    DEFAULT_IMAGE_NAME,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROTOCOL,
    DEFAULT_RETRIES,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    infer_gpt4o,
    run_batch_inference,
    save_result_json,
)
from infer import DEFAULT_SCENES_ROOT

DEFAULT_MODEL_NAME = "glm-4.6v"
DEFAULT_BASE_URL = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GLM-4.6V API inference")
    parser.add_argument("--image", default=None, help="Path to input image (single mode)")
    parser.add_argument("--scene-id", default=None, help="Scene ID for metadata lookup in single mode")
    parser.add_argument("--scenes-root", default=DEFAULT_SCENES_ROOT)
    parser.add_argument("--output", default="prediction_glm46v.json")

    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--output-root", default="data/inference_result")
    parser.add_argument("--image-name", default=DEFAULT_IMAGE_NAME)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-scenes", type=int, default=None)

    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--protocol", default=DEFAULT_PROTOCOL, choices=("floor", "camera", "room_centered", "camera_centered"))
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)

    parser.add_argument("--api-key", default=None, help="GLM API key (or set GLM_API_KEY / OPENAI_API_KEY)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible GLM endpoint")
    return parser.parse_args()


def _resolve_key(cli_key: str | None) -> str:
    key = (cli_key or os.environ.get("GLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("Missing API key: pass --api-key or set GLM_API_KEY")
    return key


def main() -> int:
    args = _parse_args()
    key = _resolve_key(args.api_key)

    if args.batch:
        success, total = run_batch_inference(
            scenes_root=args.scenes_root,
            output_root=args.output_root,
            image_name=args.image_name,
            protocol=args.protocol,
            model_name=args.model_name,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            max_tokens=args.max_tokens,
            retries=args.retries,
            overwrite=args.overwrite,
            max_scenes=args.max_scenes,
            api_key=key,
            base_url=args.base_url,
        )
        print(f"Done: {success}/{total} scenes succeeded")
        return 0 if success == total else 1

    if not args.image:
        raise ValueError("Single mode requires --image")

    result = infer_gpt4o(
        image_path=args.image,
        model_name=args.model_name,
        protocol=args.protocol,
        scenes_root=args.scenes_root,
        scene_id=args.scene_id,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        max_tokens=args.max_tokens,
        retries=args.retries,
        api_key=key,
        base_url=args.base_url,
    )

    output_path = Path(args.output)
    save_result_json(result, str(output_path))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved to: {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
