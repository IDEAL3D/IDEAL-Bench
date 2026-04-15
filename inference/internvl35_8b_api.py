"""Local inference wrapper for InternVL3.5-8B via LMDeploy.

This module uses LMDeploy's native local pipeline and does not rely on a local
HTTP server.

Quick use:
    from internvl35_8b_api import infer_internvl35_8b
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict

from lmdeploy import PytorchEngineConfig, pipeline
from lmdeploy.vl import load_image

from infer import (
    DEFAULT_SCENES_ROOT,
    build_user_prompt,
    load_categories_in_view,
    load_system_prompt,
    model_id_for_path,
    system_prompt_path_for_protocol,
)
from inview2sp import inject_into_system_prompt
from configs.constant import canonicalize_asset_category

DEFAULT_MODEL_NAME = "OpenGVLab/InternVL3_5-8B"
DEFAULT_TP = 1
DEFAULT_SESSION_LEN = 32768
DEFAULT_PROTOCOL = "floor"
DEFAULT_IMAGE_NAME = "color.png"

_PIPELINE_CACHE: dict[tuple[str, int, int], Any] = {}


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


def _try_parse_json_text(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        blocks = raw.split("```")
        if len(blocks) >= 2:
            raw = blocks[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
    return json.loads(raw)


def _get_pipeline(model_name: str, tp: int, session_len: int):
    key = (model_name, int(tp), int(session_len))
    if key not in _PIPELINE_CACHE:
        _PIPELINE_CACHE[key] = pipeline(
            model_name,
            backend_config=PytorchEngineConfig(
                session_len=session_len,
                tp=tp,
                max_batch_size=1,
                cache_max_entry_count=0.8,
            ),
        )
    return _PIPELINE_CACHE[key]


def infer_internvl35_8b(
    *,
    image_path: str,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    model_name: str = DEFAULT_MODEL_NAME,
    protocol: str = DEFAULT_PROTOCOL,
    categories_in_view: list[str] | None = None,
    scenes_root: str = DEFAULT_SCENES_ROOT,
    scene_id: str | None = None,
    tp: int = DEFAULT_TP,
    session_len: int = DEFAULT_SESSION_LEN,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Run one inference request on InternVL3.5-8B via local LMDeploy.

    Returns parsed JSON dict if possible. If parsing fails, returns a dict with
    `_raw_response` for later inspection.
    """
    img = load_image(image_path)

    pipe = _get_pipeline(model_name=model_name, tp=tp, session_len=session_len)

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
    prompt = f"{resolved_system_prompt}\n\n{resolved_user_prompt}"
    response = pipe((prompt, img), temperature=temperature, max_new_tokens=max_tokens)
    content = response.text if hasattr(response, "text") else str(response)

    try:
        return _try_parse_json_text(content)
    except Exception:
        return {
            "_error": "Response is not valid JSON",
            "_raw_response": content,
            "_raw_object": repr(response),
        }


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
    tp: int,
    session_len: int,
    temperature: float,
    max_tokens: int,
    overwrite: bool,
    max_scenes: int | None,
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

    # Warmup/create model once, then reuse in-loop.
    _get_pipeline(model_name=model_name, tp=tp, session_len=session_len)

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
            result = infer_internvl35_8b(
                image_path=image_path,
                system_prompt=None,
                user_prompt=None,
                model_name=model_name,
                protocol=protocol,
                categories_in_view=None,
                scenes_root=scenes_root,
                scene_id=scene_id,
                tp=tp,
                session_len=session_len,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            save_result_json(result, output_path)
            success += 1
        except Exception as e:
            print(f"[{idx}/{total}] failed: {scene_id} -> {e}")

    return success, total


def save_result_json(result: Dict[str, Any], output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one InternVL3.5-8B local LMDeploy inference")
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
    parser.add_argument("--batch-scenes-root", default=None, help="Root dir containing scene_id subfolders")
    parser.add_argument("--batch-output-root", default=None, help="Root dir to write predictions by scene_id")
    parser.add_argument("--image-name", default=DEFAULT_IMAGE_NAME, help="Image filename inside each scene folder")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing prediction files")
    parser.add_argument("--max-scenes", type=int, default=None, help="Optional cap for number of scenes in batch")
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME, help="Model id from Hugging Face")
    parser.add_argument("--tp", type=int, default=DEFAULT_TP, help="Tensor parallel size")
    parser.add_argument("--session-len", type=int, default=DEFAULT_SESSION_LEN, help="LMDeploy session length")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=2048)
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
            tp=args.tp,
            session_len=args.session_len,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            overwrite=args.overwrite,
            max_scenes=args.max_scenes,
        )
        print(f"Done: {success}/{total} scenes succeeded")
        return

    if not args.image or not args.output:
        raise SystemExit("Single mode requires --image and --output")

    result = infer_internvl35_8b(
        image_path=args.image,
        system_prompt=args.system_prompt,
        user_prompt=args.user_prompt,
        model_name=args.model,
        protocol=args.protocol,
        categories_in_view=args.categories_in_view,
        scenes_root=args.scenes_root,
        scene_id=args.scene_id,
        tp=args.tp,
        session_len=args.session_len,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    save_result_json(result, args.output)
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
