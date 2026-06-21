import argparse
import base64
import csv
import json
import os
import shutil
import sys
import tempfile
import time

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)
from configs.constants import ROOM_TYPES, canonicalize_asset_category  # noqa: E402
from inview2sp import inject_into_system_prompt  # noqa: E402

"""
Inference CLI: discover scenes, call Gemini or OpenAI, write
``result/inference/inference_result/<room_type>/<scene_id>/<model>.json``.

Reads ``image.png`` and ``categories_in_view`` from each scene dir; user message carries
``assets_list`` + ``allowed_room_types``. System prompt template ``prompts/system_prompt_room_centered.md``
gets ``{{THIS_ROOM_CONTAINS}}`` filled via ``inview2sp.inject_into_system_prompt``. Scene IDs
and paths are not sent as hints to the model; Gemini uploads use a temp copy with a neutral name.

Scene roots may be nested ``<scenes_root>/<room_type>/<scene_id>/`` (matches ``data/scenes``) or
flat ``<scenes_root>/<scene_id>/`` (room type inferred from the folder name prefix).

Example:

cd inference

python infer.py --model gemini-3-flash-preview


"""

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_SCENES_ROOT = os.path.join(PROJECT_DIR, "data", "scenes")
DEFAULT_RESULT_ROOT = os.path.join(
    PROJECT_DIR, "result", "inference", "inference_result"
)
SCENE_IMAGE_NAME = "image.png"
OBJECTS_IN_VIEW_JSON = "objects_in_view.json"
INFO_JSON_LEGACY = "info.json"
GT_JSON = "GT.json"

# Same room-type vocabulary as ``configs.constant.ROOM_TYPES`` (generation pipeline).
ALLOWED_ROOM_TYPES = tuple(ROOM_TYPES)

SYSTEM_PROMPT_FILENAME = "system_prompt_room_centered.md"

_ROOM_TYPES_SET = frozenset(ROOM_TYPES)


def default_api_keys_path() -> str:
    return os.path.join(PROJECT_DIR, "api", "secrets", "api_keys.csv")


def model_id_for_path(model_name: str) -> str:
    return (
        model_name.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .strip()
    )


def system_prompt_path_for_protocol(_protocol: str | None = None) -> str:
    """Room-centered prompt only; *_protocol* is ignored (kept for ``*_api.py`` imports)."""
    return os.path.join(BASE_DIR, "prompts", SYSTEM_PROMPT_FILENAME)


def prediction_json_path(model_name: str, room_type: str, scene_id: str) -> str:
    mid = model_id_for_path(model_name)
    out_dir = os.path.join(DEFAULT_RESULT_ROOT, room_type, scene_id)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{mid}.json")


def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_categories_in_view(
    scenes_root: str,
    scene_id: str,
    *,
    room_type: str | None = None,
) -> list[str] | None:
    """Load categories from metadata files under a scene directory.

    If ``room_type`` is set, the scene dir is ``scenes_root/room_type/scene_id``; otherwise
    ``scenes_root/scene_id`` (flat layout, used by ``*_api.py`` callers).

    Search order:
    1) ``objects_in_view.json`` / ``info.json`` with ``categories_in_view`` list.
    2) ``GT.json`` with ``scene.objects[*].category`` fallback (for filtered datasets).

    Returns ``None`` if no supported source exists; ``[]`` if source exists but no categories.
    """
    if room_type:
        base = os.path.join(scenes_root, room_type, scene_id)
    else:
        base = os.path.join(scenes_root, scene_id)
    path = None
    for name in (OBJECTS_IN_VIEW_JSON, INFO_JSON_LEGACY):
        cand = os.path.join(base, name)
        if os.path.isfile(cand):
            path = cand
            break
    seen: set[str] = set()
    out: list[str] = []
    if path is not None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("categories_in_view")
        if not isinstance(raw, list):
            return None
        for x in raw:
            c = canonicalize_asset_category(str(x))
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        if out:
            return out

    gt_path = os.path.join(base, GT_JSON)
    if not os.path.isfile(gt_path):
        return None if path is None else out
    with open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)
    scene = gt.get("scene") if isinstance(gt, dict) else None
    objects = scene.get("objects") if isinstance(scene, dict) else None
    if not isinstance(objects, list):
        return out

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        c = canonicalize_asset_category(str(obj.get("category", "")))
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def build_user_prompt(assets_list: list[str]) -> str:
    """User text: assets_list + allowed_room_types (no scene id or path hints)."""
    assets_str = ", ".join(assets_list)
    rooms_str = ", ".join(ALLOWED_ROOM_TYPES)
    return (
        f"assets_list: {assets_str}\n\n"
        f"allowed_room_types: {rooms_str}\n\n"
        "For scene.room.type, choose exactly one string from allowed_room_types that best "
        "matches the room **visible in the image only**. Do not infer room type from folder "
        "names, file paths, or any metadata—there is no such hint in this request.\n\n"
        "Infer object counts and geometry only from the image. "
        "Output the scene JSON as specified in the system instructions."
    )


