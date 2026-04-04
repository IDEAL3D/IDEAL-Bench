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
from configs.constant import ROOM_TYPES, canonicalize_asset_category  # noqa: E402

"""
infer.py — 业务逻辑（只做推理入口，不做评测）

1. 扫描场景根目录下一级子目录；若存在约定图像文件名则作为候选任务（scene_id = 目录名）。
2. 从同目录 `objects_in_view.json`（或旧版 `info.json`）读取 `categories_in_view` 作为 **assets_list**；并注入 **allowed_room_types**。
3. 自 CSV 读取所选模型的 provider / 密钥等；将 system prompt + **user 文案（assets_list + allowed_room_types）** 与图像一并发送。**不把场景目录名传给模型**；Gemini 上传前将图片复制到临时中性路径，避免 API 侧携带含场景名的路径。
4. 将模型返回解析为 JSON 并保存为 `result/{protocol}/{scene_id}/{model_id}.json`（仓库根下 `result/`，与同场景其它产物并列）。

用法示例（在仓库根 IDL3D/ 下）：
  cd /scratch2/f0080vw/IDL3D/inference

  python infer.py --model gemini-3-flash-preview --protocol floor
  python infer.py --model gpt-5.2-2025-12-11 --protocol camera --scene-prefix bedroom
  python infer.py --model gemini-3-flash-preview --protocol camera --scene-prefix bedroom
  python infer.py --protocol floor --scene bedroom_s1412833192

输出路径（按场景聚合；可与重建图、benchmark 结果同目录并列）：
  result/{protocol}/{scene_id}/{model_id}.json
"""

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_SCENES_ROOT = os.path.join(PROJECT_DIR, "data", "scenes2test")
SCENE_IMAGE_NAME = "image.png"
OBJECTS_IN_VIEW_JSON = "objects_in_view.json"
INFO_JSON_LEGACY = "info.json"

# 与生成管线 canonical 一致（`configs/constant.py` `ROOM_TYPES`）
ALLOWED_ROOM_TYPES = tuple(ROOM_TYPES)

PROTOCOL_PROMPT_FILES = {
    "floor": "system_prompt_floor.md",
    "camera": "system_prompt_camera.md",
}


def default_api_keys_path() -> str:
    return os.path.join(PROJECT_DIR, "api", "secrets", "api_keys.csv")


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
    out_dir = os.path.join(PROJECT_DIR, "result", protocol, scene_id)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{mid}.json")


