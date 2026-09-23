"""
Ежедневный подбор ФОТОГРАФИЙ для рецептов без фото.

ВАЖНО: раньше этот скрипт РИСОВАЛ картинки через ИИ (Pollinations.ai). Это
изменено по требованию - теперь вместо рисунка ищется и вставляется
настоящая фотография из свободного, лицензионно чистого источника (Pexels /
Openverse, см. backend/photo_search.py). Название файла и способ
использования не поменялись - существующий systemd-таймер/cron трогать не
нужно, они по-прежнему вызывают этот же модуль.

Как это работает:
1. Ищутся рецепты без фото (Recipe.photo_path IS NULL) - обычный ежедневный
   режим - или ВСЕ рецепты (кроме тех, что помечены как реальное фото со
   страницы-источника), если запущено с --replace-all - для разовой замены
   уже существующих (в т.ч. старых, нарисованных ИИ) картинок.
2. Для каждого ищется фотография по названию блюда в Pexels (если задан
   PEXELS_API_KEY) и/или Openverse (без ключа).
3. Если фото нашлось - скачивается и сохраняется в webapp/photos/, путь
   прописывается в Recipe.photo_path, Recipe.photo_source = "web_search".
4. Если не нашлось - рецепт остаётся без фото, попробуем на следующий запуск.

Обычный ежедневный запуск (только рецепты без фото):
    python -m scripts.generate_recipe_images

Разовая замена ВСЕХ фото, включая уже существующие/нарисованные ИИ раньше
(рецепты с photo_source="source_page" - настоящее фото с сайта-источника,
добавленное вручную через админскую команду /import в bot.py - не трогаются):
    python -m scripts.generate_recipe_images --replace-all

Пауза между запросами - вежливость к бесплатным API (Pexels/Openverse),
чтобы не упереться в рейт-лимит при обработке сразу многих рецептов.
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
from backend.photo_search import download_and_store_photo, search_dish_photo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_recipe_images")

REQUEST_DELAY_SECONDS = 2


async def _recipes_to_process(replace_all: bool) -> list[Recipe]:
    async with async_session() as session:
        # selectinload(Recipe.category) - категория нужна для поиска фото
        # (см. process_recipe/search_dish_photo: у напитков поиск должен
        # просить фото напитка, а не еды), подгружаем сразу, чтобы обращение
        # к recipe.category ниже не требовало отдельного запроса к уже
        # закрытой сессии.
        query = select(Recipe).where(Recipe.is_active.is_(True)).options(selectinload(Recipe.category))
        if replace_all:
            # Разовая замена: рецепты без фото ИЛИ с фото неизвестного
            # происхождения (старое, ещё не помеченное - в т.ч. нарисованное
            # ИИ раньше). "source_page" - настоящее фото с сайта-источника,
            # добавленное вручную - никогда не трогаем.
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


async def process_recipe(recipe_id: int, name: str, cuisine: str | None, category: str | None) -> bool:
    photo_url = await asyncio.to_thread(search_dish_photo, name, cuisine, category)
    if not photo_url:
        logger.info("  фото не найдено: «%s»", name)
        return False

    filename = await asyncio.to_thread(download_and_store_photo, recipe_id, photo_url)
    if not filename:
        logger.warning("  не удалось скачать найденное фото: «%s»", name)
        return False

    async with async_session() as session:
        db_recipe = await session.get(Recipe, recipe_id)
        if db_recipe is not None:
            db_recipe.photo_path = filename
            db_recipe.photo_source = "web_search"
            await session.commit()

    logger.info("  сохранено: %s", filename)
    return True


async def main(replace_all: bool) -> None:
    await init_db()

    recipes = await _recipes_to_process(replace_all)

    if not recipes:
        logger.info("Обрабатывать нечего - у всех активных рецептов уже есть подходящее фото.")
        return

    logger.info("Рецептов для обработки: %d (replace_all=%s)", len(recipes), replace_all)

    found = 0
    for i, recipe in enumerate(recipes, start=1):
        logger.info("[%d/%d] %s", i, len(recipes), recipe.name)
        category_name = recipe.category.name if recipe.category else None
        ok = await process_recipe(recipe.id, recipe.name, recipe.cuisine, category_name)
        if ok:
            found += 1
        if i < len(recipes):
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info("Готово: найдено и вставлено %d фото из %d.", found, len(recipes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--replace-all", action="store_true",
        help="Заменить ВСЕ фото, включая уже существующие (в т.ч. нарисованные ИИ раньше)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.replace_all))
