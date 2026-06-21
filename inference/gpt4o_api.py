"""API inference wrapper for GPT-4o following gemma4_31b_it_api.py flow.

Logic parity with gemma4 script:
- same prompt/category resolution path
- same retry policy and JSON repair/quality checks
- same single/batch CLI surface and output format
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

from openai import OpenAI

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from configs.constant import canonicalize_asset_category
from infer import (
    ALLOWED_ROOM_TYPES,
    DEFAULT_SCENES_ROOT,
    build_user_prompt,
    load_categories_in_view,
    load_system_prompt,
    model_id_for_path,
    system_prompt_path_for_protocol,
)
from inview2sp import inject_into_system_prompt

DEFAULT_MODEL_NAME = "gpt-4o"
DEFAULT_PROTOCOL = "floor"
DEFAULT_IMAGE_NAME = "color.png"
DEFAULT_RETRIES = 3
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.1
DEFAULT_TOP_P = 0.95
DEFAULT_TOP_K = 64

_OPENAI_CACHE: dict[str, Any] = {}


def _chat_create_with_token_compat(
    client: OpenAI,
    *,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    image_url: str,
    temperature: float,
    top_p: float,
):
    common_kwargs = dict(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
        top_p=top_p,
    )

    # Intentionally do not pass max_tokens/max_completion_tokens.
    return client.chat.completions.create(**common_kwargs), "omitted"


def _normalize_protocol_alias(protocol: str) -> str:
    aliases = {
        "room_centered": "floor",
        "camera_centered": "camera",
    }
    return aliases.get(protocol, protocol)


def _resolve_categories(
    *,
    categories_in_view: list[str] | None,
    scenes_root: str,
    scene_id: str | None,
) -> list[str]:
    if categories_in_view is not None:
        seen: set[str] = set()
        out: list[str] = []
        for x in categories_in_view:
            c = canonicalize_asset_category(str(x))
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    if not scene_id:
        raise ValueError(
            "categories_in_view is required when scene_id is not provided. "
            "Pass --categories-in-view or provide --scene-id and --scenes-root."
        )

    cats = load_categories_in_view(scenes_root, scene_id)
    if cats is None or not cats:
        raise ValueError(
            f"Could not load non-empty categories_in_view for scene_id={scene_id!r} "
            f"under scenes_root={scenes_root!r}."
        )
    return cats


def _resolve_prompts(
    *,
    system_prompt: str | None,
    user_prompt: str | None,
    protocol: str,
    categories_in_view: list[str],
) -> tuple[str, str]:
    normalized_protocol = _normalize_protocol_alias(protocol)

    resolved_system_prompt = (system_prompt or "").strip()
    if not resolved_system_prompt:
        base_system_prompt = load_system_prompt(
            system_prompt_path_for_protocol(normalized_protocol)
        )
        resolved_system_prompt = inject_into_system_prompt(
            base_system_prompt, categories_in_view
        )

    resolved_user_prompt = (user_prompt or "").strip()
    if not resolved_user_prompt:
        resolved_user_prompt = build_user_prompt(categories_in_view)

    return resolved_system_prompt, resolved_user_prompt


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _try_parse_with_repair(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        blocks = raw.split("```")
        if len(blocks) >= 2:
            raw = blocks[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    candidate = _extract_first_json_object(text) or text
    candidate = candidate.strip()
    candidate = re.sub(r"^```(?:json)?", "", candidate.strip(), flags=re.IGNORECASE)
    candidate = re.sub(r"```$", "", candidate.strip())
    candidate = candidate.strip()

    try:
        return json.loads(candidate)
    except Exception:
        pass

    opens = candidate.count("{")
    closes = candidate.count("}")
    if opens > closes:
        candidate = candidate + ("}" * (opens - closes))
    return json.loads(candidate)


def _contains_placeholder_tokens(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"\b(?:W|D|H|x|y|z)\b", value))
    if isinstance(value, list):
        return any(_contains_placeholder_tokens(x) for x in value)
    if isinstance(value, dict):
        return any(_contains_placeholder_tokens(v) for v in value.values())
    return False


def _normalize_prediction(result: Dict[str, Any]) -> Dict[str, Any]:
    def _to_float(x: Any, default: float = 0.0) -> float:
        try:
            return float(x)
        except Exception:
            return default

    def _coerce_vec3(value: Any) -> list[float]:
        if not isinstance(value, list):
            return [0.0, 0.0, 0.0]
        nums = [_to_float(v) for v in value]
        if len(nums) >= 3:
            return nums[:3]
        while len(nums) < 3:
            nums.append(0.0)
        return nums

    scene = result.get("scene")
    if not isinstance(scene, dict):
        return result

    room = scene.get("room")
    objects = scene.get("objects")
    if not isinstance(objects, list):
        scene["objects"] = []
        objects = scene["objects"]

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if "position" in obj:
            obj["position"] = _coerce_vec3(obj.get("position"))
        if "dimensions" in obj:
            obj["dimensions"] = _coerce_vec3(obj.get("dimensions"))

    if isinstance(room, dict):
        room["asset_count"] = len(objects)
    return result


def _prediction_quality_ok(result: Dict[str, Any]) -> bool:
    scene = result.get("scene")
    if not isinstance(scene, dict):
        return False

    room = scene.get("room")
    objects = scene.get("objects")
    if not isinstance(room, dict) or not isinstance(objects, list):
        return False

    room_type = room.get("type")
    if room_type not in ALLOWED_ROOM_TYPES:
        return False

    if _contains_placeholder_tokens(result):
        return False

    if room.get("asset_count") != len(objects):
        return False

    return True


def _encode_image_data_url(image_path: str) -> str:
    ext = os.path.splitext(image_path)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _load_openai_runtime(api_key: str, base_url: str | None):
    key = f"{api_key}::{base_url or ''}"
    if key in _OPENAI_CACHE:
        return _OPENAI_CACHE[key]

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    _OPENAI_CACHE[key] = client
    return client


def infer_gpt4o(
    *,
    image_path: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    model_name: str = DEFAULT_MODEL_NAME,
    protocol: str = DEFAULT_PROTOCOL,
    categories_in_view: list[str] | None = None,
    scenes_root: str = DEFAULT_SCENES_ROOT,
    scene_id: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    top_k: int = DEFAULT_TOP_K,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    retries: int = DEFAULT_RETRIES,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Dict[str, Any]:
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("Missing API key: pass --api-key or set OPENAI_API_KEY")

    client = _load_openai_runtime(key, base_url)

    resolved_categories = _resolve_categories(
        categories_in_view=categories_in_view,
        scenes_root=scenes_root,
        scene_id=scene_id,
    )
    resolved_system_prompt, resolved_user_prompt = _resolve_prompts(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        protocol=protocol,
        categories_in_view=resolved_categories,
    )

    image_url = _encode_image_data_url(image_path)

    attempts = max(1, int(retries))
    last_text = ""
    last_meta: dict[str, Any] = {}
    last_parsed: Dict[str, Any] | None = None

    for attempt in range(attempts):
        attempt_temp = temperature if attempt == 0 else 0.0
        attempt_tokens = min(max_tokens * (2**attempt), 4096)

        try:
            response, token_param_name = _chat_create_with_token_compat(
                client,
                model_name=model_name,
                system_prompt=resolved_system_prompt,
                user_prompt=resolved_user_prompt,
                image_url=image_url,
                temperature=attempt_temp,
                top_p=top_p,
            )
            text_out = response.choices[0].message.content or ""
        except Exception as e:
            last_text = str(e)
            last_meta = {
                "attempt": attempt + 1,
                "attempt_tokens": attempt_tokens,
                "attempt_temperature": attempt_temp,
            }
            continue

        last_text = str(text_out).strip()
        last_meta = {
            "attempt": attempt + 1,
            "attempt_tokens": attempt_tokens,
            "attempt_temperature": attempt_temp,
            "token_param": token_param_name,
            "top_k_ignored": top_k,
        }

        try:
            parsed = _normalize_prediction(_try_parse_with_repair(last_text))
            if _prediction_quality_ok(parsed):
                return parsed
            last_parsed = parsed
        except Exception:
            continue

    if last_parsed is not None:
        return last_parsed

    return {
        "_error": "Response is not valid JSON",
        "_raw_response": last_text,
        "_meta": last_meta,
    }


def save_result_json(result: Dict[str, Any], output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _discover_scene_ids(scenes_root: str, image_name: str) -> list[str]:
    if not os.path.isdir(scenes_root):
        raise FileNotFoundError(f"Batch scenes root not found: {scenes_root}")

    out: list[str] = []
    for name in sorted(os.listdir(scenes_root)):
        scene_dir = os.path.join(scenes_root, name)
        if not os.path.isdir(scene_dir):
            continue
        image_path = os.path.join(scene_dir, image_name)
        if not os.path.isfile(image_path):
            continue
        out.append(name)
    return out


def run_batch_inference(
    *,
    scenes_root: str,
    output_root: str,
    image_name: str,
    protocol: str,
    model_name: str,
    temperature: float,
    top_p: float,
    top_k: int,
    max_tokens: int,
    retries: int,
    overwrite: bool,
    max_scenes: int | None,
    api_key: str | None,
    base_url: str | None,
) -> tuple[int, int]:
    scene_ids = _discover_scene_ids(scenes_root, image_name)
    if max_scenes is not None and max_scenes > 0:
        scene_ids = scene_ids[:max_scenes]

    model_tag = model_id_for_path(model_name)
    total = len(scene_ids)
    if total == 0:
        print(f"No runnable scenes found in: {scenes_root}")
        return 0, 0

    print(f"Batch scenes: {total}")
    print(f"Input root: {scenes_root}")
    print(f"Output root: {output_root}")
    print(f"Image file: {image_name}")
    print(f"Model file name: {model_tag}.json")

    success = 0
    for idx, scene_id in enumerate(scene_ids, 1):
        scene_dir = os.path.join(scenes_root, scene_id)
        image_path = os.path.join(scene_dir, image_name)
        out_dir = os.path.join(output_root, scene_id)
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, f"{model_tag}.json")

        if os.path.isfile(output_path) and not overwrite:
            print(f"[{idx}/{total}] skip existing: {scene_id}")
            success += 1
            continue

        print(f"[{idx}/{total}] run: {scene_id}")
        try:
            result = infer_gpt4o(
                image_path=image_path,
                system_prompt=None,
                user_prompt=None,
                model_name=model_name,
                protocol=protocol,
                categories_in_view=None,
                scenes_root=scenes_root,
                scene_id=scene_id,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                retries=retries,
                api_key=api_key,
                base_url=base_url,
            )
            save_result_json(result, output_path)
            success += 1
        except Exception as e:
            print(f"[{idx}/{total}] failed: {scene_id} -> {e}")

    return success, total


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GPT-4o API inference (Gemma-logic aligned)")
    parser.add_argument("--image", default=None, help="Path to input image (single mode)")
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Optional inline system prompt override; if omitted, load from prompts/*.md by --protocol",
    )
    parser.add_argument(
        "--user-prompt",
        default=None,
        help="Optional inline user prompt override; if omitted, build exactly as infer.py",
    )
    parser.add_argument(
        "--protocol",
        choices=("floor", "camera", "room_centered", "camera_centered"),
        default=DEFAULT_PROTOCOL,
        help="Prompt protocol (aliases room_centered->floor, camera_centered->camera)",
    )
    parser.add_argument(
        "--categories-in-view",
        nargs="*",
        default=None,
        help="Optional category list; if omitted, load categories exactly as infer.py",
    )
    parser.add_argument(
        "--scenes-root",
        default=DEFAULT_SCENES_ROOT,
        help="Scene root used with --scene-id to load objects_in_view/info metadata",
    )
    parser.add_argument(
        "--scene-id",
        default=None,
        help="Scene folder name under --scenes-root used to load categories_in_view",
    )
    parser.add_argument("--output", default=None, help="Path to output JSON (single mode)")
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME, help="OpenAI model id")
    parser.add_argument("--api-key", default=None, help="OpenAI API key (or set OPENAI_API_KEY)")
    parser.add_argument("--base-url", default=None, help="Optional OpenAI-compatible base URL")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)

    parser.add_argument("--batch-scenes-root", default=None, help="Root dir containing scene_id subfolders")
    parser.add_argument("--batch-output-root", default=None, help="Root dir to write predictions by scene_id")
    parser.add_argument("--image-name", default=DEFAULT_IMAGE_NAME, help="Image filename inside each scene folder")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing prediction files")
    parser.add_argument("--max-scenes", type=int, default=None, help="Optional cap for number of scenes in batch")

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    is_batch = bool(args.batch_scenes_root or args.batch_output_root)

    if is_batch:
        if not args.batch_scenes_root or not args.batch_output_root:
            raise SystemExit("Batch mode requires both --batch-scenes-root and --batch-output-root")
        success, total = run_batch_inference(
            scenes_root=os.path.abspath(args.batch_scenes_root),
            output_root=os.path.abspath(args.batch_output_root),
            image_name=args.image_name,
            protocol=args.protocol,
            model_name=args.model,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            max_tokens=args.max_tokens,
            retries=args.retries,
            overwrite=args.overwrite,
            max_scenes=args.max_scenes,
            api_key=args.api_key,
            base_url=args.base_url,
        )
        print(f"Done: {success}/{total} scenes succeeded")
        return

    if not args.image or not args.output:
        raise SystemExit("Single mode requires --image and --output")

    result = infer_gpt4o(
        image_path=args.image,
        system_prompt=args.system_prompt,
        user_prompt=args.user_prompt,
        model_name=args.model,
        protocol=args.protocol,
        categories_in_view=args.categories_in_view,
        scenes_root=args.scenes_root,
        scene_id=args.scene_id,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        max_tokens=args.max_tokens,
        retries=args.retries,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    save_result_json(result, args.output)
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
