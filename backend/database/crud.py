"""
Общие функции доступа к данным, используемые API-роутами backend/main.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.models import (
    Category,
    CookLog,
    Favorite,
    Ingredient,
    Recipe,
    RecipeCustomization,
    RecipeIngredient,
    RecipeStep,
    ShoppingListItem,
    User,
)


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None, full_name: str | None) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    now = datetime.utcnow()
    if user is None:
        user = User(
            telegram_id=telegram_id, username=username, full_name=full_name,
            interaction_count=1, last_seen_at=now,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        user.interaction_count += 1
        user.last_seen_at = now
        await session.commit()
    return user


async def get_or_create_category(
    session: AsyncSession, name: str, emoji: str = "🍽", color: str = "#B5462F",
    sort_order: int = 0, created_by_user_id: int | None = None,
) -> Category:
    result = await session.execute(select(Category).where(Category.name == name))
    obj = result.scalar_one_or_none()
    if obj is None:
        obj = Category(
            name=name, emoji=emoji, color=color, sort_order=sort_order,
            created_by_user_id=created_by_user_id,
        )
        session.add(obj)
        await session.flush()
    return obj


async def get_or_create_ingredient(session: AsyncSession, name: str) -> Ingredient:
    norm = name.strip().lower()
    result = await session.execute(select(Ingredient).where(Ingredient.name == norm))
    obj = result.scalar_one_or_none()
    if obj is None:
        obj = Ingredient(name=norm)
        session.add(obj)
        await session.flush()
    return obj


CATEGORY_KEY_TO_NAME = {
    "soups": "Первые блюда",
    "mains": "Вторые блюда",
    "salads": "Салаты",
    "baking": "Выпечка",
    "desserts": "Десерты",
    "drinks": "Напитки",
    "sauces": "Соусы и закуски",
}


# Слова, которые не несут смысла для сравнения названий блюд (предлоги, союзы
# и самое частое уточняющее слово "с"), чтобы "Рыба фугу" и "Рыба фугу в
# соевом соусе" сравнивались по значимым словам, а не по общей длине строки.
_NAME_STOPWORDS = {
    "с", "со", "в", "во", "на", "из", "и", "или", "по", "для", "от", "к", "а",
}


def _normalize_name_words(name: str) -> frozenset[str]:
    """
    Приводит название блюда к набору значимых слов для сравнения:
    убирает уточнения в скобках (обычно перевод на латинице), пунктуацию
    и разбивает по дефисам, чтобы "суп-пюре" сравнивалось как {суп, пюре}.
    """
    without_parens = re.sub(r"\([^)]*\)", " ", name.lower())
    words = re.split(r"[^а-яёa-z0-9]+", without_parens.replace("-", " "))
    return frozenset(w for w in words if w and w not in _NAME_STOPWORDS)


def names_are_similar(name_a: str, name_b: str) -> bool:
    """
    Приблизительно определяет, что два названия блюда — это, скорее всего,
    одно и то же блюдо (например, из-за разных уточнений у ИИ-генерации):
    "Рыба фугу" / "Рыба фугу в соевом соусе", "Фокачча" / "Фокачча (итальянский
    плоский хлеб)". Не заменяет ручную проверку, но отсеивает очевидные дубли.
    """
    words_a, words_b = _normalize_name_words(name_a), _normalize_name_words(name_b)
    if not words_a or not words_b:
        return False
    if words_a == words_b:
        return True
    # Один набор слов целиком содержится в другом - похоже на тот же
    # рецепт с добавленным уточнением ("рыба фугу" внутри "рыба фугу в соевом соусе").
    if words_a.issubset(words_b) or words_b.issubset(words_a):
        return True
    overlap = len(words_a & words_b) / len(words_a | words_b)
    return overlap >= 0.5


async def find_similar_active_recipe(session: AsyncSession, name: str) -> Recipe | None:
    """
    Ищет среди активных рецептов такой, чьё название похоже на переданное
    (см. names_are_similar). Используется, чтобы не плодить дубли вроде
    "Грибной крем-суп" / "Грибной суп-пюре" при генерации через ИИ.
    """
    result = await session.execute(
        select(Recipe).where(Recipe.is_active.is_(True)).options(selectinload(Recipe.category))
    )
    for recipe in result.scalars().all():
        if names_are_similar(recipe.name, name):
            return recipe
    return None


def normalize_timer_minutes(value) -> int | None:
    """
    Приводит время таймера шага к целому числу минут (в базе и в схеме
    ответа API оно хранится как int). Если ИИ вернул дробное значение
    (например 0.5 для "разогрейте 30 секунд"), округляем и не даём
    результату уйти в 0 - для шага с таймером минимум 1 минута.
    """
    if value is None:
        return None
    try:
        return max(1, round(float(value)))
    except (TypeError, ValueError):
        return None


async def create_recipe_from_ai_data(
    session: AsyncSession, data: dict, source_url: str | None = None, added_by_user_id: int | None = None
) -> Recipe:
    """
    Создаёт рецепт из JSON, полученного от ИИ (backend/ai_recipe.py),
    той же схемы, что и data/seed_recipes.json, плюс поле photo_prompt.
    Помечает рецепт как is_ai_generated=True.

    source_url - ссылка на исходную страницу, если рецепт был импортирован
    (см. backend/recipe_import.py), чтобы указать источник и не нарушать
    авторские права; для обычной генерации "с нуля" - None.

    added_by_user_id - id пользователя (Users.id, не telegram_id), который
    инициировал добавление (через поиск незнакомого блюда или импорт по
    ссылке) - для модерации, см. /admin в bot.py. None для рецептов из
    seed_recipes.json и добавленных вручную админом через JSON.

    Если среди уже сохранённых рецептов находится похожий по названию
    (см. find_similar_active_recipe) - новый не создаётся, возвращается
    существующий, чтобы избежать дублей вроде "Рыба фугу" / "Рыба фугу
    в соевом соусе".
    """
    similar = await find_similar_active_recipe(session, data["name"])
    if similar is not None:
        return similar

    category_name = CATEGORY_KEY_TO_NAME.get(data.get("category"), "Вторые блюда")
    category = await get_or_create_category(session, category_name, created_by_user_id=added_by_user_id)

    recipe = Recipe(
        category_id=category.id,
        name=data["name"],
        photo_prompt=data.get("photo_prompt"),
        time_minutes=data.get("time_minutes", 30),
        difficulty=data.get("difficulty", 2),
        calories=data.get("calories", 0),
        price_level=data.get("price_level", "Недорого"),
        cuisine=data.get("cuisine", "Русская"),
        base_portions=data.get("base_portions", 4),
        description=data.get("description", ""),
        is_ai_generated=True,
        source_url=source_url,
        added_by_user_id=added_by_user_id,
    )
    session.add(recipe)
    await session.flush()

    for ing in data.get("ingredients", []):
        ingredient = await get_or_create_ingredient(session, ing["name"])
        session.add(RecipeIngredient(
            recipe_id=recipe.id, ingredient_id=ingredient.id,
            amount=ing.get("amount", 0), unit=ing.get("unit", ""),
        ))

    for i, step in enumerate(data.get("steps", []), start=1):
        session.add(RecipeStep(
            recipe_id=recipe.id, step_number=i,
            text=step["text"], timer_minutes=normalize_timer_minutes(step.get("timer_minutes")),
        ))

    await session.commit()
    return recipe


async def get_all_categories(session: AsyncSession) -> list[Category]:
    result = await session.execute(select(Category).order_by(Category.sort_order))
    return list(result.scalars().all())


async def get_recipes_by_category(session: AsyncSession, category_id: int) -> list[Recipe]:
    result = await session.execute(
        select(Recipe)
        .where(Recipe.category_id == category_id, Recipe.is_active.is_(True))
        .options(selectinload(Recipe.category))
        .order_by(Recipe.name)
    )
    return list(result.scalars().all())


async def get_recipe_full(session: AsyncSession, recipe_id: int) -> Recipe | None:
    result = await session.execute(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(
            selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
            selectinload(Recipe.steps),
            selectinload(Recipe.category),
        )
    )
    return result.scalar_one_or_none()


async def search_recipes(session: AsyncSession, query: str, limit: int = 30) -> list[Recipe]:
    by_name = await session.execute(
        select(Recipe)
        .where(Recipe.name.ilike(f"%{query}%"), Recipe.is_active.is_(True))
        .options(selectinload(Recipe.category))
        .order_by(Recipe.name)
        .limit(limit)
    )
    by_ingredient = await session.execute(
        select(Recipe)
        .join(RecipeIngredient, RecipeIngredient.recipe_id == Recipe.id)
        .join(Ingredient, Ingredient.id == RecipeIngredient.ingredient_id)
        .where(Ingredient.name.ilike(f"%{query.lower()}%"), Recipe.is_active.is_(True))
        .options(selectinload(Recipe.category))
        .order_by(Recipe.name)
        .distinct()
        .limit(limit)
    )
    seen, combined = set(), []
    for r in list(by_name.scalars().all()) + list(by_ingredient.scalars().all()):
        if r.id not in seen:
            seen.add(r.id)
            combined.append(r)
    return combined[:limit]


async def get_all_ingredient_names(session: AsyncSession) -> list[str]:
    result = await session.execute(select(Ingredient.name).order_by(Ingredient.name))
    return [row[0] for row in result.all()]


async def search_ingredient_names(session: AsyncSession, query: str, limit: int = 20) -> list[str]:
    """
    Ищет продукты по всей базе ингредиентов (не только по короткому списку
    часто используемых) - для строки поиска в разделе "Мой холодильник".
    База ингредиентов пополняется сама по себе с каждым новым рецептом
    (см. get_or_create_ingredient), включая рецепты от ежедневной
    ИИ-генерации, так что искать здесь можно и по недавно появившимся
    продуктам.
    """
    result = await session.execute(
        select(Ingredient.name)
        .where(Ingredient.name.ilike(f"%{query.strip().lower()}%"))
        .order_by(Ingredient.name)
        .limit(limit)
    )
    return [row[0] for row in result.all()]


async def find_recipes_by_available_ingredients(
    session: AsyncSession, available: set[str], limit: int = 30
) -> list[tuple[Recipe, int, int]]:
    result = await session.execute(
        select(Recipe)
        .where(Recipe.is_active.is_(True))
        .options(
            selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
            selectinload(Recipe.category),
        )
    )
    recipes = result.scalars().unique().all()

    scored = []
    for r in recipes:
        names = [link.ingredient.name for link in r.ingredient_links]
        total = len(names)
        if total == 0:
            continue
        matched = sum(1 for n in names if n in available)
        if matched > 0:
            missing = [n for n in names if n not in available]
            scored.append((r, matched, total, missing))

    scored.sort(key=lambda x: (x[1] / x[2], x[1]), reverse=True)
    return scored[:limit]


async def toggle_favorite(session: AsyncSession, user_id: int, recipe_id: int) -> bool:
    result = await session.execute(
        select(Favorite).where(Favorite.user_id == user_id, Favorite.recipe_id == recipe_id)
    )
    fav = result.scalar_one_or_none()
    if fav is None:
        session.add(Favorite(user_id=user_id, recipe_id=recipe_id))
        await session.commit()
        return True
    await session.delete(fav)
    await session.commit()
    return False


async def get_favorite_ids(session: AsyncSession, user_id: int) -> set[int]:
    result = await session.execute(select(Favorite.recipe_id).where(Favorite.user_id == user_id))
    return {row[0] for row in result.all()}


async def get_favorites(session: AsyncSession, user_id: int) -> list[Recipe]:
    result = await session.execute(
        select(Recipe)
        .join(Favorite, Favorite.recipe_id == Recipe.id)
        .where(Favorite.user_id == user_id)
        .options(selectinload(Recipe.category))
        .order_by(Recipe.name)
    )
    return list(result.scalars().all())


async def get_favorites_with_dates(session: AsyncSession, user_id: int) -> list[tuple[Favorite, Recipe]]:
    """
    Избранное вместе с полными данными рецепта (ингредиенты, категория) и
    датой добавления в избранное - для ачивок, которым важен не просто факт
    добавления в избранное, а состав блюда и/или когда это было сделано
    (см. backend/achievements.py).
    """
    result = await session.execute(
        select(Favorite, Recipe)
        .join(Recipe, Recipe.id == Favorite.recipe_id)
        .where(Favorite.user_id == user_id)
        .options(
            selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
            selectinload(Recipe.category),
        )
    )
    return [(fav, recipe) for fav, recipe in result.all()]


async def add_ingredients_to_shopping_list(
    session: AsyncSession, user_id: int, ingredients: list[tuple[str, float, str]]
) -> None:
    for name, amount, unit in ingredients:
        session.add(ShoppingListItem(user_id=user_id, ingredient_name=name, amount=amount, unit=unit))
    await session.commit()


async def get_shopping_list(session: AsyncSession, user_id: int) -> list[ShoppingListItem]:
    result = await session.execute(
        select(ShoppingListItem)
        .where(ShoppingListItem.user_id == user_id)
        .order_by(ShoppingListItem.is_checked, ShoppingListItem.ingredient_name)
    )
    return list(result.scalars().all())


async def get_shopping_item(session: AsyncSession, item_id: int, user_id: int) -> ShoppingListItem | None:
    result = await session.execute(
        select(ShoppingListItem).where(ShoppingListItem.id == item_id, ShoppingListItem.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def clear_checked_shopping_items(session: AsyncSession, user_id: int) -> None:
    result = await session.execute(
        select(ShoppingListItem).where(ShoppingListItem.user_id == user_id, ShoppingListItem.is_checked.is_(True))
    )
    for item in result.scalars().all():
        await session.delete(item)
    await session.commit()


async def get_recipe_customization(
    session: AsyncSession, user_id: int, recipe_id: int
) -> RecipeCustomization | None:
    result = await session.execute(
        select(RecipeCustomization).where(
            RecipeCustomization.user_id == user_id, RecipeCustomization.recipe_id == recipe_id
        )
    )
    return result.scalar_one_or_none()


async def save_recipe_customization(
    session: AsyncSession,
    user_id: int,
    recipe_id: int,
    time_minutes: int | None,
    step_notes: dict[str, str],
) -> RecipeCustomization | None:
    """
    Сохраняет (или создаёт) личные правки пользователя к рецепту. Пустые
    заметки не сохраняются - если после очистки правок ничего не осталось
    (нет ни своего времени, ни заметок), удаляет запись целиком.
    """
    clean_notes = {k: v.strip() for k, v in step_notes.items() if v and v.strip()}
    existing = await get_recipe_customization(session, user_id, recipe_id)

    if time_minutes is None and not clean_notes:
        if existing is not None:
            await session.delete(existing)
            await session.commit()
        return existing

    if existing is None:
        existing = RecipeCustomization(user_id=user_id, recipe_id=recipe_id)
        session.add(existing)

    existing.custom_time_minutes = time_minutes
    existing.step_notes = clean_notes
    await session.commit()
    return existing


async def delete_recipe_customization(session: AsyncSession, user_id: int, recipe_id: int) -> None:
    existing = await get_recipe_customization(session, user_id, recipe_id)
    if existing is not None:
        await session.delete(existing)
        await session.commit()


async def get_usage_stats(session: AsyncSession) -> dict:
    """
    Сводная статистика использования бота для /admin (см. bot.py).
    Активность считается по User.last_seen_at, который обновляется на
    каждое обращение к API (см. get_or_create_user).
    """
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async def count(stmt) -> int:
        return (await session.execute(stmt)).scalar_one()

    users_total = await count(select(func.count(User.id)))
    active_today = await count(select(func.count(User.id)).where(User.last_seen_at >= day_ago))
    active_week = await count(select(func.count(User.id)).where(User.last_seen_at >= week_ago))
    active_month = await count(select(func.count(User.id)).where(User.last_seen_at >= month_ago))

    interactions_total = (await session.execute(select(func.sum(User.interaction_count)))).scalar_one() or 0

    recipes_total = await count(select(func.count(Recipe.id)).where(Recipe.is_active.is_(True)))
    recipes_ai_generated = await count(
        select(func.count(Recipe.id)).where(Recipe.is_active.is_(True), Recipe.is_ai_generated.is_(True))
    )
    recipes_imported = await count(
        select(func.count(Recipe.id)).where(Recipe.is_active.is_(True), Recipe.source_url.is_not(None))
    )
    recipes_added_today = await count(select(func.count(Recipe.id)).where(Recipe.created_at >= day_ago))
    recipes_added_week = await count(select(func.count(Recipe.id)).where(Recipe.created_at >= week_ago))
    recipes_added_month = await count(select(func.count(Recipe.id)).where(Recipe.created_at >= month_ago))

    favorites_total = await count(select(func.count(Favorite.id)))

    return {
        "users_total": users_total,
        "active_today": active_today,
        "active_week": active_week,
        "active_month": active_month,
        "interactions_total": int(interactions_total),
        "recipes_total": recipes_total,
        "recipes_ai_generated": recipes_ai_generated,
        "recipes_imported": recipes_imported,
        "recipes_added_today": recipes_added_today,
        "recipes_added_week": recipes_added_week,
        "recipes_added_month": recipes_added_month,
        "favorites_total": favorites_total,
    }


async def get_top_users(session: AsyncSession, limit: int = 20) -> list[User]:
    """Пользователи, отсортированные по частоте использования бота (interaction_count)."""
    result = await session.execute(
        select(User).order_by(User.interaction_count.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_all_users(session: AsyncSession) -> list[User]:
    """Все пользователи (для полного списка при модерации, см. /admin в bot.py)."""
    result = await session.execute(select(User).order_by(User.interaction_count.desc()))
    return list(result.scalars().all())


async def get_user_submitted_recipes(session: AsyncSession) -> list[Recipe]:
    """
    Рецепты, добавленные пользователями (через поиск незнакомого блюда или
    импорт по ссылке) - для модерации, см. /admin в bot.py. Не включает
    рецепты из seed_recipes.json и добавленные вручную админом через JSON
    (у них added_by_user_id пусто).
    """
    result = await session.execute(
        select(Recipe)
        .where(Recipe.added_by_user_id.is_not(None))
        .options(selectinload(Recipe.added_by))
        .order_by(Recipe.created_at.desc())
    )
    return list(result.scalars().all())


async def record_cook(session: AsyncSession, user_id: int, recipe_id: int, via_random: bool = False) -> CookLog:
    """
    Фиксирует, что пользователь довёл рецепт до конца в режиме готовки
    (см. POST /api/recipes/{id}/cook) - основа для ачивок,
    см. backend/achievements.py.
    """
    log = CookLog(user_id=user_id, recipe_id=recipe_id, via_random=via_random)
    session.add(log)
    await session.commit()
    return log


async def increment_share_count(session: AsyncSession, recipe_id: int) -> None:
    """См. POST /api/recipes/{id}/share - для ачивки 'Притча во языцех'."""
    recipe = await session.get(Recipe, recipe_id)
    if recipe is not None:
        recipe.share_count += 1
        await session.commit()


async def record_share(session: AsyncSession, user_id: int, recipe_id: int) -> None:
    """
    То же самое, что increment_share_count, плюс личный счётчик пользователя
    (для ачивки "Правило бойцовского клуба" - важно, что именно этот
    человек ни разу не делился, а не что рецепт вообще не пересылали).
    """
    recipe = await session.get(Recipe, recipe_id)
    if recipe is not None:
        recipe.share_count += 1
    user = await session.get(User, user_id)
    if user is not None:
        user.shares_initiated_total += 1
    await session.commit()
