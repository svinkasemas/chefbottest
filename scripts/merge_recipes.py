"""
Объединяет дубли рецептов, найденные scripts/find_duplicates.py.

Оставляет один рецепт (--keep), а остальные (--remove) удаляет из базы.
Перед удалением переносит на оставшийся рецепт избранное пользователей
(чтобы не потерять их "лайки") и удаляет файлы фото удаляемых рецептов,
если они не совпадают с фото оставляемого.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.merge_recipes --keep 22 --remove 45 67

Сначала запустите scripts.find_duplicates, чтобы получить id рецептов и
готовую команду — при желании id можно поменять местами, если предложенный
скриптом выбор "оставить" не устраивает.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from backend.database.db import async_session
from backend.database.models import Favorite, Recipe

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "webapp" / "photos"


async def merge(keep_id: int, remove_ids: list[int]) -> None:
    if keep_id in remove_ids:
        print("id в --keep не должен повторяться в --remove.")
        return

    async with async_session() as session:
        keep = (
            await session.execute(select(Recipe).where(Recipe.id == keep_id))
        ).scalar_one_or_none()
        if keep is None:
            print(f"Рецепт с id={keep_id} (--keep) не найден, ничего не делаю.")
            return

        for rid in remove_ids:
            recipe = (
                await session.execute(select(Recipe).where(Recipe.id == rid))
            ).scalar_one_or_none()
            if recipe is None:
                print(f"Рецепт с id={rid} не найден, пропускаю.")
                continue

            # Переносим избранное на оставшийся рецепт, чтобы не терять
            # "лайки" пользователей. Если у пользователя уже есть в
            # избранном оставляемый рецепт — просто убираем дубль записи.
            favorites = (
                await session.execute(select(Favorite).where(Favorite.recipe_id == rid))
            ).scalars().all()
            for fav in favorites:
                existing = await session.execute(
                    select(Favorite).where(
                        Favorite.user_id == fav.user_id, Favorite.recipe_id == keep_id
                    )
                )
                if existing.scalar_one_or_none() is None:
                    fav.recipe_id = keep_id
                else:
                    await session.delete(fav)

            # Удаляем файл фото удаляемого рецепта, если он отличается от
            # фото оставляемого (иначе рискуем стереть общую картинку).
            if recipe.photo_path and recipe.photo_path != keep.photo_path:
                photo_file = PHOTOS_DIR / recipe.photo_path
                if photo_file.exists():
                    photo_file.unlink()

            print(f"Удаляю [{rid}] {recipe.name!r} -> оставлен [{keep_id}] {keep.name!r}")
            await session.delete(recipe)

        await session.commit()

    print("Готово.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, required=True, help="id рецепта, который остаётся")
    parser.add_argument(
        "--remove", type=int, nargs="+", required=True, help="id рецептов, которые нужно удалить"
    )
    args = parser.parse_args()
    asyncio.run(merge(args.keep, args.remove))


if __name__ == "__main__":
    main()
