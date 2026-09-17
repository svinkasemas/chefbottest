"""
Тонкий бот-компаньон для ChefBot Mini App.

Роль бота теперь минимальна — вся работа с рецептами происходит в
веб-приложении (см. webapp/ + backend/). Бот только:
  1) открывает Mini App по /start и через постоянную кнопку меню чата;
  2) даёт админ-панель прямо в Telegram (статистика, рассылка,
     добавление/скрытие рецептов) — без необходимости лезть в базу руками.

Запуск (из корня проекта RecipeApp/):
    python -m bot.bot

Обязательно заполните .env: BOT_TOKEN, ADMIN_IDS, WEBAPP_URL (HTTPS-адрес,
на котором развёрнут backend, см. README.md).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MenuButtonWebApp,
    WebAppInfo,
)
from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()

from backend.config import ADMIN_IDS, BOT_TOKEN, PHOTOS_DIR, PROXY_URL  # noqa: E402
from backend.database import crud  # noqa: E402
from backend.database.db import async_session, init_db  # noqa: E402
from backend.database.models import Favorite, Recipe, RecipeIngredient, RecipeStep, User  # noqa: E402
from backend.recipe_import import RecipeImportError, download_image_bytes, import_recipe_from_url  # noqa: E402

WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

# Версия ссылки на Mini App - поднимайте на 1 при каждом деплое фронтенда
# (webapp/index.html, app.js, styles.css). Telegram (особенно мобильный
# клиент) агрессивно кэширует саму страницу Mini App по её URL; изменение
# URL - самый надёжный способ заставить его загрузить свежую версию, не
# полагаясь на HTTP-кэш и не прося пользователей вручную чистить кэш.
WEBAPP_VERSION = "3"


def _webapp_url() -> str:
    if not WEBAPP_URL:
        return WEBAPP_URL
    separator = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{separator}v={WEBAPP_VERSION}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if PROXY_URL:
    logger.info("Использую прокси для подключения к Telegram API: %s", PROXY_URL.split("@")[-1])
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def open_app_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🍽 Открыть ChefBot", web_app=WebAppInfo(url=_webapp_url()))]]
    )


# ---------------------------------------------------------------------------
# Запуск приложения
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not WEBAPP_URL:
        await message.answer(
            "⚠️ Mini App пока не настроен: не задан WEBAPP_URL в .env.\n"
            "Смотрите README.md — нужен HTTPS-адрес, где развёрнут backend."
        )
        return

    async with async_session() as session:
        await crud.get_or_create_user(session, message.from_user.id, message.from_user.username, message.from_user.full_name)

    await message.answer(
        "👋 Привет! Я <b>ChefBot</b> — кухонный помощник.\n\n"
        "Помогу решить главный вопрос: <b>что приготовить сегодня?</b>\n"
        "Открой приложение кнопкой ниже 👇\n\n"
        "💡 Нашли рецепт на другом сайте — просто скиньте мне сюда ссылку на него, "
        "и я добавлю его в общую базу.",
        reply_markup=open_app_kb(),
    )


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------

def admin_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📃 Список рецептов", callback_data="admin_recipes_list")],
        [InlineKeyboardButton(text="➕ Как добавить рецепт", callback_data="admin_add_help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>Панель администратора</b>\n\n"
        "Команды:\n"
        "/recipes_list — список рецептов с id\n"
        "/delete_recipe <id> — скрыть рецепт\n"
        "/broadcast <текст> — рассылка всем пользователям\n\n"
        "Чтобы добавить рецепт — пришлите JSON-объект рецепта (см. кнопку ниже), "
        "или просто скиньте ссылку на рецепт с любого кулинарного сайта — рецепт "
        "будет извлечён со страницы автоматически.",
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    async with async_session() as session:
        stats = await crud.get_usage_stats(session)
        top_users = await crud.get_top_users(session, limit=20)

    lines = [
        "📊 <b>Статистика</b>\n",
        f"👥 Пользователей всего: {stats['users_total']}",
        f"   активны сегодня: {stats['active_today']} · за 7 дней: {stats['active_week']} · за 30 дней: {stats['active_month']}",
        f"🔄 Использований бота всего: {stats['interactions_total']}",
        f"❤️ Избранного добавлено: {stats['favorites_total']}\n",
        f"📚 Рецептов в базе: {stats['recipes_total']} "
        f"(ИИ-генерация: {stats['recipes_ai_generated']}, по ссылке: {stats['recipes_imported']})",
        f"   добавлено сегодня: {stats['recipes_added_today']} · за 7 дней: {stats['recipes_added_week']} · "
        f"за 30 дней: {stats['recipes_added_month']}\n",
        "🏆 <b>Топ по частоте использования:</b>",
    ]

    if not top_users:
        lines.append("(пока никто не пользовался)")
    else:
        for u in top_users:
            label = f"@{u.username}" if u.username else html.escape(u.full_name or f"id{u.telegram_id}")
            last_seen = u.last_seen_at.strftime("%d.%m %H:%M") if u.last_seen_at else "—"
            lines.append(f"{label} — {u.interaction_count} · был(а) {last_seen}")

    await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data == "admin_add_help")
async def admin_add_help(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    example = {
        "category": "soups",
        "name": "Название блюда",
        "time_minutes": 30,
        "difficulty": 2,
        "calories": 200,
        "price_level": "Недорого",
        "cuisine": "Русская",
        "base_portions": 4,
        "description": "Краткое описание.",
        "ingredients": [{"name": "картофель", "amount": 3, "unit": "шт."}],
        "steps": [{"text": "Первый шаг.", "timer_minutes": 10}],
    }
    text = (
        "Чтобы добавить рецепт, пришлите мне сообщением JSON вида:\n\n"
        f"<pre>{json.dumps(example, ensure_ascii=False, indent=2)}</pre>\n\n"
        "category — ключ категории (soups, mains, salads, baking, desserts, drinks, sauces "
        "или новый — категория создастся автоматически)."
    )
    await callback.message.answer(text)
    await callback.answer()


@dp.callback_query(F.data == "admin_recipes_list")
async def admin_recipes_list_cb(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await send_recipes_list(callback.message)
    await callback.answer()


@dp.message(Command("recipes_list"))
async def recipes_list_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await send_recipes_list(message)


async def send_recipes_list(message: Message):
    async with async_session() as session:
        result = await session.execute(select(Recipe).order_by(Recipe.id))
        recipes = result.scalars().all()

    if not recipes:
        await message.answer("Рецептов пока нет.")
        return

    lines = [f"#{r.id} {'✅' if r.is_active else '🚫'} {r.name}" for r in recipes]
    chunk_size = 60
    for i in range(0, len(lines), chunk_size):
        await message.answer("\n".join(lines[i : i + chunk_size]))


@dp.message(Command("delete_recipe"))
async def delete_recipe_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /delete_recipe <id>")
        return
    recipe_id = int(parts[1].strip())

    async with async_session() as session:
        result = await session.execute(select(Recipe).where(Recipe.id == recipe_id))
        recipe = result.scalar_one_or_none()
        if recipe is None:
            await message.answer("Рецепт с таким id не найден.")
            return
        recipe.is_active = False
        await session.commit()

    await message.answer(f"Рецепт «{recipe.name}» скрыт из приложения.")


@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /broadcast <текст сообщения>")
        return
    text = parts[1]

    async with async_session() as session:
        result = await session.execute(select(User.telegram_id).where(User.is_blocked.is_(False)))
        user_ids = [row[0] for row in result.all()]

    sent, failed = 0, 0
    for tg_id in user_ids:
        try:
            await bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            failed += 1

    await message.answer(f"Рассылка завершена. Отправлено: {sent}, не доставлено: {failed}.")


@dp.message(F.text.startswith("{"))
async def try_add_recipe_json(message: Message):
    """Админ присылает JSON рецепта отдельным сообщением — добавляем в базу."""
    if not is_admin(message.from_user.id):
        return

    try:
        data = json.loads(message.text)
    except json.JSONDecodeError as e:
        await message.answer(f"Не удалось разобрать JSON: {e}")
        return

    if not {"category", "name"}.issubset(data.keys()):
        await message.answer("В JSON должны быть как минимум поля 'category' и 'name'.")
        return

    async with async_session() as session:
        category = await crud.get_or_create_category(session, data["category"], "🍽", "#B5462F", 0)

        result = await session.execute(select(Recipe).where(Recipe.name == data["name"]))
        if result.scalar_one_or_none() is not None:
            await message.answer("Рецепт с таким названием уже есть в базе.")
            return

        recipe = Recipe(
            category_id=category.id,
            name=data["name"],
            photo_path=data.get("photo_path"),
            time_minutes=data.get("time_minutes", 30),
            difficulty=data.get("difficulty", 2),
            calories=data.get("calories", 0),
            price_level=data.get("price_level", "Недорого"),
            cuisine=data.get("cuisine", "Русская"),
            base_portions=data.get("base_portions", 4),
            description=data.get("description", ""),
        )
        session.add(recipe)
        await session.flush()

        for ing in data.get("ingredients", []):
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

        for i, step in enumerate(data.get("steps", []), start=1):
            session.add(
                RecipeStep(
                    recipe_id=recipe.id, step_number=i, text=step["text"],
                    timer_minutes=crud.normalize_timer_minutes(step.get("timer_minutes")),
                )
            )

        await session.commit()

    await message.answer(f"✅ Рецепт «{data['name']}» добавлен (id {recipe.id}).")


@dp.message(F.text.regexp(r"^https?://\S+$"))
async def try_add_recipe_from_url(message: Message):
    """
    Любой пользователь может прислать отдельным сообщением ссылку на рецепт
    с внешнего сайта - скачиваем страницу, просим ИИ извлечь из неё рецепт
    (не придумать, а именно перенести реальные ингредиенты и шаги) и
    добавляем в базу, доступную сразу всем в Mini App.
    Работает не только на конкретных сайтах, а на любой странице с текстовым
    рецептом - но некоторые сайты блокируют автоматические запросы (защита
    от ботов), тогда импорт с них не сработает.
    """
    url = message.text.strip()
    status = await message.answer("🔎 Открываю страницу и извлекаю рецепт... это может занять до минуты.")

    try:
        data, image_url = await asyncio.to_thread(import_recipe_from_url, url)
    except RecipeImportError as e:
        await status.edit_text(f"Не удалось импортировать рецепт: {e}")
        return
    except Exception as e:
        logger.exception("Ошибка импорта рецепта по ссылке %s", url)
        await status.edit_text(f"Не удалось импортировать рецепт: {e}")
        return

    async with async_session() as session:
        existing = await crud.find_similar_active_recipe(session, data["name"])
        recipe = await crud.create_recipe_from_ai_data(session, data, source_url=url)

    if existing is not None:
        await status.edit_text(
            f"Похожий рецепт «{recipe.name}» уже есть в базе (id {recipe.id}) — новый не создавал."
        )
        return

    photo_note = "фото появится при следующей ночной генерации (04:00)"
    if image_url:
        try:
            image_bytes = await asyncio.to_thread(download_image_bytes, image_url)
            filename = f"recipe_{recipe.id}.jpg"
            (PHOTOS_DIR / filename).write_bytes(image_bytes)
            async with async_session() as session:
                db_recipe = await session.get(Recipe, recipe.id)
                db_recipe.photo_path = filename
                await session.commit()
            photo_note = "фото взято с исходной страницы"
        except Exception as e:
            logger.warning("Не удалось скачать фото рецепта с %s: %s", image_url, e)

    await status.edit_text(
        f"✅ Рецепт «{recipe.name}» импортирован (id {recipe.id}), {photo_note}.\nИсточник: {url}"
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env на основе .env.example.")

    await init_db()

    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="ChefBot", web_app=WebAppInfo(url=_webapp_url())))
        except Exception as e:
            logger.warning("Не удалось установить кнопку меню чата: %s", e)
    else:
        logger.warning("WEBAPP_URL не задан — кнопка запуска Mini App работать не будет, пока вы не укажете HTTPS-адрес в .env.")

    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
