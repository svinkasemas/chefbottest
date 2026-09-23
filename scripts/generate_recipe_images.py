"""
Ежедневный подбор ФОТОГРАФИЙ для рецептов без фото.

Как это работает (см. подробный docstring в backend/photo_search.py):
1. Ищутся рецепты без фото (Recipe.photo_path IS NULL) - обычный ежедневный
   режим - или ВСЕ рецепты (кроме тех, что помечены как реальное фото со
   страницы-источника), если запущено с --replace-all - для разовой замены
   уже существующих картинок.
2. Для каждого сначала ищется НАСТОЯЩАЯ фотография блюда в Pexels (если
   задан PEXELS_API_KEY) и/или Openverse (без ключа); каждый найденный
   кандидат проверяется через Gemini Vision - действительно ли на нём
   изображено именно это блюдо (иначе поиск по словам иногда подсовывает
   случайное фото - сырники вместо куриных сердечек и т.п.).
3. Если подтверждённого настоящего фото не нашлось - рисуем иллюстрацию
   через ИИ (Pollinations.ai) как запасной вариант, чтобы у рецепта в любом
   случае была подходящая по смыслу картинка.
4. Путь сохранённого файла прописывается в Recipe.photo_path,
   Recipe.photo_source = "web_search" (настоящее, подтверждённое фото) или
   "ai_generated" (нарисовано, поскольку ничего подходящего не нашлось).

Обычный ежедневный запуск (только рецепты без фото):
    python -m scripts.generate_recipe_images

Разовая замена ВСЕХ фото, включая уже существующие/нарисованные ИИ
(рецепты с photo_source="source_page" - настоящее фото с сайта-источника,
добавленное вручную через админскую команду /import в bot.py - не трогаются):
    python -m scripts.generate_recipe_images --replace-all

Пауза между запросами - вежливость к бесплатным API (Pexels/Openverse/
Gemini), чтобы не упереться в рейт-лимит при обработке сразу многих
рецептов. Учтите: с проверкой через Gemini Vision на рецепт теперь уходит
заметно больше запросов (до нескольких кандидатов на источник), чем раньше -
это медленнее, но не подставляет случайные фото.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from backend.database.db import async_session, init_db
from backend.database.models import Recipe
from backend.photo_search import get_dish_photo, save_photo_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_recipe_images")

REQUEST_DELAY_SECONDS = 3


async def _recipes_to_process(replace_all: bool) -> list[Recipe]:
    async with async_session() as session:
        # selectinload(Recipe.category) - категория нужна для поиска фото
        # (см. process_recipe/get_dish_photo: у напитков поиск должен
        # просить фото напитка, а не еды), подгружаем сразу, чтобы обращение
        # к recipe.category ниже не требовало отдельного запроса к уже
        # закрытой сессии.
        query = select(Recipe).where(Recipe.is_active.is_(True)).options(selectinload(Recipe.category))
        if replace_all:
            # Разовая замена: рецепты без фото ИЛИ с фото неизвестного
            # происхождения (старое, ещё не помеченное, либо нарисованное
            # ИИ раньше/сейчас - вдруг теперь найдётся настоящее фото).
            # "source_page" - настоящее фото с сайта-источника, добавленное
            # вручную - никогда не трогаем.
            # is_distinct_from - NULL-безопасное "не равно": обычное != "source_page"
            # в SQL пропускает строки с photo_source IS NULL (NULL != x даёт NULL,
            # не TRUE), а такие старые рецепты как раз и нужно подхватывать.
            query = query.where(
                or_(Recipe.photo_path.is_(None), Recipe.photo_source.is_distinct_from("source_page"))
            )
        else:
            query = query.where(Recipe.photo_path.is_(None))
        result = await session.execute(query)
        return list(result.scalars().all())


async def process_recipe(recipe_id: int, name: str, cuisine: str | None, category: str | None) -> str | None:
    """
    Возвращает "web_search"/"ai_generated" (что удалось сохранить) или None,
    если не получилось вообще ничего.
    """
    image_bytes, source = await asyncio.to_thread(get_dish_photo, name, cuisine, category)
    if image_bytes is None:
        logger.warning("  не удалось получить фото (ни найти, ни нарисовать): «%s»", name)
        return None

    filename = await asyncio.to_thread(save_photo_bytes, recipe_id, image_bytes)

    async with async_session() as session:
        db_recipe = await session.get(Recipe, recipe_id)
        if db_recipe is not None:
            db_recipe.photo_path = filename
            db_recipe.photo_source = source
            await session.commit()

    logger.info("  сохранено (%s): %s", source, filename)
    return source


async def main(replace_all: bool) -> None:
    await init_db()

    recipes = await _recipes_to_process(replace_all)

    if not recipes:
        logger.info("Обрабатывать нечего - у всех активных рецептов уже есть подходящее фото.")
        return

    logger.info("Рецептов для обработки: %d (replace_all=%s)", len(recipes), replace_all)

    found_real = 0
    found_ai = 0
    for i, recipe in enumerate(recipes, start=1):
        logger.info("[%d/%d] %s", i, len(recipes), recipe.name)
        category_name = recipe.category.name if recipe.category else None
        source = await process_recipe(recipe.id, recipe.name, recipe.cuisine, category_name)
        if source == "web_search":
            found_real += 1
        elif source == "ai_generated":
            found_ai += 1
        if i < len(recipes):
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Готово: настоящих фото найдено %d, нарисовано ИИ %d, не удалось получить %d (из %d всего).",
        found_real, found_ai, len(recipes) - found_real - found_ai, len(recipes),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--replace-all", action="store_true",
        help="Заменить ВСЕ фото, включая уже существующие (в т.ч. нарисованные ИИ раньше)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.replace_all))