def load_system_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_categories_in_view(scenes_root: str, scene_id: str) -> list[str] | None:
    """
    读取 ``objects_in_view.json``（或旧 ``info.json``）的 categories_in_view。
    返回 None：两文件均无或字段非列表；返回 []：列表为空（调用方应跳过）。
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
    """user 文案：assets_list + allowed_room_types；不泄露场景目录名。"""
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
    """
    复制到临时文件，文件名不含场景目录名，降低 Gemini 文件 API 把路径带入上下文的概率。
    调用方负责在使用后 os.unlink。
    """
    suffix = os.path.splitext(image_path)[1] or ".png"
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="idl3d_")
    os.close(fd)
    shutil.copyfile(image_path, tmp)
    return tmp


def discover_scenes(scenes_root: str) -> list[tuple[str, str]]:
    """
    扫描 scenes_root 下的一级子目录，收集存在 SCENE_IMAGE_NAME 的场景。
    返回 [(scene_id, image_abspath), ...]，按 scene_id 排序。
    """
    if not os.path.isdir(scenes_root):
        raise FileNotFoundError(f"场景根目录不存在或不是目录: {scenes_root}")

    pairs: list[tuple[str, str]] = []
    for name in sorted(os.listdir(scenes_root)):
        sub = os.path.join(scenes_root, name)
        if not os.path.isdir(sub):
            continue
        img = os.path.join(sub, SCENE_IMAGE_NAME)
        if os.path.isfile(img):
            pairs.append((name, os.path.abspath(img)))
        else:
            print(f"⚠️ 跳过（缺少 {SCENE_IMAGE_NAME}）: {name}")
    return pairs


def tasks_for_named_scenes(
    scenes_root: str, scene_ids: list[str]
) -> list[tuple[str, str]]:
    """
    只包含用户指定的 scene_id（子目录名），顺序与首次出现顺序一致（去重）。
    若目录不存在或缺少约定图像则退出进程。
    """
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
        img = os.path.join(sub, SCENE_IMAGE_NAME)
        if not os.path.isdir(sub):
            print(f"❌ 场景目录不存在: {sid}")
            raise SystemExit(1)
        if not os.path.isfile(img):
            print(f"❌ 缺少 {SCENE_IMAGE_NAME}: {sid}")
            raise SystemExit(1)
        out.append((sid, os.path.abspath(img)))
    if not out:
        print("❌ --scene 未指定有效场景名")
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
    raise ValueError(f"在 {config_path} 里找不到模型: {model_name}")


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


# Gemini 503/429 等：服务端过载或限流，适合退避重试
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
        print(f"❌ 找不到文件: {image_path}")
        return False

    print(f"🚀 正在上传图片: {os.path.basename(image_path)}...")

    for attempt in range(GEMINI_MAX_RETRIES):
        upload_path = _copy_image_to_temp_for_upload(image_path)
        try:
            client = genai.Client(api_key=api_key)
            media_file = client.files.upload(file=upload_path)

            while media_file.state.name == "PROCESSING":
                print("⏳ Gemini 正在处理图片...", end="\r")
                time.sleep(2)
                media_file = client.files.get(name=media_file.name)

            if media_file.state.name == "FAILED":
                print("\n❌ 图片处理失败")
                return False

            print("\n✅ 图片就绪，开始执行推理...")

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
                    "_error": "模型未返回有效内容",
                    "_raw": getattr(response, "text", "") or str(response),
                }

            save_json(result, output_file)
            print(f"✨ 成功！已保存至: {output_file}")
            return True

        except Exception as e:
            if not _retryable_gemini_error(e) or attempt >= GEMINI_MAX_RETRIES - 1:
                print(f"\n❌ 运行出错: {e}")
                return False
            delay = min(
                GEMINI_RETRY_BASE_SEC * (2**attempt), GEMINI_RETRY_MAX_SEC
            )
            print(
                f"\n⚠️ API 暂不可用（503/限流等），{delay}s 后自动重试 "
                f"({attempt + 1}/{GEMINI_MAX_RETRIES})…"
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
        print(f"❌ 找不到文件: {image_path}")
        return False

    print(f"\n🚀 正在处理图片: {os.path.basename(image_path)}...")

    try:
        b64, mime = encode_image(image_path)
        print("✅ 图片已编码，开始执行推理...")

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
        print(f"✨ 成功！已保存至: {output_file}")
        return True

    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
        return False


def parse_args():
    p = argparse.ArgumentParser(
        description="从 data/scenes2test 各场景的 image.png 生成场景 JSON（Gemini / OpenAI）。"
    )
    p.add_argument(
        "--scenes-root",
        default=DEFAULT_SCENES_ROOT,
        help=f"场景根目录（默认: {DEFAULT_SCENES_ROOT}）",
    )
    p.add_argument(
        "--model",
        default="gemini-3-flash-preview",
        help="api_keys.csv 中的模型名称",
    )
    p.add_argument(
        "--config",
        default=default_api_keys_path(),
        help="API 配置文件路径",
    )
    p.add_argument(
        "--protocol",
        choices=tuple(PROTOCOL_PROMPT_FILES.keys()),
        default="floor",
        help="坐标/提示词协议：对应 prompts/ 下 system_prompt_{protocol}.md",
    )
    p.add_argument(
        "--scene-prefix",
        default=None,
        metavar="PREFIX",
        help=(
            "只处理场景子目录名以「PREFIX_」开头的项，例如 bedroom 只跑 bedroom_*；"
            "省略则处理全部有效场景（若未使用 --scene）"
        ),
    )
    p.add_argument(
        "--scene",
        action="append",
        dest="scenes_explicit",
        metavar="SCENE_ID",
        help=(
            "只跑指定场景子目录名（与 --scenes-root 下文件夹名一致），可多次传入多个；"
            "若指定则忽略 --scene-prefix"
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    scenes_root = os.path.abspath(args.scenes_root)

    try:
        cfg = load_config(args.model, args.config)
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
        raise SystemExit(1) from e

    try:
        if args.scenes_explicit:
            tasks = tasks_for_named_scenes(scenes_root, args.scenes_explicit)
        else:
            tasks = discover_scenes(scenes_root)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        raise SystemExit(1) from e

    if not args.scenes_explicit and args.scene_prefix:
        pfx = args.scene_prefix.strip().lower()
        tasks = [
            (sid, pth)
            for sid, pth in tasks
            if sid.lower().startswith(pfx + "_") or sid.lower() == pfx
        ]
        if not tasks:
            print(f"❌ 无场景匹配 --scene-prefix {args.scene_prefix!r}")
            raise SystemExit(1)

    if not tasks:
        print(
            f"❌ 在 {scenes_root} 下未找到任何含 {SCENE_IMAGE_NAME} 的场景子目录。"
        )
        raise SystemExit(1)

    valid_tasks: list[tuple[str, str, list[str]]] = []
    for scene_id, img_path in tasks:
        cats = load_categories_in_view(scenes_root, scene_id)
        if cats is None:
            print(
                f"⚠️ 跳过（缺少 {OBJECTS_IN_VIEW_JSON} / {INFO_JSON_LEGACY} 或 categories_in_view 无效）: {scene_id}"
            )
            continue
        if not cats:
            print(f"⚠️ 跳过（categories_in_view 为空）: {scene_id}")
            continue
        valid_tasks.append((scene_id, img_path, cats))

    if not valid_tasks:
        print(
            f"❌ 无可用场景：每个含 {SCENE_IMAGE_NAME} 的目录还需有效 "
            f"{OBJECTS_IN_VIEW_JSON}（或 {INFO_JSON_LEGACY}）与非空 categories_in_view。"
        )
        raise SystemExit(1)

    provider = cfg["provider"]
    model_name = cfg["model"]
    mid = model_id_for_path(model_name)
    total = len(valid_tasks)
    prompt_path = system_prompt_path_for_protocol(args.protocol)
    system_prompt = load_system_prompt(prompt_path)

    print(f"📋 共 {total} 个场景待处理（已过滤无 info / 无类别列表）")
    print(f"📁 场景根: {scenes_root}")
    print(f"📁 protocol: {args.protocol}")
    print(
        f"📁 输出: result/{args.protocol}/<scene_id>/{mid}.json\n"
    )

    success = 0
    if provider == "gemini":
        for i, (scene_id, img_path, cats) in enumerate(valid_tasks, 1):
            print(f"[{i}/{total}] scene_id={scene_id} ", end="")
            out = prediction_json_path(args.protocol, model_name, scene_id)
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
            up = build_user_prompt(cats)
            if process_openai(
                img_path, client, model_name, out, system_prompt, up
            ):
                success += 1

    else:
        print(f"❌ 不支持的 provider: {provider}（支持: gemini / openai）")
        raise SystemExit(1)

    print(f"\n{'=' * 50}")
    print(f"✅ 处理完成: {success}/{total} 个场景成功")
    if success < total:
        print(f"❌ 失败: {total - success} 个场景")


if __name__ == "__main__":
    main()
