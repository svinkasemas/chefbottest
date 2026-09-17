"""
RecipeApp backend — FastAPI-сервер.

Отвечает за:
- REST API для мини-приложения (/api/*)
- раздачу статических файлов фронтенда (папка webapp/) по корню сайта

Запуск (для разработки):
    uvicorn backend.main:app --reload --port 8000

Для продакшена нужен HTTPS-домен — Telegram не откроет Mini App по http.
См. README.md для вариантов хостинга.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import TelegramUser, get_current_user
from backend.config import CORS_ORIGINS, WEBAPP_DIR
from backend.database import crud
from backend.database.db import get_db, init_db
from backend.database.models import Category
from backend.schemas import (
    AddCustomShoppingItemIn,
    AddToShoppingListIn,
    CategoryOut,
    FridgeMatch,
    FridgeMatchIn,
    GenerateRecipeIn,
    IngredientOut,
    RecipeCustomizationIn,
    RecipeDetail,
    RecipeShort,
    ShoppingItemOut,
    StepOut,
    ToggleFavoriteIn,
)
from backend.ai_recipe import RecipeGenerationError, generate_recipe_dict
from backend.utils import scale_amount

# Небольшой предустановленный список продуктов для быстрого выбора в
# разделе "Мой холодильник" на фронтенде (полный поиск по любому продукту
# доступен через /api/search).
COMMON_INGREDIENTS = [
    "картофель", "лук репчатый", "морковь", "курица", "говядина",
    "фарш говяжий", "рис", "макароны", "спагетти", "яйцо куриное",
    "сыр твёрдый", "молоко", "сливки", "помидоры", "огурцы",
    "капуста белокочанная", "грибы шампиньоны", "чеснок", "сметана", "майонез",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="RecipeApp API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_for_index_html(request: Request, call_next):
    """
    Telegram Mini App (особенно мобильный клиент) агрессивно кэширует саму
    страницу index.html - без явного заголовка браузер может продолжать
    показывать старую версию разметки даже после обновления на сервере
    (в отличие от app.js/styles.css, у которых кэш сбрасывается через
    ?v=N в самом index.html). Явный no-cache заставляет клиент каждый раз
    перепроверять актуальность у сервера, а не полагаться на догадку.
    """
    response = await call_next(request)
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def photo_url_for(recipe) -> str | None:
    if not recipe.photo_path:
        return None
    if recipe.photo_path.startswith("http"):
        return recipe.photo_path
    return f"/photos/{recipe.photo_path}"


def recipe_to_short(recipe, favorite_ids: set[int]) -> RecipeShort:
    return RecipeShort(
        id=recipe.id,
        name=recipe.name,
        emoji=recipe.category.emoji if recipe.category else "🍽",
        time_minutes=recipe.time_minutes,
        difficulty=recipe.difficulty,
        price_level=recipe.price_level,
        calories=recipe.calories,
        is_favorite=recipe.id in favorite_ids,
        photo_url=photo_url_for(recipe),
    )


# ---------------------------------------------------------------------------
# Категории и рецепты
# ---------------------------------------------------------------------------

@app.get("/api/categories", response_model=list[CategoryOut])
async def api_categories(db: AsyncSession = Depends(get_db)):
    categories = await crud.get_all_categories(db)
    result = []
    for c in categories:
        recipes = await crud.get_recipes_by_category(db, c.id)
        result.append(CategoryOut(id=c.id, name=c.name, emoji=c.emoji, color=c.color, recipe_count=len(recipes)))
    return result


@app.get("/api/categories/{category_id}/recipes", response_model=list[RecipeShort])
async def api_category_recipes(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    recipes = await crud.get_recipes_by_category(db, category_id)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)
    return [recipe_to_short(r, favorite_ids) for r in recipes]


@app.get("/api/recipes/random", response_model=RecipeShort)
async def api_random_recipe(db: AsyncSession = Depends(get_db), user: TelegramUser = Depends(get_current_user)):
    import random

    from sqlalchemy import select

    from backend.database.models import Recipe

    result = await db.execute(select(Recipe).where(Recipe.is_active.is_(True)))
    recipes = result.scalars().all()
    if not recipes:
        raise HTTPException(404, "В базе пока нет рецептов")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)
    recipe = await crud.get_recipe_full(db, random.choice(recipes).id)
    return recipe_to_short(recipe, favorite_ids)


@app.get("/api/recipes/{recipe_id}", response_model=RecipeDetail)
async def api_recipe_detail(
    recipe_id: int,
    portions: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    recipe = await crud.get_recipe_full(db, recipe_id)
    if recipe is None:
        raise HTTPException(404, "Рецепт не найден")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)
    customization = await crud.get_recipe_customization(db, db_user.id, recipe_id)
    step_notes = customization.step_notes if customization else {}

    target_portions = portions or recipe.base_portions
    ingredients = [
        IngredientOut(
            name=link.ingredient.name,
            amount=scale_amount(link.amount, recipe.base_portions, target_portions),
            unit=link.unit,
            is_optional=link.is_optional,
        )
        for link in recipe.ingredient_links
    ]
    steps = [
        StepOut(
            step_number=s.step_number,
            text=s.text,
            timer_minutes=s.timer_minutes,
            note=step_notes.get(str(s.step_number)),
        )
        for s in recipe.steps
    ]

    return RecipeDetail(
        id=recipe.id,
        name=recipe.name,
        category_id=recipe.category_id,
        category_name=recipe.category.name if recipe.category else "",
        emoji=recipe.category.emoji if recipe.category else "🍽",
        time_minutes=recipe.time_minutes,
        difficulty=recipe.difficulty,
        calories=recipe.calories,
        price_level=recipe.price_level,
        cuisine=recipe.cuisine,
        description=recipe.description,
        base_portions=recipe.base_portions,
        portions=target_portions,
        ingredients=ingredients,
        steps=steps,
        is_favorite=recipe.id in favorite_ids,
        photo_url=photo_url_for(recipe),
        custom_time_minutes=customization.custom_time_minutes if customization else None,
        source_url=recipe.source_url,
    )


@app.put("/api/recipes/{recipe_id}/customize")
async def api_save_recipe_customization(
    recipe_id: int,
    payload: RecipeCustomizationIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    """
    Сохраняет личные правки пользователя к рецепту - своё время
    приготовления и/или заметки к шагам (например "добавить лимон").
    Не меняет сам рецепт в общей базе - видно только автору правок.
    """
    recipe = await crud.get_recipe_full(db, recipe_id)
    if recipe is None:
        raise HTTPException(404, "Рецепт не найден")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    await crud.save_recipe_customization(db, db_user.id, recipe_id, payload.time_minutes, payload.step_notes)
    return {"ok": True}


@app.delete("/api/recipes/{recipe_id}/customize")
async def api_delete_recipe_customization(
    recipe_id: int,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    """Сбрасывает личные правки пользователя к рецепту обратно к оригиналу."""
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    await crud.delete_recipe_customization(db, db_user.id, recipe_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------

@app.get("/api/search", response_model=list[RecipeShort])
async def api_search(
    q: str,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)
    recipes = await crud.search_recipes(db, q)
    return [recipe_to_short(r, favorite_ids) for r in recipes]


@app.post("/api/recipes/generate", response_model=RecipeShort)
async def api_generate_recipe(
    payload: GenerateRecipeIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    import asyncio

    dish_name = payload.name.strip()
    if not dish_name:
        raise HTTPException(400, "Название блюда не может быть пустым")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)

    # если рецепт с таким или похожим названием уже есть - не генерируем повторно
    # (ловит и точные совпадения, и вариации вида "Рыба фугу" / "Рыба фугу в соевом соусе")
    similar = await crud.find_similar_active_recipe(db, dish_name)
    if similar is not None:
        return recipe_to_short(similar, favorite_ids)

    try:
        # generate_recipe_dict синхронный (requests) и может занимать до минуты -
        # выносим в отдельный поток, чтобы не блокировать сервер для остальных пользователей
        data = await asyncio.to_thread(generate_recipe_dict, dish_name)
    except RecipeGenerationError as e:
        raise HTTPException(502, str(e))

    recipe = await crud.create_recipe_from_ai_data(db, data, added_by_user_id=db_user.id)
    recipe_full = await crud.get_recipe_full(db, recipe.id)
    return recipe_to_short(recipe_full, favorite_ids)


# ---------------------------------------------------------------------------
# Мой холодильник
# ---------------------------------------------------------------------------

@app.get("/api/ingredients/common")
async def api_common_ingredients():
    return {"ingredients": COMMON_INGREDIENTS}


@app.get("/api/ingredients/search")
async def api_search_ingredients(q: str, db: AsyncSession = Depends(get_db)):
    q = q.strip()
    if not q:
        return {"ingredients": []}
    names = await crud.search_ingredient_names(db, q)
    return {"ingredients": names}


@app.post("/api/fridge/match", response_model=list[FridgeMatch])
async def api_fridge_match(
    payload: FridgeMatchIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    if not payload.ingredients:
        raise HTTPException(400, "Список продуктов пуст")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)

    available = {i.strip().lower() for i in payload.ingredients}
    scored = await crud.find_recipes_by_available_ingredients(db, available)

    return [
        FridgeMatch(
            recipe=recipe_to_short(recipe, favorite_ids),
            matched=matched,
            total=total,
            percent=round(matched / total * 100),
            missing=missing,
        )
        for recipe, matched, total, missing in scored
    ]


# ---------------------------------------------------------------------------
# Избранное
# ---------------------------------------------------------------------------

@app.get("/api/favorites", response_model=list[RecipeShort])
async def api_favorites(db: AsyncSession = Depends(get_db), user: TelegramUser = Depends(get_current_user)):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    recipes = await crud.get_favorites(db, db_user.id)
    favorite_ids = await crud.get_favorite_ids(db, db_user.id)
    return [recipe_to_short(r, favorite_ids) for r in recipes]


@app.post("/api/favorites/toggle")
async def api_toggle_favorite(
    payload: ToggleFavoriteIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    now_favorite = await crud.toggle_favorite(db, db_user.id, payload.recipe_id)
    return {"is_favorite": now_favorite}


# ---------------------------------------------------------------------------
# Список покупок
# ---------------------------------------------------------------------------

@app.get("/api/shopping-list", response_model=list[ShoppingItemOut])
async def api_get_shopping_list(db: AsyncSession = Depends(get_db), user: TelegramUser = Depends(get_current_user)):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    items = await crud.get_shopping_list(db, db_user.id)
    return [
        ShoppingItemOut(id=i.id, ingredient_name=i.ingredient_name, amount=i.amount, unit=i.unit, is_checked=i.is_checked)
        for i in items
    ]


@app.post("/api/shopping-list/add-recipe")
async def api_add_recipe_to_shopping_list(
    payload: AddToShoppingListIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    recipe = await crud.get_recipe_full(db, payload.recipe_id)
    if recipe is None:
        raise HTTPException(404, "Рецепт не найден")

    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    items = [
        (link.ingredient.name, scale_amount(link.amount, recipe.base_portions, payload.portions), link.unit)
        for link in recipe.ingredient_links
    ]
    await crud.add_ingredients_to_shopping_list(db, db_user.id, items)
    return {"added": len(items)}


@app.post("/api/shopping-list/add-item")
async def api_add_custom_item(
    payload: AddCustomShoppingItemIn,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    await crud.add_ingredients_to_shopping_list(db, db_user.id, [(payload.name, payload.amount, payload.unit)])
    return {"ok": True}


@app.post("/api/shopping-list/{item_id}/toggle")
async def api_toggle_shopping_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: TelegramUser = Depends(get_current_user),
):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    item = await crud.get_shopping_item(db, item_id, db_user.id)
    if item is None:
        raise HTTPException(404, "Позиция не найдена")
    item.is_checked = not item.is_checked
    await db.commit()
    return {"is_checked": item.is_checked}


@app.post("/api/shopping-list/clear-checked")
async def api_clear_checked(db: AsyncSession = Depends(get_db), user: TelegramUser = Depends(get_current_user)):
    db_user = await crud.get_or_create_user(db, user.telegram_id, user.username, user.full_name)
    await crud.clear_checked_shopping_items(db, db_user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Раздача фронтенда мини-приложения (должна быть зарегистрирована последней,
# чтобы не перекрывать маршруты /api/*)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
