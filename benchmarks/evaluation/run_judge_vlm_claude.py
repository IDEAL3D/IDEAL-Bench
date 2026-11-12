#!/usr/bin/env python3
"""Perceptual judge for IDEAL-Bench reconstructions.

For each sampled scene the judge sees the reference rendering
(data/scenes/<room>/<scene>/color.png) and the reconstructions of six models
(result/reconstruction/prediction/<room>/<scene>/<model>.png). Candidates are
shuffled per scene and shown only as image_1 ... image_6, so the judge never
sees model names. It returns one 1-5 score and a strict rank for every image.

Outputs, under result/judge_vlm/<run_name>/:
    run_config.json      settings of the run (judge model, seeds, prompt hash)
    sampled_scenes.txt   the sampled scenes, one <room>/<scene_id> per line
    scenes/<id>.json     per-scene image mapping, raw responses, parsed result
    judge_scores.csv     one row per scene: score and rank of every model
    judge_table.tex      mean score, mean rank and top-1 share per model
    run.log

Running the same command again resumes the run: scenes already marked ok are
skipped and failed ones are tried again. --summarize-only rebuilds the CSV and
the table from the scene files without calling the API.

The API key is read from ANTHROPIC_API_KEY, or from --api-key-file.

Usage (from the IDEAL root):
    python benchmarks/evaluation/run_judge_vlm_claude.py --limit 1
    python benchmarks/evaluation/run_judge_vlm_claude.py
    python benchmarks/evaluation/run_judge_vlm_claude.py --summarize-only
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOM_TYPES = [
    "bedroom",
    "homestudio",
    "livingroom",
    "kitchen",
    "bathroom",
    "diningroom",
    "office",
    "classroom",
    "meetingroom",
    "library",
]

# Model id (file stem of the reconstruction) -> name used in the leaderboard.
JUDGED_MODELS = {
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gpt-4o": "GPT-4o",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "gpt-5.4": "GPT-5.4",
    "google_gemma-4-31B-it": "Gemma-4-31B-IT",
    "qwen2.5-vl-72b-instruct": "Qwen2.5-VL-72B-Instruct",
}

SCORE_KEY = "overall_spatial_impression_similarity"
GT_IMAGE_NAME = "color.png"

# API limits: 5 MB per image and 32 MB per request.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024

# List prices in USD per million tokens; only used for the estimate in run.log.
PRICE_INPUT_PER_MTOK = 5.0
PRICE_OUTPUT_PER_MTOK = 25.0

# Settings that must match when resuming an existing run directory.
CONFIG_KEYS_TO_MATCH = (
    "judge_model",
    "sample_seed",
    "shuffle_seed",
    "scenes_per_room",
    "models",
    "prompt_sha256",
)

log = logging.getLogger("judge")


class InvalidResponse(Exception):
    """The judge answered, but not in the required format."""


class RequestTooLarge(Exception):
    """The images of a scene exceed the API size limits."""


@dataclass
class Prompt:
    system: str
    user_template: str
    sha256: str


# ---------------------------------------------------------------------------
# Paths, prompt, config
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Perceptual judge for IDEAL-Bench reconstructions.")
    p.add_argument("--ideal-root", default=None,
                   help="IDEAL repository root (default: two levels above this file)")
    p.add_argument("--scenes-root", default="data/scenes")
    p.add_argument("--recon-root", default="result/reconstruction/prediction")
    p.add_argument("--prompt-file",
                   default="inference/prompts/judge_vlm_perceptual_prompt_multi_model.md")
    p.add_argument("--output-root", default="result/judge_vlm")
    p.add_argument("--run-name", default="perceptual_200")
    p.add_argument("--model", default="claude-opus-4-7", help="judge model")
    p.add_argument("--scenes-per-room", type=int, default=20)
    p.add_argument("--sample-seed", type=int, default=2026)
    p.add_argument("--shuffle-seed", type=int, default=0)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=None,
                   help="not sent unless given; claude-opus-4-7 rejects this parameter")
    p.add_argument("--limit", type=int, default=0,
                   help="judge only the first N sampled scenes (for a test run)")
    p.add_argument("--api-key-file", default=None,
                   help="file containing the API key, used if ANTHROPIC_API_KEY is not set")
    p.add_argument("--summarize-only", action="store_true",
                   help="rebuild the CSV and the table from existing scene files")
    return p.parse_args()


def resolve(root: Path, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else root / p


def rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def read_section(text: str, header: str) -> str:
    """Body of a '## <header>' section of the prompt file."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == header
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def load_prompt(path: Path) -> Prompt:
    if not path.is_file():
        raise SystemExit(f"Prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    system = read_section(text, "System Prompt")
    user_template = read_section(text, "User Prompt Template")
    if not system or not user_template:
        raise SystemExit(f"{path} needs both '## System Prompt' and '## User Prompt Template' sections")
    if "{image_ids}" not in user_template:
        raise SystemExit(f"The user prompt template in {path} must contain {{image_ids}}")
    return Prompt(system, user_template, hashlib.sha256(text.encode("utf-8")).hexdigest())


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def check_run_config(run_dir: Path, config: dict[str, Any]) -> None:
    path = run_dir / "run_config.json"
    if path.is_file():
        old = load_json(path)
        diffs = [k for k in CONFIG_KEYS_TO_MATCH if old.get(k) != config.get(k)]
        if diffs:
            raise SystemExit(
                f"{path} was written with different settings ({', '.join(diffs)}). "
                "Use another --run-name for a new run."
            )
        return
    config = dict(config, created_at=datetime.now().isoformat(timespec="seconds"))
    write_json(path, config)


def setup_logging(log_path: Path) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def load_api_key(ideal_root: Path, key_file: str | None) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    if key_file:
        path = resolve(ideal_root, key_file)
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        raise SystemExit(f"API key file not found or empty: {path}")
    raise SystemExit("No API key: set ANTHROPIC_API_KEY or pass --api-key-file.")


# ---------------------------------------------------------------------------
# Scene sampling and candidate order
# ---------------------------------------------------------------------------


def complete_scenes(scenes_root: Path, recon_root: Path, room: str) -> list[str]:
    """Scenes of a room that have the reference image and all six reconstructions."""
    room_dir = scenes_root / room
    if not room_dir.is_dir():
        return []
    out = []
    for scene_dir in sorted(room_dir.iterdir()):
        if not scene_dir.is_dir() or scene_dir.name.startswith((".", "_")):
            continue
        if not (scene_dir / GT_IMAGE_NAME).is_file():
            continue
        recon_dir = recon_root / room / scene_dir.name
        if all((recon_dir / f"{m}.png").is_file() for m in JUDGED_MODELS):
            out.append(scene_dir.name)
    return out


def sample_scenes(scenes_root: Path, recon_root: Path, per_room: int, seed: int) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    picked: list[tuple[str, str]] = []
    short: list[str] = []
    for room in ROOM_TYPES:
        pool = complete_scenes(scenes_root, recon_root, room)
        if len(pool) < per_room:
            short.append(f"{room}: {len(pool)} complete scenes, need {per_room}")
            continue
        picked.extend((room, sid) for sid in sorted(rng.sample(pool, per_room)))
    if short:
        raise SystemExit(
            "Not enough complete scenes to sample from:\n  " + "\n  ".join(short)
            + "\nRun judge_precheck.py to see what is missing."
        )
    return picked


def read_sampled(path: Path) -> list[tuple[str, str]]:
    scenes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            room, scene_id = line.split("/", 1)
            scenes.append((room, scene_id))
    return scenes


def scene_shuffle_seed(base_seed: int, scene_id: str) -> int:
    # sha256 rather than hash(): Python's string hash changes between processes.
    digest = hashlib.sha256(f"{base_seed}:{scene_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def candidate_order(seed: int) -> list[str]:
    order = list(JUDGED_MODELS)
    random.Random(seed).shuffle(order)
    return order


# ---------------------------------------------------------------------------
# Request and response
# ---------------------------------------------------------------------------


def encode_png(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(data) > MAX_IMAGE_BYTES:
        raise RequestTooLarge(f"{path.name} is {len(data) / 1e6:.1f} MB after base64 encoding (limit 5 MB)")
    return data


def image_block(data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def build_content(user_template: str, gt_path: Path, candidates: list[Path]) -> list[dict[str, Any]]:
    image_ids = [f"image_{i}" for i in range(1, len(candidates) + 1)]
    content: list[dict[str, Any]] = [
        {"type": "text", "text": user_template.replace("{image_ids}", ", ".join(image_ids))},
        {"type": "text", "text": "GT image:"},
        image_block(encode_png(gt_path)),
    ]
    for image_id, path in zip(image_ids, candidates):
        content.append({"type": "text", "text": f"{image_id}:"})
        content.append(image_block(encode_png(path)))
    total = sum(len(b["source"]["data"]) for b in content if b["type"] == "image")
    if total > MAX_REQUEST_BYTES - 1024 * 1024:
        raise RequestTooLarge(f"images total {total / 1e6:.1f} MB after base64 encoding (limit 32 MB)")
    return content


def extract_json_array(text: str) -> str | None:
    """First balanced [...] span in the text, ignoring brackets inside strings."""
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def parse_judgement(text: str, image_map: dict[str, str]) -> dict[str, dict[str, int]]:
    """Validate the judge output and map it back from image ids to model ids."""
    array_text = extract_json_array(text)
    if array_text is None:
        raise InvalidResponse("no JSON array in the response")
    try:
        items = json.loads(array_text)
    except json.JSONDecodeError as e:
        raise InvalidResponse(f"invalid JSON: {e}") from e

    n = len(image_map)
    parsed: dict[str, tuple[int, int]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponse("array item is not an object")
        image_id = item.get("image_id")
        if image_id not in image_map:
            raise InvalidResponse(f"unknown image_id {image_id!r}")
        if image_id in parsed:
            raise InvalidResponse(f"duplicate image_id {image_id}")
        score = as_int(item.get(SCORE_KEY))
        rank = as_int(item.get("rank"))
        if score is None or not 1 <= score <= 5:
            raise InvalidResponse(f"{image_id}: score must be an integer from 1 to 5, got {item.get(SCORE_KEY)!r}")
        if rank is None:
            raise InvalidResponse(f"{image_id}: rank must be an integer, got {item.get('rank')!r}")
        parsed[image_id] = (score, rank)

    missing = sorted(set(image_map) - set(parsed))
    if missing:
        raise InvalidResponse(f"missing image_id: {', '.join(missing)}")
    ranks = sorted(rank for _, rank in parsed.values())
    if ranks != list(range(1, n + 1)):
        raise InvalidResponse(f"ranks must use each of 1..{n} once, got {ranks}")
    return {image_map[i]: {"score": s, "rank": r} for i, (s, r) in parsed.items()}


def call_judge(client: Any, args: argparse.Namespace, prompt: Prompt, content: list[dict[str, Any]]) -> tuple[str, Any]:
    request: dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "system": prompt.system,
        "messages": [{"role": "user", "content": content}],
    }
    if args.temperature is not None:
        request["temperature"] = args.temperature
    response = client.messages.create(**request)
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    return text, response


def judge_scene(
    client: Any,
    anthropic: Any,
    args: argparse.Namespace,
    prompt: Prompt,
    scenes_root: Path,
    recon_root: Path,
    room: str,
    scene_id: str,
) -> dict[str, Any]:
    seed = scene_shuffle_seed(args.shuffle_seed, scene_id)
    order = candidate_order(seed)
    image_map = {f"image_{i}": model for i, model in enumerate(order, 1)}
    record: dict[str, Any] = {
        "scene_id": scene_id,
        "room": room,
        "judge_model": args.model,
        "shuffle_seed": seed,
        "image_map": image_map,
        "status": "failed",
        "result": None,
        "error": None,
        "attempts": [],
    }

    recon_dir = recon_root / room / scene_id
    try:
        content = build_content(
            prompt.user_template,
            scenes_root / room / scene_id / GT_IMAGE_NAME,
            [recon_dir / f"{m}.png" for m in order],
        )
    except (RequestTooLarge, OSError) as e:
        record["error"] = str(e)
        return record

    for attempt in range(1, args.max_attempts + 1):
        entry: dict[str, Any] = {
            "attempt": attempt,
            "raw_response": None,
            "stop_reason": None,
            "input_tokens": None,
            "output_tokens": None,
            "error": None,
        }
        record["attempts"].append(entry)
        try:
            text, response = call_judge(client, args, prompt, content)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError):
            raise
        except anthropic.BadRequestError as e:
            # The same request would be rejected again.
            entry["error"] = record["error"] = f"bad request: {e}"
            break
        except anthropic.APIError as e:
            entry["error"] = record["error"] = f"{type(e).__name__}: {e}"
            if attempt < args.max_attempts:
                time.sleep(15 * attempt)
            continue

        entry["raw_response"] = text
        entry["stop_reason"] = response.stop_reason
        entry["input_tokens"] = response.usage.input_tokens
        entry["output_tokens"] = response.usage.output_tokens
        try:
            record["result"] = parse_judgement(text, image_map)
        except InvalidResponse as e:
            entry["error"] = record["error"] = str(e)
            continue
        record["status"] = "ok"
        record["error"] = None
        break
    return record


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def write_scores_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = ["scene_id"]
    for model in JUDGED_MODELS:
        fields += [f"{model}_score", f"{model}_rank"]
    fields += ["status", "shuffle_seed"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            row: dict[str, Any] = {
                "scene_id": rec["scene_id"],
                "status": rec["status"],
                "shuffle_seed": rec["shuffle_seed"],
            }
            result = rec.get("result") or {}
            for model in JUDGED_MODELS:
                row[f"{model}_score"] = result.get(model, {}).get("score", "")
                row[f"{model}_rank"] = result.get(model, {}).get("rank", "")
            writer.writerow(row)


def summarize(records: list[dict[str, Any]]) -> tuple[int, dict[str, dict[str, float]]]:
    ok = [r for r in records if r["status"] == "ok"]
    n = len(ok)
    stats: dict[str, dict[str, float]] = {}
    for model in JUDGED_MODELS:
        scores = [r["result"][model]["score"] for r in ok]
        ranks = [r["result"][model]["rank"] for r in ok]
        stats[model] = {
            "mean_score": sum(scores) / n,
            "mean_rank": sum(ranks) / n,
            "top1": 100.0 * sum(1 for x in ranks if x == 1) / n,
        }
    return n, stats


def write_table(path: Path, n_scenes: int, stats: dict[str, dict[str, float]]) -> None:
    order = sorted(JUDGED_MODELS, key=lambda m: (-stats[m]["mean_score"], stats[m]["mean_rank"]))
    cells = {
        m: [f"{stats[m]['mean_score']:.2f}", f"{stats[m]['mean_rank']:.2f}", f"{stats[m]['top1']:.1f}"]
        for m in order
    }
    # Best value per column, compared at the printed precision so ties are bolded together.
    best = [
        max(float(cells[m][0]) for m in order),
        min(float(cells[m][1]) for m in order),
        max(float(cells[m][2]) for m in order),
    ]

    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\footnotesize",
        r"\vspace{.07in}",
        "",
        r"\resizebox{\linewidth}{!}{%",
        r"\begin{tabular}{lccc}",
        r"\toprule",
        r"Model & Mean Perceptual $\uparrow$ & Mean Rank $\downarrow$ & Top-1 (\%)\\",
        r"\midrule",
    ]
    for m in order:
        row = [rf"\textbf{{{c}}}" if float(c) == b else c for c, b in zip(cells[m], best)]
        lines.append(f"{JUDGED_MODELS[m]} & " + " & ".join(row) + r" \\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}%",
        "}",
        r"\vspace{-.02in}",
        r"\caption{Perceptual evaluation of six models on "
        + str(n_scenes)
        + r" scenes: mean judge score (1--5), mean rank among the six models, and share of scenes ranked first.}",
        r"\label{tab:perception_overall}",
        r"\vspace{-.12in}",
        r"\end{table}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(run_dir: Path, ideal_root: Path, scenes: list[tuple[str, str]], scenes_dir: Path) -> None:
    records = []
    for _, scene_id in scenes:
        path = scenes_dir / f"{scene_id}.json"
        if path.is_file():
            records.append(load_json(path))

    csv_path = run_dir / "judge_scores.csv"
    write_scores_csv(csv_path, records)
    n_failed = sum(1 for r in records if r["status"] != "ok")
    n_missing = len(scenes) - len(records)
    log.info("Wrote %s (%d scenes, %d failed, %d not run yet)",
             rel(ideal_root, csv_path), len(records), n_failed, n_missing)

    in_tok = sum(a.get("input_tokens") or 0 for r in records for a in r.get("attempts", []))
    out_tok = sum(a.get("output_tokens") or 0 for r in records for a in r.get("attempts", []))
    cost = in_tok / 1e6 * PRICE_INPUT_PER_MTOK + out_tok / 1e6 * PRICE_OUTPUT_PER_MTOK
    log.info("Tokens over all attempts: %d input, %d output (about $%.2f at list price)",
             in_tok, out_tok, cost)

    n_ok, stats = (0, {}) if not any(r["status"] == "ok" for r in records) else summarize(records)
    if n_ok == 0:
        log.info("No successful scenes yet, table not written.")
        return
    tex_path = run_dir / "judge_table.tex"
    write_table(tex_path, n_ok, stats)
    log.info("Wrote %s (%d scenes)", rel(ideal_root, tex_path), n_ok)
    for m in sorted(stats, key=lambda k: -stats[k]["mean_score"]):
        s = stats[m]
        log.info("  %-24s score %.2f  rank %.2f  top-1 %.1f%%",
                 JUDGED_MODELS[m], s["mean_score"], s["mean_rank"], s["top1"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    ideal_root = Path(args.ideal_root).resolve() if args.ideal_root else Path(__file__).resolve().parents[2]
    scenes_root = resolve(ideal_root, args.scenes_root)
    recon_root = resolve(ideal_root, args.recon_root)
    run_dir = resolve(ideal_root, args.output_root) / args.run_name
    scenes_dir = run_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir / "run.log")

    prompt = load_prompt(resolve(ideal_root, args.prompt_file))
    check_run_config(run_dir, {
        "judge_model": args.model,
        "sample_seed": args.sample_seed,
        "shuffle_seed": args.shuffle_seed,
        "scenes_per_room": args.scenes_per_room,
        "models": list(JUDGED_MODELS),
        "prompt_file": rel(ideal_root, resolve(ideal_root, args.prompt_file)),
        "prompt_sha256": prompt.sha256,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "scenes_root": rel(ideal_root, scenes_root),
        "recon_root": rel(ideal_root, recon_root),
    })

    sampled_path = run_dir / "sampled_scenes.txt"
    if sampled_path.is_file():
        scenes = read_sampled(sampled_path)
    else:
        scenes = sample_scenes(scenes_root, recon_root, args.scenes_per_room, args.sample_seed)
        sampled_path.write_text("".join(f"{room}/{sid}\n" for room, sid in scenes), encoding="utf-8")
        log.info("Sampled %d scenes -> %s", len(scenes), rel(ideal_root, sampled_path))

    if args.summarize_only:
        write_outputs(run_dir, ideal_root, scenes, scenes_dir)
        return 0

    import anthropic

    client = anthropic.Anthropic(api_key=load_api_key(ideal_root, args.api_key_file), max_retries=4)
    todo = scenes[: args.limit] if args.limit > 0 else scenes
    log.info("Judge %s, %d scenes, run dir %s", args.model, len(todo), rel(ideal_root, run_dir))

    for i, (room, scene_id) in enumerate(todo, 1):
        out_path = scenes_dir / f"{scene_id}.json"
        if out_path.is_file() and load_json(out_path).get("status") == "ok":
            log.info("[%d/%d] %s  already done", i, len(todo), scene_id)
            continue
        try:
            record = judge_scene(client, anthropic, args, prompt, scenes_root, recon_root, room, scene_id)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            log.info("Stopping: %s: %s", type(e).__name__, e)
            return 1
        write_json(out_path, record)
        tokens = sum(a.get("input_tokens") or 0 for a in record["attempts"])
        if record["status"] == "ok":
            log.info("[%d/%d] %s  ok  (%d attempt(s), %d input tokens)",
                     i, len(todo), scene_id, len(record["attempts"]), tokens)
        else:
            log.info("[%d/%d] %s  FAILED  %s", i, len(todo), scene_id, record["error"])

    write_outputs(run_dir, ideal_root, scenes, scenes_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
