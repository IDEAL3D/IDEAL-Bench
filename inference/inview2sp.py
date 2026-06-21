"""Inject ``categories_in_view`` into the system prompt (``This room contains: …``).

Used by ``infer.py`` via ``inject_into_system_prompt``. Optional ``build_asset_hint_appendix``
for extra glossary text (not wired in infer by default).
"""

from __future__ import annotations

import os
import sys
from typing import Iterable

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from configs.constant import canonicalize_asset_category  # noqa: E402

SYSTEM_PROMPT_CATEGORIES_PLACEHOLDER = "{{THIS_ROOM_CONTAINS}}"

# Optional canonical → gloss for ambiguous categories (extend as needed).
CATEGORY_TO_MODEL_HINT: dict[str, str] = {
    "kitchen_space": (
        "integrated kitchen cabinetry (upper wall cabinets + base cabinets + countertop; "
        "Infinigen/KitchenSpaceFactory single asset — predict as one object or merged boxes, not unrelated cabinets)"
    ),
}


def normalize_category_key(raw: str) -> str:
    """Same normalization as ``infer.load_categories_in_view``."""
    return canonicalize_asset_category(str(raw).strip())


def expand_category_for_model(canonical: str) -> str:
    c = normalize_category_key(canonical)
    hint = CATEGORY_TO_MODEL_HINT.get(c)
    if hint:
        return f"{c}: {hint}"
    return c


def expand_categories_for_model(categories: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in categories:
        c = normalize_category_key(str(raw))
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(expand_category_for_model(c))
    return out


def format_categories_for_system_sentence(categories: Iterable[str]) -> str:
    """Comma-separated canonical list for the ``This room contains:`` line (matches user assets_list)."""
    parts = [normalize_category_key(str(x)) for x in categories]
    parts = [p for p in parts if p]
    return ", ".join(parts)


def inject_into_system_prompt(system_template: str, categories: Iterable[str]) -> str:
    """Replace ``{{THIS_ROOM_CONTAINS}}`` in ``prompts/system_prompt_*.md``; append a section if missing."""
    fragment = format_categories_for_system_sentence(categories)
    if SYSTEM_PROMPT_CATEGORIES_PLACEHOLDER in system_template:
        return system_template.replace(SYSTEM_PROMPT_CATEGORIES_PLACEHOLDER, fragment)

    return (
        system_template.rstrip()
        + "\n\n## Scene categories (viewpoint metadata)\n\n"
        + f"This room contains: {fragment}\n"
    )


def build_asset_hint_appendix(categories: Iterable[str]) -> str:
    """Optional markdown block to append to the system prompt (glossary)."""
    lines: list[str] = [
        "## Asset glossary (canonical → meaning)",
        "",
        "Model output JSON must still use **canonical category strings** listed in assets_list where applicable.",
        "Below expands ambiguous names only for interpretation.",
    ]
    expanded = expand_categories_for_model(categories)
    any_hint = False
    for item in expanded:
        if ": " in item:
            lines.append(f"- {item}")
            any_hint = True
    if not any_hint:
        lines.append("- _(no extra hints for this scene; expand CATEGORY_TO_MODEL_HINT)_")
    return "\n".join(lines)


if __name__ == "__main__":
    print("inview2sp is a library module; run infer.py for inference.", file=sys.stderr)
    raise SystemExit(1)
