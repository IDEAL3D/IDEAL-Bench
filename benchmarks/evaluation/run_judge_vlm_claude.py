#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def extract_section(text: str, header: str) -> str:
    # Extract markdown section content after `## <header>` until next `## `.
    pat = re.compile(rf"^##\s+{re.escape(header)}\s*$", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return ""
    start = m.end()
    next_h = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + next_h.start() if next_h else len(text)
    return text[start:end].strip()


def encode_image_b64(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return data, mime


def extract_text_from_response(resp: Any) -> str:
    parts: list[str] = []
    for blk in getattr(resp, "content", []) or []:
        if getattr(blk, "type", None) == "text":
            parts.append(getattr(blk, "text", ""))
    return "\n".join(parts).strip()


def extract_first_json_array(text: str) -> str | None:
    t = text.strip()
    if t.startswith("```"):
        blocks = t.split("```")
        if len(blocks) >= 2:
            t = blocks[1].strip()
            if t.startswith("json"):
                t = t[4:].strip()

    start = t.find("[")
    if start < 0:
        return None

    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(t[start:], start=start):
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
        if ch == "[":
            depth += 1
            continue
        if ch == "]":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run judge-VLM multi-model evaluation with Claude")
    p.add_argument("--input-root", default="result/judge_vlm/input")
    p.add_argument("--prompt-file", default="inference/prompts/judge_vlm_perceptual_prompt_multi_model.md")
    p.add_argument("--output-root", default="result/judge_vlm/output")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--api-key", default=None)
    p.add_argument("--api-key-file", default="api/secrets/anthropic_judge_api_key.txt")
    p.add_argument("--limit", type=int, default=0)
    return p.parse_args()


def load_api_key(args: argparse.Namespace, root: Path) -> str:
    k = (args.api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if k:
        return k
    f = (root / args.api_key_file).resolve()
    if f.is_file():
        return f.read_text(encoding="utf-8").strip()
    raise RuntimeError("Missing Anthropic API key (--api-key / ANTHROPIC_API_KEY / --api-key-file)")


def validate_scores_one_to_one(scores: list[Any], candidate_names: list[str]) -> list[dict[str, Any]]:
    if not isinstance(scores, list):
        raise RuntimeError("scores_not_list")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected = set(candidate_names)

    for item in scores:
        if not isinstance(item, dict):
            raise RuntimeError("score_item_not_object")
        name = item.get("model_name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("missing_model_name")
        if name not in expected:
            raise RuntimeError(f"unexpected_model_name:{name}")
        if name in seen:
            raise RuntimeError(f"duplicate_model_name:{name}")
        seen.add(name)
        normalized.append(item)

    missing = [n for n in candidate_names if n not in seen]
    if missing:
        raise RuntimeError(f"missing_model_names:{','.join(missing)}")

    by_name = {x["model_name"]: x for x in normalized}
    return [by_name[n] for n in candidate_names]


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]

    input_root = (root / args.input_root).resolve()
    prompt_file = (root / args.prompt_file).resolve()
    output_root = (root / args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    run_tag = time.strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"run_{run_tag}"
    run_dir.mkdir(parents=True, exist_ok=True)
    scene_dir = run_dir / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "judge_run.log"

    def log(msg: str) -> None:
        line = f"[{ts()}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")

    key = load_api_key(args, root)
    client = Anthropic(api_key=key)

    if not input_root.is_dir():
        raise RuntimeError(f"input_root not found: {input_root}")
    if not prompt_file.is_file():
        raise RuntimeError(f"prompt_file not found: {prompt_file}")

    prompt_md = prompt_file.read_text(encoding="utf-8")
    system_prompt = extract_section(prompt_md, "System Prompt") or prompt_md
    user_template = extract_section(prompt_md, "User Prompt Template")
    if not user_template:
        user_template = (
            "Compare one GT image with multiple reconstruction images from different models. "
            "Return JSON array only, following required schema."
        )

    scenes = sorted([p for p in input_root.iterdir() if p.is_dir() and not p.name.startswith(".")])
    if args.limit and args.limit > 0:
        scenes = scenes[: args.limit]

    log(f"START model={args.model}")
    log(f"INPUT_ROOT={input_root}")
    log(f"SCENES={len(scenes)}")

    rows: list[dict[str, Any]] = []

    for idx, scene in enumerate(scenes, 1):
        gt = scene / "GT.png"
        cands = sorted([p for p in scene.glob("*.png") if p.name != "GT.png"])

        if not gt.is_file() or not cands:
            log(f"SKIP scene={scene.name} reason=missing_gt_or_candidates")
            rows.append({
                "scene_id": scene.name,
                "model_name": "__scene__",
                "status": "skip",
                "error": "missing_gt_or_candidates",
            })
            continue

        cand_names = [p.stem for p in cands]
        log(f"RUN {idx}/{len(scenes)} scene={scene.name} candidates={len(cands)}")

        content: list[dict[str, Any]] = []
        content.append({"type": "text", "text": "Reference image (GT):"})
        gt_b64, gt_mime = encode_image_b64(gt)
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": gt_mime, "data": gt_b64},
            }
        )

        listing_lines = [
            user_template.strip(),
            "",
            "Candidate reconstructions (model_name order):",
        ]
        for i, n in enumerate(cand_names, 1):
            listing_lines.append(f"{i}. {n}")
        listing_lines.append("")
        listing_lines.append("Use model_name exactly as listed above.")
        listing_lines.append("Return exactly one JSON array item per candidate.")

        content.append({"type": "text", "text": "\n".join(listing_lines)})

        for i, p in enumerate(cands, 1):
            b64, mime = encode_image_b64(p)
            content.append({"type": "text", "text": f"Candidate {i}: model_name={p.stem}"})
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                }
            )

        scene_result: dict[str, Any] = {
            "scene_id": scene.name,
            "judge_model": args.model,
            "candidates": cand_names,
            "status": "ok",
        }

        try:
            req_kwargs: dict[str, Any] = {
                "model": args.model,
                "max_tokens": args.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            }
            # claude-opus-4-7 no longer accepts temperature.
            if args.model != "claude-opus-4-7":
                req_kwargs["temperature"] = args.temperature
            resp = client.messages.create(**req_kwargs)
            raw = extract_text_from_response(resp)
            arr_text = extract_first_json_array(raw)
            if not arr_text:
                raise RuntimeError("no_json_array_found")
            parsed = json.loads(arr_text)
            if not isinstance(parsed, list):
                raise RuntimeError("json_not_array")
            scene_result["scores"] = validate_scores_one_to_one(parsed, cand_names)
        except Exception as e:
            scene_result["status"] = "error"
            scene_result["error"] = str(e)
            try:
                scene_result["raw_response"] = raw  # type: ignore[name-defined]
            except Exception:
                pass

        out_scene = scene_dir / f"{scene.name}.json"
        out_scene.write_text(json.dumps(scene_result, ensure_ascii=False, indent=2), encoding="utf-8")

        if scene_result.get("status") == "ok":
            for item in scene_result.get("scores", []):
                rows.append(
                    {
                        "scene_id": scene.name,
                        "model_name": item.get("model_name"),
                        "overall_spatial_impression_similarity": item.get("overall_spatial_impression_similarity"),
                        "spatial_layering_and_depth": item.get("spatial_layering_and_depth"),
                        "perceptual_score": item.get("perceptual_score"),
                        "rank": item.get("rank"),
                        "status": "ok",
                        "error": "",
                    }
                )
        else:
            rows.append(
                {
                    "scene_id": scene.name,
                    "model_name": "__scene__",
                    "overall_spatial_impression_similarity": "",
                    "spatial_layering_and_depth": "",
                    "perceptual_score": "",
                    "rank": "",
                    "status": "error",
                    "error": scene_result.get("error", "unknown"),
                }
            )

    csv_path = run_dir / "judge_scores.csv"
    fields = [
        "scene_id",
        "model_name",
        "overall_spatial_impression_similarity",
        "spatial_layering_and_depth",
        "perceptual_score",
        "rank",
        "status",
        "error",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary = {
        "run_dir": str(run_dir),
        "scene_count": len(scenes),
        "row_count": len(rows),
        "csv": str(csv_path),
        "log": str(log_path),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE run_dir={run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())