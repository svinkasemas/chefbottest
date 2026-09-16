"""
Находит рецепты высокой сложности (difficulty 4-5), у которых слишком мало
шагов приготовления - признак того, что рецепт был сгенерирован до того, как
в промпт добавили требование расписывать сложные блюда подробнее (см.
backend/ai_recipe.py). Например, "Говядина «Веллингтон»" с 6-8 шагами вместо
ожидаемых для такого блюда 12-20.

Ничего не меняет в базе - только печатает список и готовые команды
scripts/regenerate_recipe.py для пересоздания с более подробным промптом.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.find_underdetailed_recipes
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database.db import async_session
from backend.database.models import Recipe

# Ориентир из обновлённого промпта: для difficulty 4-5 ожидается 12-20 шагов.
# Всё, что заметно меньше нижней границы, считаем недостаточно подробным.
MIN_STEPS_FOR_DIFFICULTY = {4: 10, 5: 12}


async def main() -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Recipe)
            .where(Recipe.is_active.is_(True), Recipe.difficulty >= 4)
            .options(selectinload(Recipe.steps))
            .order_by(Recipe.difficulty.desc(), Recipe.name)
        )
        recipes = list(result.scalars().all())

    underdetailed = [
        r for r in recipes if len(r.steps) < MIN_STEPS_FOR_DIFFICULTY.get(r.difficulty, 10)
    ]

    if not underdetailed:
        print("Недостаточно подробных сложных рецептов не найдено.")
        return

    print(f"Найдено рецептов с нехваткой шагов: {len(underdetailed)}\n")
    for r in underdetailed:
        expected = MIN_STEPS_FOR_DIFFICULTY.get(r.difficulty, 10)
        print(
            f"[{r.id}] {r.name!r}  сложность={r.difficulty}  "
            f"шагов сейчас={len(r.steps)}  ожидается от={expected}"
        )
        print(f"  Пересоздать: python -m scripts.regenerate_recipe --id {r.id}\n")


if __name__ == "__main__":
    asyncio.run(main())
