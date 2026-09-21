"""
Глобальные пищевые ограничения пользователя (аллергии/предпочтения),
настраиваемые один раз в профиле - см. GET/POST /api/settings/dietary
в backend/main.py.

Каждому ключу соответствует список ключевых слов (по-русски, в нижнем
регистре, без учёта окончаний) - если название рецепта или любой его
ингредиент содержит подстроку из списка, рецепт считается "restricted"
для пользователя с этим ограничением (см. recipe_to_short в main.py).
Совпадение эвристическое, а не медицинское - это фильтр для удобства,
а не гарантия безопасности при реальной аллергии.
"""
from __future__ import annotations

DIETARY_OPTIONS: dict[str, dict[str, object]] = {
    "nuts": {
        "label": "Орехи",
        "keywords": ["орех", "арахис", "миндал", "фундук", "кешью", "фисташ", "кедров"],
    },
    "pork": {
        "label": "Свинина",
        "keywords": ["свинин", "бекон", "сало", "ветчин", "бастурм"],
    },
    "seafood": {
        "label": "Рыба и морепродукты",
        "keywords": [
            "рыба", "лосось", "треска", "судак", "сельдь", "форель", "тунец",
            "скумбри", "кальмар", "креветк", "морепродукт", "мидии", "краб", "осьминог",
        ],
    },
    "dairy": {
        "label": "Молочное",
        "keywords": ["молок", "сыр", "сливк", "творог", "сметан", "масло сливочное", "йогурт"],
    },
    "eggs": {
        "label": "Яйца",
        "keywords": ["яйцо", "яичн"],
    },
    "gluten": {
        "label": "Глютен",
        "keywords": ["мука пшеничн", "макарон", "спагетти", "хлеб", "панировочн", "лаваш"],
    },
    "alcohol": {
        "label": "Алкоголь",
        "keywords": ["вино", "пиво", "коньяк", "ликёр", "ликер", "ром ", "водк"],
    },
}


MAX_CUSTOM_ALLERGENS = 10


def restriction_labels_for(
    recipe_text: str, active_keys: list[str], custom_keywords: list[str] | None = None
) -> list[str]:
    """
    recipe_text - объединённый в один нижний регистр текст названия рецепта
    и всех его ингредиентов. Возвращает названия ограничений (для показа в
    предупреждении), которым соответствует этот рецепт: из числа активных
    у пользователя ключей (готовый список DIETARY_OPTIONS) и/или его
    собственных, вписанных вручную продуктов (custom_keywords, уже в нижнем
    регистре - см. User.custom_allergens).
    """
    labels = []
    for key in active_keys or []:
        option = DIETARY_OPTIONS.get(key)
        if not option:
            continue
        if any(kw in recipe_text for kw in option["keywords"]):
            labels.append(option["label"])
    for kw in custom_keywords or []:
        if kw and kw in recipe_text:
            labels.append(kw.capitalize())
    return labels
