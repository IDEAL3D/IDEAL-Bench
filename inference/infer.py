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
Inference CLI: discover scenes, call Gemini or OpenAI, write ``result/inference/{protocol}/{scene}/{model}.json``.

Reads an RGB frame (``image.png`` preferred, else ``color.png`` as in ``data/scenes``) and
``categories_in_view`` from ``objects_in_view.json`` (or legacy ``info.json``) per scene dir.
``--protocol`` selects the coordinate-system prompt under ``prompts/`` (room-centered vs camera-centered).
User message carries ``assets_list`` + ``allowed_room_types``. System prompt template gets
``{{THIS_ROOM_CONTAINS}}`` filled via ``inview2sp.inject_into_system_prompt``. Scene IDs and paths
are not sent as hints to the model; Gemini uploads use a temp copy with a neutral name.

Example:

cd inference

python infer.py --model gemini-3-flash-preview --protocol floor

"""

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_SCENES_ROOT = os.path.join(PROJECT_DIR, "data", "scenes")
# Prefer explicit inference name; fall back to render output from the generation pipeline.
SCENE_IMAGE_CANDIDATES: tuple[str, ...] = ("image.png", "color.png")
OBJECTS_IN_VIEW_JSON = "objects_in_view.json"
INFO_JSON_LEGACY = "info.json"

# Same room-type vocabulary as ``configs.constants.ROOM_TYPES`` (generation pipeline).
ALLOWED_ROOM_TYPES = tuple(ROOM_TYPES)

PROTOCOL_PROMPT_FILES = {
    "floor": "system_prompt_room_centered.md",
    "camera": "system_prompt_camera_centered.md",
    "room_centered": "system_prompt_room_centered.md",
    "camera_centered": "system_prompt_camera_centered.md",
}


def default_api_keys_path() -> str:
    return os.path.join(PROJECT_DIR, "[INTERNAL]api", "secrets", "api_keys.csv")


def model_id_for_path(model_name: str) -> str:
    return (
        model_name.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .strip()
    )


def system_prompt_path_for_protocol(protocol: str) -> str:
    fname = PROTOCOL_PROMPT_FILES[protocol]
    return os.path.join(BASE_DIR, "prompts", fname)


def prediction_json_path(protocol: str, model_name: str, scene_id: str) -> str:
    mid = model_id_for_path(model_name)
    # protocol = coordinate-frame choice; matches prompts/ and result/inference/<protocol>/
    out_dir = os.path.join(PROJECT_DIR, "result", "inference", protocol, scene_id)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{mid}.json")


def resolve_scene_image_path(scene_dir: str) -> str | None:
    """First existing file among ``SCENE_IMAGE_CANDIDATES`` under ``scene_dir``; else ``None``."""
    for name in SCENE_IMAGE_CANDIDATES:
        cand = os.path.join(scene_dir, name)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_categories_in_view(scenes_root: str, scene_id: str) -> list[str] | None:
    """Load ``categories_in_view`` from ``objects_in_view.json`` or legacy ``info.json``.

    Returns ``None`` if neither file exists or the field is not a list; ``[]`` if empty.
    """
    base = os.path.join(scenes_root, scene_id)
    path = None
    for name in (OBJECTS_IN_VIEW_JSON, INFO_JSON_LEGACY):
        cand = os.path.join(base, name)
        if os.path.isfile(cand):
            path = cand
            break
    if path is None:
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("categories_in_view")
    if not isinstance(raw, list):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        c = canonicalize_asset_category(str(x))
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


def discover_scenes(scenes_root: str) -> list[tuple[str, str]]:
    """List immediate subdirs under ``scenes_root`` that contain a usable RGB image."""
    if not os.path.isdir(scenes_root):
        raise FileNotFoundError(f"Scenes root is missing or not a directory: {scenes_root}")

    pairs: list[tuple[str, str]] = []
    for name in sorted(os.listdir(scenes_root)):
        sub = os.path.join(scenes_root, name)
        if not os.path.isdir(sub):
            continue
        img = resolve_scene_image_path(sub)
        if img:
            pairs.append((name, img))
        else:
            print(
                f"Skip (no RGB image; tried {', '.join(SCENE_IMAGE_CANDIDATES)}): {name}"
            )
    return pairs


def tasks_for_named_scenes(
    scenes_root: str, scene_ids: list[str]
) -> list[tuple[str, str]]:
    """Resolve explicit scene ids to (scene_id, image path); exit if dir or image missing."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in scene_ids:
        sid = raw.strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        ordered.append(sid)

    out: list[tuple[str, str]] = []
    for sid in ordered:
        sub = os.path.join(scenes_root, sid)
        if not os.path.isdir(sub):
            print(f"Error: scene directory not found: {sid}")
            raise SystemExit(1)
        img = resolve_scene_image_path(sub)
        if not img:
            print(
                f"Error: missing RGB image for {sid} (tried {', '.join(SCENE_IMAGE_CANDIDATES)})"
            )
            raise SystemExit(1)
        out.append((sid, img))
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
            "Run Gemini or OpenAI on scene RGB image (image.png or color.png) + objects_in_view.json; "
            "write JSON under result/inference/<protocol>/."
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
        "--protocol",
        choices=tuple(PROTOCOL_PROMPT_FILES.keys()),
        default="floor",
        help=(
            "Coordinate frame → prompts/system_prompt_*.md and result/inference/<protocol>/ "
            "(floor/room_centered = room-centered; camera/camera_centered = camera-centered)"
        ),
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
            "Run only these scene directory names under --scenes-root; repeat flag for multiple. "
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
            (sid, pth)
            for sid, pth in tasks
            if sid.lower().startswith(pfx + "_") or sid.lower() == pfx
        ]
        if not tasks:
            print(f"Error: no scenes match --scene-prefix {args.scene_prefix!r}")
            raise SystemExit(1)

    if not tasks:
        print(
            "Error: no scene subdirs with a usable RGB image "
            f"({', '.join(SCENE_IMAGE_CANDIDATES)}) under {scenes_root}."
        )
        raise SystemExit(1)

    valid_tasks: list[tuple[str, str, list[str]]] = []
    for scene_id, img_path in tasks:
        cats = load_categories_in_view(scenes_root, scene_id)
        if cats is None:
            print(
                f"Skip (missing or invalid {OBJECTS_IN_VIEW_JSON}/{INFO_JSON_LEGACY} "
                f"or categories_in_view): {scene_id}"
            )
            continue
        if not cats:
            print(f"Skip (empty categories_in_view): {scene_id}")
            continue
        valid_tasks.append((scene_id, img_path, cats))

    if not valid_tasks:
        print(
            "Error: no runnable scenes. Each dir needs a usable RGB image "
            f"({', '.join(SCENE_IMAGE_CANDIDATES)}) and a valid {OBJECTS_IN_VIEW_JSON} "
            f"or {INFO_JSON_LEGACY} with non-empty categories_in_view."
        )
        raise SystemExit(1)

    provider = cfg["provider"]
    model_name = cfg["model"]
    mid = model_id_for_path(model_name)
    total = len(valid_tasks)
    prompt_path = system_prompt_path_for_protocol(args.protocol)
    base_system_prompt = load_system_prompt(prompt_path)

    print(f"Scenes to run: {total} (after filtering missing metadata)")
    print(f"Scenes root: {scenes_root}")
    print(f"Protocol: {args.protocol}")
    print(
        f"Output: result/inference/{args.protocol}/<scene_id>/{mid}.json\n"
    )

    success = 0
    if provider == "gemini":
        for i, (scene_id, img_path, cats) in enumerate(valid_tasks, 1):
            print(f"[{i}/{total}] scene_id={scene_id} ", end="")
            out = prediction_json_path(args.protocol, model_name, scene_id)
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

        for i, (scene_id, img_path, cats) in enumerate(valid_tasks, 1):
            print(f"[{i}/{total}] scene_id={scene_id} ", end="")
            out = prediction_json_path(args.protocol, model_name, scene_id)
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
