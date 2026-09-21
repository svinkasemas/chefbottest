"""
Сезонные подборки для главного экрана - бэкенд сам решает, какую подборку
показать, по текущему месяцу (см. GET /api/home/seasonal в backend/main.py).
Никаких настроек пользователя не нужно - подборка одинакова для всех и
меняется по календарю.
"""
from __future__ import annotations

from datetime import datetime

# month (1..12) -> ключ сезона
_SEASON_BY_MONTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}

SEASONS: dict[str, dict[str, object]] = {
    "autumn": {
        "title": "🍂 Осенние блюда",
        "keywords": ["тыкв", "глинтвейн", "гриб", "капуст", "яблок", "айва", "хаггис"],
    },
    "winter": {
        "title": "❄️ Согревающее к зиме",
        "keywords": ["суп", "имбир", "мандарин", "запекан", "какао", "жарк", "холодец"],
    },
    "spring": {
        "title": "🌱 Весенняя лёгкость",
        "keywords": ["редис", "зелень", "спарж", "щавел", "укроп", "шпинат", "авокадо"],
    },
    "summer": {
        "title": "☀️ Летние холодные закуски",
        "keywords": ["окрошк", "салат", "арбуз", "гаспачо", "гриль", "холодн", "мороженое"],
    },
}


def current_season_key(now: datetime | None = None) -> str:
    month = (now or datetime.utcnow()).month
    return _SEASON_BY_MONTH[month]


def current_season() -> dict[str, object]:
    return SEASONS[current_season_key()]
