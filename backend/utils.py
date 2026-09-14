from __future__ import annotations


def scale_amount(base_amount: float, base_portions: int, target_portions: int) -> float:
    if base_portions <= 0:
        return base_amount
    return round(base_amount * target_portions / base_portions, 2)


CATEGORY_EMOJI_FALLBACK = "🍽"
