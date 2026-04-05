#!/usr/bin/env python3
"""
``categories_in_view`` / assets_list → 给多模态模型的可读提示（system / user 侧补文本）。

**定位**：占位主干，映射表与拼接策略后续再细化。``infer.py`` 里仍用 canonical category；
真正请求 API 前可改为调用本模块的展开结果。

**建议用法（以后）**：
  - **user**：``assets_list`` 仍用 canonical（便于输出 JSON 与 GT 对齐），或改为「canonical + 短注」二选一；
  - **system**：附加一段 ``build_asset_hint_appendix`` 生成的释义表，省 token 又避免歧义。

运行示例::

    cd IDEAL/inference
    python inview2sp.py --scene-id kitchen_s511530340
    python inview2sp.py --scene-id kitchen_s511530340 --print-user-skeleton
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Iterable

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from configs.constant import canonicalize_asset_category  # noqa: E402

from infer import (  # noqa: E402
    ALLOWED_ROOM_TYPES,
    DEFAULT_SCENES_ROOT,
    build_user_prompt,
    load_categories_in_view,
)

# ── canonical ``GT / objects_in_view`` category → 模型友好英文描述（占位，按需补全） ──
# 键必须与 canonicalize_asset_category 之后、出现在 categories_in_view 里的字符串一致。
CATEGORY_TO_MODEL_HINT: dict[str, str] = {
    "kitchen_space": (
        "integrated kitchen cabinetry (upper wall cabinets + base cabinets + countertop; "
        "Infinigen/KitchenSpaceFactory single asset — predict as one object or merged boxes, not unrelated cabinets)"
    ),
    # TODO: library_block, large_shelf vs cabinet, open-office vs office, …
}


def normalize_category_key(raw: str) -> str:
    """与 ``infer.load_categories_in_view`` 一致：先去库别名再等义映射。"""
    return canonicalize_asset_category(str(raw).strip())


def expand_category_for_model(canonical: str) -> str:
    """
    单个 canonical → 模型侧字符串。

    未命中映射时：原样返回（或将来改为「canonical (no glossary entry)」）。
    """
    c = normalize_category_key(canonical)
    hint = CATEGORY_TO_MODEL_HINT.get(c)
    if hint:
        return f"{c}: {hint}"
    return c


def expand_categories_for_model(categories: Iterable[str]) -> list[str]:
    """去重顺序与 ``load_categories_in_view`` 类似：按输入顺序保留首次出现。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in categories:
        c = normalize_category_key(str(raw))
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(expand_category_for_model(c))
    return out


def build_asset_hint_appendix(categories: Iterable[str]) -> str:
    """
    适合贴在 **system prompt 末尾** 的释义块（仅占位的 markdown 风格）。

    无映射项时可返回空串或简短说明；后续可改为从 YAML 加载整表。
    """
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


def build_user_prompt_with_model_hints(assets_canonical: list[str]) -> str:
    """
    **占位**：user 侧直接把展开串写进 assets_list。

    若不想污染 canonical，可改为此函数仅调 ``build_user_prompt(assets_canonical)``，
    把 ``build_asset_hint_appendix`` 交给调用方拼进 system。
    """
    expanded = expand_categories_for_model(assets_canonical)
    return build_user_prompt(expanded)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview in-view categories → model hint expansion (stub)."
    )
    parser.add_argument(
        "--scenes-root",
        default=DEFAULT_SCENES_ROOT,
        help="场景根目录（与 infer 默认一致）",
    )
    parser.add_argument("--scene-id", required=True, metavar="ID", help="子目录名 = scene_id")
    parser.add_argument(
        "--print-user-skeleton",
        action="store_true",
        help="打印用展开串调用 build_user_prompt 后的整段 user 草稿",
    )
    parser.add_argument(
        "--print-system-appendix",
        action="store_true",
        help="打印建议附在 system 后的 glossary 块",
    )
    args = parser.parse_args()

    cats = load_categories_in_view(args.scenes_root, args.scene_id)
    if cats is None:
        print("[inview2sp] 无 objects_in_view.json / info.json 或非列表 categories_in_view", file=sys.stderr)
        sys.exit(2)
    if not cats:
        print("[inview2sp] categories_in_view 为空")
        sys.exit(0)

    print("[in canonical order]", cats)
    print("[expanded for model]", expand_categories_for_model(cats))
    if args.print_user_skeleton:
        print("\n--- user prompt skeleton ---\n")
        print(build_user_prompt_with_model_hints(cats))
    if args.print_system_appendix:
        print("\n--- system appendix ---\n")
        print(build_asset_hint_appendix(cats))
        print(f"\n(allowed_room_types unchanged: {', '.join(ALLOWED_ROOM_TYPES)})")


if __name__ == "__main__":
    main()
