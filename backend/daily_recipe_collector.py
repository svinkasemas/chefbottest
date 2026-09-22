"""
Ежедневный сбор новых рецептов без дублей.

ВАЖНО: рецепт "с нуля" через ИИ НЕ придумывается - добавляются только
реально найденные по ссылке рецепты (см. пункт 3 ниже). ИИ используется
только для того, чтобы подсказать, КАКИЕ названия блюд поискать - не для
сочинения состава и шагов приготовления.

Раз в день (см. настройку автозапуска ниже) скрипт:
1. Смотрит, сколько рецептов уже в каждой категории и что было добавлено
   в последние запуски - чтобы не заваливать одну категорию и не повторяться.
2. Просит ИИ предложить названия блюд-кандидатов (backend/ai_recipe.py,
   suggest_new_dish_names) - с запасом (больше, чем --count), так как не
   для каждого названия в интернете найдётся страница с настоящим рецептом.
3. Для каждого кандидата ищет страницу с реальным рецептом и импортирует
   именно её (search_recipe_url + import_recipe_from_url - тот же пайплайн,
   что и "Не нашли блюдо?" в мини-приложении, backend/main.py,
   api_generate_recipe, только БЕЗ его запасного пути с ИИ-генерацией).
   Если подходящей страницы не нашлось или её не удалось разобрать -
   кандидат просто пропускается, ничего не выдумывается.
4. Настоящая защита от дублей - в crud.create_recipe_from_ai_data
   (find_similar_active_recipe по названию): если похожий рецепт уже есть,
   новая запись не создаётся, возвращается существующая, и такой кандидат
   учитывается как "пропущен", а не "добавлен".
5. Останавливается, как только набрано --count добавленных рецептов (или
   когда кандидаты закончились), и присылает админам (ADMIN_IDS из .env)
   короткую сводку в Telegram.

Запуск вручную (из корня проекта RecipeApp/, с активным venv):
    python -m scripts.daily_recipe_collector [--count N]

Настройка автозапуска раз в день - systemd timer (предпочтительно) или cron:

  systemd timer (на сервере, от root):
    /etc/systemd/system/chefbot-daily-recipes.service:
        [Unit]
        Description=ChefBot daily recipe collector

        [Service]
        Type=oneshot
        User=root
        WorkingDirectory=/opt/chefbot
        ExecStart=/opt/chefbot/venv/bin/python -m scripts.daily_recipe_collector
        EnvironmentFile=/opt/chefbot/.env

    /etc/systemd/system/chefbot-daily-recipes.timer:
        [Unit]
        Description=Run ChefBot daily recipe collector once a day

        [Timer]
        OnCalendar=*-*-* 06:00:00
        Persistent=true

        [Install]
        WantedBy=timers.target

    Затем:
        sudo systemctl daemon-reload
        sudo systemctl enable --now chefbot-daily-recipes.timer
        systemctl list-timers | grep chefbot   # проверить, что таймер встал

  Либо через cron (проще, но без Persistent=true - пропущенный запуск при
  выключенном сервере не наверстается):
        crontab -e
        0 6 * * * cd /opt/chefbot && venv/bin/python -m scripts.daily_recipe_collector >> /var/log/chefbot-daily-recipes.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, select

from backend.ai_recipe import RecipeGenerationError, suggest_new_dish_names
from backend.config import ADMIN_IDS, BOT_TOKEN, PROXY_URL
from backend.database import crud
from backend.database.db import async_session, init_db
from backend.database.models import Category, Recipe
from backend.recipe_import import RecipeImportError, import_recipe_from_url, search_recipe_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_recipe_collector")

DEFAULT_COUNT = 5
# Сколько последних добавленных названий показывать ИИ как "не предлагай
# снова" - без этого соседние запуски (сегодня/завтра) чаще предлагали бы
# одно и то же, пока крупные категории не заполнятся.
RECENT_NAMES_LIMIT = 40
# Во сколько раз запрашивать у ИИ больше названий-кандидатов, чем нужно
# реально добавить: раз рецепт "с нуля" не выдумываем, для многих названий
# в интернете просто не найдётся страницы с рецептом - нужен запас.
CANDIDATE_POOL_MULTIPLIER = 4
MAX_CANDIDATES = 40


async def _existing_ids(session) -> set[int]:
    result = await session.execute(select(Recipe.id).where(Recipe.is_active.is_(True)))
    return {row[0] for row in result.all()}


async def _recent_names(session, limit: int) -> list[str]:
    result = await session.execute(
        select(Recipe.name).where(Recipe.is_active.is_(True)).order_by(Recipe.created_at.desc()).limit(limit)
    )
    return [row[0] for row in result.all()]


async def _category_counts(session) -> dict[str, int]:
    result = await session.execute(
        select(Category.name, func.count(Recipe.id))
        .join(Recipe, Recipe.category_id == Category.id, isouter=True)
        .group_by(Category.id)
        .order_by(Category.sort_order)
    )
    return {name: count for name, count in result.all()}


async def _add_one(session, dish_name: str) -> Recipe | None:
    """
    Пробует найти НАСТОЯЩИЙ рецепт этого блюда по ссылке и добавить именно
    его. Ничего не выдумывает: если подходящей страницы не нашлось или её
    не удалось разобрать - возвращает None, кандидат просто пропускается.
    Дедупликация - внутри crud.create_recipe_from_ai_data, см. docstring модуля.
    """
    found_url = await asyncio.to_thread(search_recipe_url, dish_name)
    if not found_url:
        return None

    try:
        data, _ = await asyncio.to_thread(import_recipe_from_url, found_url)
    except RecipeImportError as e:
        logger.warning("Импорт «%s» с %s не удался: %s", dish_name, found_url, e)
        return None

    return await crud.create_recipe_from_ai_data(session, data, source_url=found_url)


async def collect(count: int) -> dict:
    await init_db()

    async with async_session() as session:
        seen_ids = await _existing_ids(session)
        recent_names = await _recent_names(session, RECENT_NAMES_LIMIT)
        category_counts = await _category_counts(session)

    logger.info("В базе сейчас %d активных рецептов по категориям: %s", len(seen_ids), category_counts)

    pool_size = min(count * CANDIDATE_POOL_MULTIPLIER, MAX_CANDIDATES)
    try:
        candidates = suggest_new_dish_names(category_counts, recent_names, pool_size)
    except RecipeGenerationError as e:
        logger.error("Не удалось получить предложения от ИИ: %s", e)
        return {"added": [], "skipped": [], "no_source": [], "failed": [], "error": str(e)}

    if not candidates:
        logger.warning("ИИ не предложил ни одного блюда")
        return {"added": [], "skipped": [], "no_source": [], "failed": [], "error": None}

    added: list[str] = []
    skipped: list[tuple[str, str]] = []
    no_source: list[str] = []
    failed: list[str] = []

    for dish_name in candidates:
        if len(added) >= count:
            break
        try:
            async with async_session() as session:
                recipe = await _add_one(session, dish_name)
        except Exception as e:
            failed.append(dish_name)
            logger.error("Не удалось добавить «%s»: %s", dish_name, e)
            continue

        if recipe is None:
            no_source.append(dish_name)
            logger.info("Пропущено (не нашлось страницы с настоящим рецептом): «%s»", dish_name)
        elif recipe.id in seen_ids:
            skipped.append((dish_name, recipe.name))
            logger.info("Пропущено (уже есть похожий рецепт): «%s» -> «%s»", dish_name, recipe.name)
        else:
            seen_ids.add(recipe.id)
            added.append(recipe.name)
            logger.info("Добавлен рецепт (по ссылке): %s", recipe.name)

    return {"added": added, "skipped": skipped, "no_source": no_source, "failed": failed, "error": None}


async def notify_admins(result: dict) -> None:
    if not ADMIN_IDS or not BOT_TOKEN:
        logger.info("ADMIN_IDS/BOT_TOKEN не заданы - сводка в Telegram не отправляется")
        return

    lines = ["🌱 <b>Ежедневный сбор рецептов</b>"]
    if result.get("error"):
        lines.append(f"⚠️ Не удалось получить предложения от ИИ: {result['error']}")
    else:
        lines.append(f"✅ Добавлено: {len(result['added'])}")
        if result["added"]:
            lines.append("• " + "\n• ".join(result["added"]))
        if result["skipped"]:
            skipped_names = ", ".join(f"«{dish}»→«{existing}»" for dish, existing in result["skipped"])
            lines.append(f"⏭ Пропущено как дубли ({len(result['skipped'])}): {skipped_names}")
        if result.get("no_source"):
            lines.append(f"🔍 Не нашлось страницы с рецептом ({len(result['no_source'])}): {', '.join(result['no_source'])}")
        if result["failed"]:
            lines.append(f"❌ Не удалось добавить ({len(result['failed'])}): {', '.join(result['failed'])}")

    text = "\n".join(lines)

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    if PROXY_URL:
        from aiogram.client.session.aiohttp import AiohttpSession

        bot = Bot(
            token=BOT_TOKEN,
            session=AiohttpSession(proxy=PROXY_URL),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    else:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.warning("Не удалось отправить сводку админу %s: %s", admin_id, e)
    await bot.session.close()


async def main(count: int) -> None:
    result = await collect(count)
    await notify_admins(result)
    logger.info(
        "Готово: добавлено %d, дублей %d, без источника %d, ошибок %d",
        len(result.get("added", [])), len(result.get("skipped", [])),
        len(result.get("no_source", [])), len(result.get("failed", [])),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Сколько новых блюд пытаться добавить за один запуск")
    args = parser.parse_args()
    asyncio.run(main(args.count))
