"""
Скрипт наполнения базы данных рецептами из data/seed_recipes.json.

Запуск (из корня проекта RecipeApp/):
    python -m scripts.seed_db [--file data/другой_файл.json]

Идемпотентен: рецепты с уже существующим названием пропускаются, поэтому
скрипт можно запускать многократно по мере добавления новых файлов рецептов,
пока база не дорастёт до нужных 2000-2500 штук.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from backend.database import crud
from backend.database.db import async_session, init_db
from backend.database.models import Recipe, RecipeIngredient, RecipeStep


async def seed(file_path: Path) -> None:
    await init_db()
    data = json.loads(file_path.read_text(encoding="utf-8"))

    async with async_session() as session:
        cat_key_to_id: dict[str, int] = {}
        for cat in data["categories"]:
            obj = await crud.get_or_create_category(
                session, cat["name"], cat["emoji"], cat.get("color", "#B5462F"), cat.get("sort_order", 0)
            )
            cat_key_to_id[cat["key"]] = obj.id
        await session.commit()

        added, skipped = 0, 0
        for r in data["recipes"]:
            existing = await session.execute(select(Recipe).where(Recipe.name == r["name"]))
            if existing.scalar_one_or_none() is not None:
                skipped += 1
                continue

            recipe = Recipe(
                category_id=cat_key_to_id[r["category"]],
                name=r["name"],
                photo_path=r.get("photo_path"),
                time_minutes=r.get("time_minutes", 30),
                difficulty=r.get("difficulty", 2),
                calories=round(r.get("calories") or 0),
                price_level=r.get("price_level", "Недорого"),
                cuisine=r.get("cuisine", "Русская"),
                base_portions=r.get("base_portions", 4),
                description=r.get("description", ""),
            )
            session.add(recipe)
            await session.flush()

            for ing in r.get("ingredients", []):
                ingredient = await crud.get_or_create_ingredient(session, ing["name"])
                session.add(
                    RecipeIngredient(
                        recipe_id=recipe.id,
                        ingredient_id=ingredient.id,
                        amount=ing.get("amount", 0),
                        unit=ing.get("unit", ""),
                        is_optional=ing.get("is_optional", False),
                    )
                )

            for i, step in enumerate(r.get("steps", []), start=1):
                session.add(
                    RecipeStep(
                        recipe_id=recipe.id,
                        step_number=i,
                        text=step["text"],
                        timer_minutes=step.get("timer_minutes"),
                    )
                )
            added += 1

        await session.commit()
        print(f"Готово. Добавлено рецептов: {added}. Пропущено (уже были): {skipped}.")


def main():
    parser = argparse.ArgumentParser(description="Импорт рецептов в базу RecipeApp")
    parser.add_argument(
        "--file",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data" / "seed_recipes.json"),
    )
    args = parser.parse_args()
    asyncio.run(seed(Path(args.file)))


if __name__ == "__main__":
    main()