def _copy_image_to_temp_for_upload(image_path: str) -> str:
    """Copy image to a temp file with a neutral name for Gemini upload; caller must unlink."""
    suffix = os.path.splitext(image_path)[1] or ".png"
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="ideal_")
    os.close(fd)
    shutil.copyfile(image_path, tmp)
    return tmp


def _infer_room_type_from_flat_scene_id(scene_id: str) -> str | None:
    """``bedroom_s14704319`` → ``bedroom``; longest ``ROOM_TYPES`` prefix wins."""
    for rt in sorted(ROOM_TYPES, key=len, reverse=True):
        if scene_id == rt or scene_id.startswith(f"{rt}_"):
            return rt
    return None


def discover_scenes(scenes_root: str) -> list[tuple[str, str, str]]:
    """List scenes as ``(room_type, scene_id, image_abs_path)``.

    Nested layout: ``<root>/<room_type>/<scene_id>/image.png`` for ``room_type`` in ``ROOM_TYPES``.
    Flat layout: ``<root>/<scene_id>/image.png`` with room type inferred from ``scene_id``.
    """
    if not os.path.isdir(scenes_root):
        raise FileNotFoundError(f"Scenes root is missing or not a directory: {scenes_root}")

    triples: list[tuple[str, str, str]] = []
    for name in sorted(os.listdir(scenes_root)):
        sub = os.path.join(scenes_root, name)
        if not os.path.isdir(sub):
            continue
        if name in _ROOM_TYPES_SET:
            for child in sorted(os.listdir(sub)):
                cdir = os.path.join(sub, child)
                if not os.path.isdir(cdir):
                    continue
                img = os.path.join(cdir, SCENE_IMAGE_NAME)
                if os.path.isfile(img):
                    triples.append((name, child, os.path.abspath(img)))
                else:
                    print(f"Skip (no {SCENE_IMAGE_NAME}): {name}/{child}")
        else:
            img = os.path.join(sub, SCENE_IMAGE_NAME)
            if os.path.isfile(img):
                rt = _infer_room_type_from_flat_scene_id(name)
                if rt is None:
                    print(
                        f"Skip (cannot infer room_type from folder name; "
                        f"use nested data/scenes/<room_type>/<scene_id>/): {name}"
                    )
                    continue
                triples.append((rt, name, os.path.abspath(img)))
            else:
                print(f"Skip (no {SCENE_IMAGE_NAME}): {name}")
    return triples


