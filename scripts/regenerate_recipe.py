"""
Пересоздаёт содержимое одного рецепта через ИИ, если scripts/audit_recipes.py
пометил его как подозрительный (название спутано с другим похожим блюдом).

Сохраняет id рецепта (а с ним - избранное пользователей), но полностью
заменяет кухню, описание, ингредиенты, шаги и photo_prompt на то, что вернёт
ИИ по актуальному, исправленному промпту. Старое фото удаляется (photo_path
сбрасывается в NULL) - следующий запуск scripts/generate_recipe_images
сгенерирует новое, соответствующее исправленному описанию.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.regenerate_recipe --id 76
    python -m scripts.regenerate_recipe --id 76 --name "Шаньга"   # если нужно
                                                                   # сгенерировать
                                                                   # под другим
                                                                   # названием
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from backend.ai_recipe import RecipeGenerationError, generate_recipe_dict
from backend.database import crud
from backend.database.db import async_session
from backend.database.models import Recipe, RecipeIngredient, RecipeStep

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "webapp" / "photos"


async def regenerate(recipe_id: int, dish_name: str | None) -> None:
    async with async_session() as session:
        recipe = (
            await session.execute(select(Recipe).where(Recipe.id == recipe_id))
        ).scalar_one_or_none()
        if recipe is None:
            print(f"Рецепт с id={recipe_id} не найден.")
            return

        name_to_generate = dish_name or recipe.name
        print(f"Генерирую заново «{name_to_generate}» (id={recipe_id})...")
        try:
            data = await asyncio.to_thread(generate_recipe_dict, name_to_generate)
        except RecipeGenerationError as e:
            print(f"Не удалось сгенерировать: {e}")
            return

        category_name = crud.CATEGORY_KEY_TO_NAME.get(data.get("category"), recipe.category_id)
        if isinstance(category_name, str):
            category = await crud.get_or_create_category(session, category_name)
            recipe.category_id = category.id

        # Удаляем старое фото - оно построено по старому, неверному photo_prompt.
        if recipe.photo_path:
            old_photo = PHOTOS_DIR / recipe.photo_path
            if old_photo.exists():
                old_photo.unlink()
            recipe.photo_path = None

        recipe.name = data.get("name", name_to_generate)
        recipe.photo_prompt = data.get("photo_prompt")
        recipe.time_minutes = data.get("time_minutes", recipe.time_minutes)
        recipe.difficulty = data.get("difficulty", recipe.difficulty)
        recipe.calories = data.get("calories", recipe.calories)
        recipe.price_level = data.get("price_level", recipe.price_level)
        recipe.cuisine = data.get("cuisine", recipe.cuisine)
        recipe.base_portions = data.get("base_portions", recipe.base_portions)
        recipe.description = data.get("description", recipe.description)
        recipe.is_ai_generated = True

        # Полностью заменяем ингредиенты и шаги на новые.
        old_links = (
            await session.execute(
                select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
            )
        ).scalars().all()
        for link in old_links:
            await session.delete(link)

        old_steps = (
            await session.execute(select(RecipeStep).where(RecipeStep.recipe_id == recipe.id))
        ).scalars().all()
        for step in old_steps:
            await session.delete(step)

        await session.flush()

        for ing in data.get("ingredients", []):
            ingredient = await crud.get_or_create_ingredient(session, ing["name"])
            session.add(RecipeIngredient(
                recipe_id=recipe.id, ingredient_id=ingredient.id,
                amount=ing.get("amount", 0), unit=ing.get("unit", ""),
            ))

        for i, step in enumerate(data.get("steps", []), start=1):
            session.add(RecipeStep(
                recipe_id=recipe.id, step_number=i,
                text=step["text"], timer_minutes=crud.normalize_timer_minutes(step.get("timer_minutes")),
            ))

        await session.commit()
        print(f"Готово: [{recipe.id}] {recipe.name!r}, кухня={recipe.cuisine!r}.")
        print("Не забудьте запустить python -m scripts.generate_recipe_images для нового фото.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="id рецепта в базе")
    parser.add_argument(
        "--name", type=str, default=None,
        help="Название блюда для генерации, если отличается от текущего в базе",
    )
    args = parser.parse_args()
    asyncio.run(regenerate(args.id, args.name))


if __name__ == "__main__":
    main()
