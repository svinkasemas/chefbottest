"""
Проставляет source_url для одного рецепта по id - для ручного подтверждения
пар, найденных scripts/find_import_sources.py, или просто когда вы точно
помните, откуда был импортирован конкретный рецепт.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.set_recipe_source --id 121 --url "https://www.povarenok.ru/recipes/show/184523/"
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from backend.database.db import async_session
from backend.database.models import Recipe


async def set_source(recipe_id: int, url: str) -> None:
    async with async_session() as session:
        recipe = (await session.execute(select(Recipe).where(Recipe.id == recipe_id))).scalar_one_or_none()
        if recipe is None:
            print(f"Рецепт с id={recipe_id} не найден.")
            return
        recipe.source_url = url
        await session.commit()
        print(f"Готово: [{recipe.id}] {recipe.name!r} -> {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="id рецепта в базе")
    parser.add_argument("--url", type=str, required=True, help="ссылка на исходную страницу рецепта")
    args = parser.parse_args()
    asyncio.run(set_source(args.id, args.url))


if __name__ == "__main__":
    main()
