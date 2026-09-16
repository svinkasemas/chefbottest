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
from sqlalchemy import func, select

load_dotenv()

from backend.config import ADMIN_IDS, BOT_TOKEN, PROXY_URL  # noqa: E402
from backend.database import crud  # noqa: E402
from backend.database.db import async_session, init_db  # noqa: E402
from backend.database.models import Favorite, Recipe, RecipeIngredient, RecipeStep, User  # noqa: E402

WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

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
        inline_keyboard=[[InlineKeyboardButton(text="🍽 Открыть ChefBot", web_app=WebAppInfo(url=WEBAPP_URL))]]
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
        "Открой приложение кнопкой ниже 👇",
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
        "Чтобы добавить рецепт — пришлите JSON-объект рецепта (см. кнопку ниже).",
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    async with async_session() as session:
        recipes_count = (await session.execute(select(func.count(Recipe.id)))).scalar_one()
        users_count = (await session.execute(select(func.count(User.id)))).scalar_one()
        favorites_count = (await session.execute(select(func.count(Favorite.id)))).scalar_one()

    await callback.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"📚 Рецептов: {recipes_count}\n"
        f"👥 Пользователей: {users_count}\n"
        f"❤️ Избранного добавлено: {favorites_count}"
    )
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
                RecipeStep(recipe_id=recipe.id, step_number=i, text=step["text"], timer_minutes=step.get("timer_minutes"))
            )

        await session.commit()

    await message.answer(f"✅ Рецепт «{data['name']}» добавлен (id {recipe.id}).")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env на основе .env.example.")

    await init_db()

    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="ChefBot", web_app=WebAppInfo(url=WEBAPP_URL)))
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