def tasks_for_named_scenes(
    scenes_root: str, scene_ids: list[str]
) -> list[tuple[str, str, str]]:
    """Resolve explicit scene ids to ``(room_type, scene_id, image_abs_path)``."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in scene_ids:
        sid = raw.strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        ordered.append(sid)

    out: list[tuple[str, str, str]] = []
    for sid in ordered:
        direct = os.path.join(scenes_root, sid)
        direct_img = os.path.join(direct, SCENE_IMAGE_NAME)
        if os.path.isdir(direct) and os.path.isfile(direct_img):
            rt = _infer_room_type_from_flat_scene_id(sid)
            if rt is None:
                print(
                    f"Error: flat scene {sid!r} under {scenes_root!r}: "
                    "folder name must start with a ROOM_TYPES prefix (e.g. bedroom_s123)."
                )
                raise SystemExit(1)
            out.append((rt, sid, os.path.abspath(direct_img)))
            continue

        found: tuple[str, str, str] | None = None
        for rt in sorted(_ROOM_TYPES_SET):
            nested = os.path.join(scenes_root, rt, sid)
            nimg = os.path.join(nested, SCENE_IMAGE_NAME)
            if os.path.isdir(nested) and os.path.isfile(nimg):
                found = (rt, sid, os.path.abspath(nimg))
                break
        if found is None:
            print(f"Error: scene not found (flat or under any ROOM_TYPES): {sid}")
            raise SystemExit(1)
        out.append(found)

    if not out:
        print("Error: --scene had no valid scene ids")
        raise SystemExit(1)
    return out


def load_config(model_name: str, config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("model") or "").strip() == model_name:
                return {
                    "provider": (row.get("provider") or "").strip().lower(),
                    "api_key": (row.get("api_key") or "").strip(),
                    "model": (row.get("model") or "").strip(),
                    "base_url": (row.get("base_url") or "").strip(),
                }
    raise ValueError(f"Model not found in {config_path}: {model_name}")


def save_json(data: dict, output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def try_parse_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# Gemini 503/429 overload or rate limits: retry with backoff
GEMINI_MAX_RETRIES = 5
GEMINI_RETRY_BASE_SEC = 10
GEMINI_RETRY_MAX_SEC = 120


def _retryable_gemini_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(
        x in msg
        for x in ("503", "429", "unavailable", "overloaded", "rate limit", "resource exhausted")
    ):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in (408, 429, 503, 529)


def process_gemini(
    image_path: str,
    api_key: str,
    model_name: str,
    output_file: str,
    system_prompt: str,
    user_prompt: str,
) -> bool:
    from google import genai
    from google.genai import types

    if not os.path.isfile(image_path):
        print(f"Error: file not found: {image_path}")
        return False

    print(f"Uploading image: {os.path.basename(image_path)}...")

    for attempt in range(GEMINI_MAX_RETRIES):
        upload_path = _copy_image_to_temp_for_upload(image_path)
        try:
            client = genai.Client(api_key=api_key)
            media_file = client.files.upload(file=upload_path)

            while media_file.state.name == "PROCESSING":
                print("Gemini processing image...", end="\r")
                time.sleep(2)
                media_file = client.files.get(name=media_file.name)

            if media_file.state.name == "FAILED":
                print("\nError: image processing failed")
                return False

            print("\nImage ready, running inference...")

            response = client.models.generate_content(
                model=model_name,
                contents=[media_file, user_prompt],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )

            result = response.parsed
            if result is None and response.text:
                try:
                    result = try_parse_json(response.text)
                except json.JSONDecodeError:
                    result = {"_raw_response": response.text}
            if result is None:
                result = {
                    "_error": "Model returned no parseable content",
                    "_raw": getattr(response, "text", "") or str(response),
                }

            save_json(result, output_file)
            print(f"OK saved: {output_file}")
            return True

        except Exception as e:
            if not _retryable_gemini_error(e) or attempt >= GEMINI_MAX_RETRIES - 1:
                print(f"\nError: {e}")
                return False
            delay = min(
                GEMINI_RETRY_BASE_SEC * (2**attempt), GEMINI_RETRY_MAX_SEC
            )
            print(
                f"\nAPI unavailable (503/rate limit); retry in {delay}s "
                f"({attempt + 1}/{GEMINI_MAX_RETRIES})"
            )
            time.sleep(delay)
        finally:
            try:
                os.unlink(upload_path)
            except OSError:
                pass

    return False


def encode_image(image_path: str) -> tuple[str, str]:
    ext = os.path.splitext(image_path)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), mime


def process_openai(
    image_path: str,
    client,
    model_name: str,
    output_file: str,
    system_prompt: str,
    user_prompt: str,
) -> bool:
    if not os.path.isfile(image_path):
        print(f"Error: file not found: {image_path}")
        return False

    print(f"\nProcessing image: {os.path.basename(image_path)}...")

    try:
        b64, mime = encode_image(image_path)
        print("Image encoded, running inference...")

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )

        content = response.choices[0].message.content
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            try:
                result = try_parse_json(content)
            except json.JSONDecodeError:
                result = {"_raw_response": content}

        save_json(result, output_file)
        print(f"OK saved: {output_file}")
        return True

    except Exception as e:
        print(f"\nError: {e}")
        return False


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "Run Gemini or OpenAI on scene image.png + metadata; "
            "write JSON under result/inference/inference_result/."
        )
    )
    p.add_argument(
        "--scenes-root",
        default=DEFAULT_SCENES_ROOT,
        help=f"Root folder of scene subdirs (default: {DEFAULT_SCENES_ROOT})",
    )
    p.add_argument(
        "--model",
        default="gemini-3-flash-preview",
        help="Model name as listed in api_keys.csv",
    )
    p.add_argument(
        "--config",
        default=default_api_keys_path(),
        help="Path to api_keys.csv",
    )
    p.add_argument(
        "--scene-prefix",
        default=None,
        metavar="PREFIX",
        help=(
            "Only scenes whose directory name starts with PREFIX_ (e.g. bedroom -> bedroom_*). "
            "Ignored if --scene is set."
        ),
    )
    p.add_argument(
        "--scene",
        action="append",
        dest="scenes_explicit",
        metavar="SCENE_ID",
        help=(
            "Run only these scene folder names (e.g. bedroom_s14704319); repeat flag for multiple. "
            "Looks under --scenes-root/<scene_id>/ or --scenes-root/<room_type>/<scene_id>/. "
            "Overrides --scene-prefix."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    scenes_root = os.path.abspath(args.scenes_root)

    try:
        cfg = load_config(args.model, args.config)
    except Exception as e:
        print(f"Error loading config: {e}")
        raise SystemExit(1) from e

    try:
        if args.scenes_explicit:
            tasks = tasks_for_named_scenes(scenes_root, args.scenes_explicit)
        else:
            tasks = discover_scenes(scenes_root)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from e

    if not args.scenes_explicit and args.scene_prefix:
        pfx = args.scene_prefix.strip().lower()
        tasks = [
            (rt, sid, pth)
            for rt, sid, pth in tasks
            if sid.lower().startswith(pfx + "_") or sid.lower() == pfx
        ]
        if not tasks:
            print(f"Error: no scenes match --scene-prefix {args.scene_prefix!r}")
            raise SystemExit(1)

    if not tasks:
        print(
            f"Error: no scene subdirs with {SCENE_IMAGE_NAME} under {scenes_root} "
            f"(expected nested <room_type>/<scene_id>/ or flat <scene_id>/)."
        )
        raise SystemExit(1)

    valid_tasks: list[tuple[str, str, str, list[str]]] = []
    for room_type, scene_id, img_path in tasks:
        cats = load_categories_in_view(
            scenes_root, scene_id, room_type=room_type
        )
        label = f"{room_type}/{scene_id}"
        if cats is None:
            print(
                f"Skip (missing or invalid {OBJECTS_IN_VIEW_JSON}/{INFO_JSON_LEGACY} "
                f"or categories_in_view): {label}"
            )
            continue
        if not cats:
            print(f"Skip (empty categories_in_view): {label}")
            continue
        valid_tasks.append((room_type, scene_id, img_path, cats))

    if not valid_tasks:
        print(
            f"Error: no runnable scenes. Each dir with {SCENE_IMAGE_NAME} needs "
            f"a valid {OBJECTS_IN_VIEW_JSON} or {INFO_JSON_LEGACY} with non-empty categories_in_view."
        )
        raise SystemExit(1)

    provider = cfg["provider"]
    model_name = cfg["model"]
    mid = model_id_for_path(model_name)
    total = len(valid_tasks)
    prompt_path = system_prompt_path_for_protocol()
    base_system_prompt = load_system_prompt(prompt_path)

    print(f"Scenes to run: {total} (after filtering missing metadata)")
    print(f"Scenes root: {scenes_root}")
    print(
        f"Output: result/inference/inference_result/<room_type>/<scene_id>/{mid}.json\n"
    )

    success = 0
    if provider == "gemini":
        for i, (room_type, scene_id, img_path, cats) in enumerate(valid_tasks, 1):
            print(f"[{i}/{total}] {room_type}/{scene_id} ", end="")
            out = prediction_json_path(model_name, room_type, scene_id)
            system_prompt = inject_into_system_prompt(base_system_prompt, cats)
            up = build_user_prompt(cats)
            if process_gemini(
                img_path,
                cfg["api_key"],
                model_name,
                out,
                system_prompt,
                up,
            ):
                success += 1

    elif provider == "openai":
        from openai import OpenAI

        kwargs = {"api_key": cfg["api_key"]}
        if cfg.get("base_url"):
            kwargs["base_url"] = cfg["base_url"]
        client = OpenAI(**kwargs)

        for i, (room_type, scene_id, img_path, cats) in enumerate(valid_tasks, 1):
            print(f"[{i}/{total}] {room_type}/{scene_id} ", end="")
            out = prediction_json_path(model_name, room_type, scene_id)
            system_prompt = inject_into_system_prompt(base_system_prompt, cats)
            up = build_user_prompt(cats)
            if process_openai(
                img_path, client, model_name, out, system_prompt, up
            ):
                success += 1

    else:
        print(f"Error: unsupported provider {provider} (use gemini or openai)")
        raise SystemExit(1)

    print(f"\n{'=' * 50}")
    print(f"Done: {success}/{total} scenes succeeded")
    if success < total:
        print(f"Failed: {total - success} scenes")


if __name__ == "__main__":
    main()
