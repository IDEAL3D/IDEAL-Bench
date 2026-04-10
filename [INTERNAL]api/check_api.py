"""
python check_api.py --model gemini-3-flash-preview
python check_api.py --model gpt-5.2-2025-12-11
"""

import argparse
import csv
from pathlib import Path

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = Path(__file__).resolve().parent / "secrets" / "api_keys.csv"
_QUIT = frozenset({"exit", "quit"})


def load_config(model: str, path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("model") or "").strip() == model:
                return {
                    "provider": (row.get("provider") or "").strip().lower(),
                    "model": (row.get("model") or "").strip(),
                    "api_key": (row.get("api_key") or "").strip(),
                    "base_url": (row.get("base_url") or "").strip(),
                }
    raise ValueError(f"No row for model {model!r} in {path}")


def _chat_loop(title: str, ask):
    print(f"Connected to {title} (type exit to quit)")
    while True:
        text = input("You: ").strip()
        if text.lower() in _QUIT:
            break
        if not text:
            continue
        try:
            print("Model:", ask(text))
        except Exception as e:
            print("Error:", e)


def chat_gemini(api_key: str, model: str):
    if google_genai is None:
        raise RuntimeError("Install google-genai: pip install google-genai")
    client = google_genai.Client(api_key=api_key)

    def ask(t: str):
        r = client.models.generate_content(model=model, contents=t)
        return r.text if r.text is not None else "(no text)"

    _chat_loop(f"Gemini: {model}", ask)


def chat_openai(api_key: str, model: str, base_url: str = ""):
    if OpenAI is None:
        raise RuntimeError("Install openai: pip install openai")
    kw = {"api_key": api_key}
    if base_url:
        kw["base_url"] = base_url
    client = OpenAI(**kw)

    def ask(t: str):
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": t}],
        )
        return r.choices[0].message.content or "(no text)"

    _chat_loop(f"OpenAI: {model}", ask)


def main():
    p = argparse.ArgumentParser(
        description="Load API keys from CSV and run a simple chat loop (Gemini / OpenAI)."
    )
    p.add_argument("--model", required=True, help="Value in the model column of api_keys.csv")
    p.add_argument("--config", type=Path, default=DEFAULT_CSV, help=f"Default: {DEFAULT_CSV}")
    args = p.parse_args()

    cfg = load_config(args.model, args.config)
    if cfg["provider"] == "gemini":
        chat_gemini(cfg["api_key"], cfg["model"])
    elif cfg["provider"] == "openai":
        chat_openai(cfg["api_key"], cfg["model"], cfg["base_url"])
    else:
        raise ValueError(f"Unsupported provider: {cfg['provider']!r}")


if __name__ == "__main__":
    main()
