"""
Поиск возможных дублей рецептов в базе по похожести названий.

Использует ту же логику сравнения, что и защита от дублей при ИИ-генерации
(backend/database/crud.names_are_similar), поэтому находит те же случаи:
"Грибной крем-суп" / "Грибной суп-пюре", "Рыба Фугу" / "Рыба Фугу в соевом
соусе", "Фокачча" / "Фокачча (итальянский плоский хлеб)" и т.п.

Ничего не удаляет и не меняет в базе — только показывает найденные группы
и для каждой печатает готовую команду scripts/merge_recipes.py, которой
можно объединить дубли (или изменить id в команде, если предложенный выбор
не устраивает).

Запуск (из корня проекта, с активированным venv):
    python -m scripts.find_duplicates
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import crud
from backend.database.db import async_session
from backend.database.models import Recipe


async def main() -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Recipe)
            .where(Recipe.is_active.is_(True))
            .options(
                selectinload(Recipe.category),
                selectinload(Recipe.ingredient_links),
                selectinload(Recipe.steps),
            )
            .order_by(Recipe.created_at)
        )
        recipes = list(result.scalars().all())

    visited: set[int] = set()
    groups: list[list[Recipe]] = []
    for i, r in enumerate(recipes):
        if r.id in visited:
            continue
        group = [r]
        visited.add(r.id)
        for other in recipes[i + 1:]:
            if other.id in visited:
                continue
            if crud.names_are_similar(r.name, other.name):
                group.append(other)
                visited.add(other.id)
        if len(group) > 1:
            groups.append(group)

    if not groups:
        print("Похожих по названию рецептов не найдено.")
        return

    print(f"Найдено групп возможных дублей: {len(groups)}\n")
    for n, group in enumerate(groups, start=1):
        print(f"--- Группа {n} ---")

        # Из группы предлагаем оставить рецепт с фото, максимальным числом
        # шагов+ингредиентов (более подробный рецепт) и не-ИИ-генерацию,
        # если такая есть (обычно это оригинальный рецепт из seed-базы).
        scored = []
        for r in group:
            score = (
                bool(r.photo_path),
                len(r.steps) + len(r.ingredient_links),
                not r.is_ai_generated,
            )
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]

        for score, r in scored:
            mark = "  <- предлагаю оставить" if r.id == best.id else ""
            print(
                f"  [{r.id}] {r.name!r}  "
                f"категория={r.category.name if r.category else '?'}  "
                f"шагов={len(r.steps)}  ингредиентов={len(r.ingredient_links)}  "
                f"фото={'да' if r.photo_path else 'нет'}  "
                f"ии={'да' if r.is_ai_generated else 'нет'}  "
                f"создан={r.created_at.date()}{mark}"
            )

        remove_ids = " ".join(str(r.id) for r in group if r.id != best.id)
        print(f"  Команда для объединения (оставить {best.id}):")
        print(f"    python -m scripts.merge_recipes --keep {best.id} --remove {remove_ids}\n")


if __name__ == "__main__":
    asyncio.run(main())
