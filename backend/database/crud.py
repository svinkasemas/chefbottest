"""
Общие функции доступа к данным, используемые API-роутами backend/main.py.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.models import (
    Category,
    Favorite,
    Ingredient,
    Recipe,
    RecipeIngredient,
    ShoppingListItem,
    User,
)


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None, full_name: str | None) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def get_or_create_category(session: AsyncSession, name: str, emoji: str = "🍽", color: str = "#B5462F", sort_order: int = 0) -> Category:
    result = await session.execute(select(Category).where(Category.name == name))
    obj = result.scalar_one_or_none()
    if obj is None:
        obj = Category(name=name, emoji=emoji, color=color, sort_order=sort_order)
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
