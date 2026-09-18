"""
Кулинарные ачивки. Основа - CookLog (запись о завершённом приготовлении,
создаётся при нажатии "Готово!" в конце режима готовки, см.
POST /api/recipes/{id}/cook в backend/main.py).

Два вида ачивок:
- "вычисляемые" - проверяются функцией check() против AchievementContext,
  который строится один раз из всей истории готовки пользователя (плюс
  немного смежных данных - избранное, свои рецепты, список покупок).
  Перепроверяются целиком при каждом вызове POST /api/achievements/check.
- "мгновенные" (INSTANT_KEYS) - выдаются прямым вызовом
  POST /api/achievements/unlock/{key} в момент действия на фронтенде,
  которое неудобно вычислять постфактum (например, использование
  калькулятора порций). Ключи вне этого списка через unlock/{key} выдать
  нельзя - см. проверку в backend/main.py.

Скрытые ачивки (is_hidden=True) не показывают название/описание, пока не
разблокированы - на фронтенде отображаются как "???" (см. tpl-achievements
в webapp/index.html).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.models import CookLog, Favorite, Recipe, RecipeIngredient, User, UserAchievement

INSTANT_KEYS = {"tactic_garrison"}


# --------------------------------------------------------------------------- Ключевые слова для определения состава блюда по названиям ингредиентов.
# Ингредиенты в базе - свободный текст ("чёрный молотый перец", "лук
# репчатый"), поэтому матчим по вхождению подстроки, а не точному совпадению.

_ONION_KEYWORDS = ["лук"]
_GARLIC_KEYWORDS = ["чеснок"]
_SPICY_KEYWORDS = ["чили", "халапеньо", "шрирача", "кайенск", "острый перец"]
_MEAT_KEYWORDS = [
    "говядина", "свинина", "баранина", "телятина", "куриц", "индейк",
    "фарш", "грудинка", "буженина", "утка", "утин",
]
_FISH_KEYWORDS = ["рыба", "лосось", "треска", "судак", "сельдь", "форель", "тунец", "скумбри"]
_ROOT_VEG_KEYWORDS = ["картофель", "морковь", "свёкла", "свекла", "репа", "пастернак"]
_BUTTER_KEYWORDS = ["сливочное масло", "масло сливочное"]
_WINE_KEYWORDS = ["вино"]
_HERB_KEYWORDS = ["тимьян", "розмарин", "петрушка", "базилик", "эстрагон", "укроп", "прованск", "шалфей"]
_SPICE_KEYWORDS = [
    "перец", "корица", "кориандр", "куркума", "зира", "паприка", "мускат",
    "гвоздика", "кардамон", "имбирь", "орегано", "тимьян", "базилик",
    "розмарин", "шафран", "ваниль", "лавровый лист", "укроп",
]
_ASIAN_CUISINES = ["китайск", "тайск", "японск", "вьетнамск", "корейск", "азиатск", "индийск"]
_ITALIAN_CUISINES = ["итальянск"]
_ITALO_AMERICAN_CUISINES = ["итало-американск", "итальяно-американск"]
_ZITI_NAME_KEYWORDS = ["зити", "лазанья"]
_CHRISTMAS_NAME_KEYWORDS = ["имбирн", "глинтвейн", "рождеств", "новогодн"]


def _ingredient_names(recipe: Recipe) -> list[str]:
    return [link.ingredient.name.lower() for link in recipe.ingredient_links]


def _any_keyword(text_list: list[str], keywords: list[str]) -> bool:
    return any(any(kw in text for kw in keywords) for text in text_list)


@dataclass
class AchievementContext:
    events: list[tuple[CookLog, Recipe]]  # вся история готовки, старые -> новые
    favorites_count: int
    own_recipes_count: int
    shopping_checked_total: int
    now: datetime

    distinct_recipe_ids: set[int] = field(default_factory=set)
    cook_dates: set[date] = field(default_factory=set)
    max_day_streak: int = 0
    max_same_recipe_streak: int = 0

    def __post_init__(self) -> None:
        self.distinct_recipe_ids = {r.id for _, r in self.events}
        self.cook_dates = {log.cooked_at.date() for log, _ in self.events}
        self.max_day_streak = _longest_streak(sorted(self.cook_dates))

        by_recipe: dict[int, list[date]] = defaultdict(list)
        for log, recipe in self.events:
            by_recipe[recipe.id].append(log.cooked_at.date())
        self.max_same_recipe_streak = max(
            (_longest_streak(sorted(set(days))) for days in by_recipe.values()), default=0
        )

    def count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        return sum(1 for _, r in self.events if predicate(r))

    def distinct_count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        return len({r.id for _, r in self.events if predicate(r)})


def _longest_streak(sorted_days: list[date]) -> int:
    if not sorted_days:
        return 0
    best = current = 1
    for i in range(1, len(sorted_days)):
        gap = (sorted_days[i] - sorted_days[i - 1]).days
        if gap == 1:
            current += 1
            best = max(best, current)
        elif gap > 1:
            current = 1
    return best


@dataclass
class Achievement:
    key: str
    title: str
    description: str
    emoji: str
    category: str
    is_hidden: bool = False
    check: Callable[[AchievementContext], bool] = None


ACHIEVEMENTS: list[Achievement] = [
    # --- Прогрессия и дисциплина ---------------------------------------
    Achievement(
        "first_blood", "Первая кровь (Первый блин не комом)",
        "Приготовьте самое первое блюдо по рецепту из приложения.",
        "🩸", "Прогрессия",
        check=lambda c: len(c.events) >= 1,
    ),
    Achievement(
        "daily_bread", "Хлеб насущный",
        "Готовьте с помощником 3 дня подряд.",
        "🍞", "Прогрессия",
        check=lambda c: c.max_day_streak >= 3,
    ),
    Achievement(
        "kitchen_marathoner", "Кухонный марафонец",
        "Держите стрик готовки 7 дней подряд.",
        "🏃", "Прогрессия",
        check=lambda c: c.max_day_streak >= 7,
    ),
    Achievement(
        "manna", "Манна небесная",
        "Приготовьте 10 блюд не дольше 20 минут.",
        "⚡", "Прогрессия",
        check=lambda c: c.count_where(lambda r: r.time_minutes is not None and r.time_minutes <= 20) >= 10,
    ),
    Achievement(
        "holy_simplicity", "Святая простота",
        "Приготовьте 15 блюд, состоящих из 5 или менее ингредиентов.",
        "🌿", "Прогрессия",
        check=lambda c: c.count_where(lambda r: len(r.ingredient_links) <= 5) >= 15,
    ),
    Achievement(
        "stumbling_block", "Камень преткновения",
        "Успешно завершите рецепт с максимальным уровнем сложности.",
        "🧗", "Прогрессия",
        check=lambda c: c.count_where(lambda r: r.difficulty == 5) >= 1,
    ),
    Achievement(
        "hero_of_ladle", "Герой меча и половника",
        "Приготовьте 50 разных блюд.",
        "🥄", "Прогрессия",
        check=lambda c: len(c.distinct_recipe_ids) >= 50,
    ),

    # --- Культурные и тематические ---------------------------------------
    Achievement(
        "vesuvio_dinner", "Ужин в Vesuvio",
        "Приготовьте 5 блюд итало-американской кухни.",
        "🍝", "Кухни мира",
        check=lambda c: c.count_where(
            lambda r: _any_keyword([r.cuisine.lower()], _ITALO_AMERICAN_CUISINES)
        ) >= 5,
    ),
    Achievement(
        "that_ziti", "Тот самый зити",
        "Приготовьте любую запечённую пасту (зити, лазанью).",
        "🧀", "Кухни мира",
        check=lambda c: c.count_where(
            lambda r: _any_keyword([r.name.lower()], _ZITI_NAME_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "wok_master", "Мастер вока",
        "Приготовьте 5 блюд азиатской кухни.",
        "🥡", "Кухни мира",
        check=lambda c: c.count_where(lambda r: _any_keyword([r.cuisine.lower()], _ASIAN_CUISINES)) >= 5,
    ),
    Achievement(
        "scandi_minimalism", "Скандинавский минимализм",
        "Приготовьте 3 блюда из рыбы или корнеплодов.",
        "🐟", "Кухни мира",
        check=lambda c: c.count_where(
            lambda r: _any_keyword(_ingredient_names(r), _FISH_KEYWORDS + _ROOT_VEG_KEYWORDS)
        ) >= 3,
    ),
    Achievement(
        "french_connection", "Французский связной",
        "Приготовьте блюдо, где есть сливочное масло, вино и травы.",
        "🇫🇷", "Кухни мира",
        check=lambda c: c.count_where(
            lambda r: _any_keyword(_ingredient_names(r), _BUTTER_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _WINE_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _HERB_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "mamma_mia", "Мамма миа!",
        "Приготовьте 10 блюд итальянской кухни.",
        "🇮🇹", "Кухни мира",
        check=lambda c: c.count_where(lambda r: _any_keyword([r.cuisine.lower()], _ITALIAN_CUISINES)) >= 10,
    ),

    # --- Исследовательские (фичи Mini App) --------------------------------
    Achievement(
        "step_into_unknown", "Шаг в неизвестность",
        "Приготовьте блюдо, которое выпало через «Случайный рецепт».",
        "🎲", "Исследование",
        check=lambda c: any(log.via_random for log, _ in c.events),
    ),
    Achievement(
        "tactic_garrison", "Тактика гарнизона",
        "Используйте калькулятор порций, чтобы увеличить рецепт на 6+ человек.",
        "🍽", "Исследование",
        check=lambda c: False,  # выдаётся мгновенно с фронтенда, см. INSTANT_KEYS
    ),
    Achievement(
        "own_contribution", "Внести свою лепту",
        "Добавьте первый собственный рецепт в базу.",
        "➕", "Исследование",
        check=lambda c: c.own_recipes_count >= 1,
    ),
    Achievement(
        "stockpiler", "Запасливый",
        "Отметьте как «купленные» 100 товаров в списке покупок.",
        "🛒", "Исследование",
        check=lambda c: c.shopping_checked_total >= 100,
    ),

    # --- Ингредиентные -----------------------------------------------------
    Achievement(
        "master_of_tears", "Повелитель слёз",
        "Приготовьте 15 блюд с луком.",
        "🧅", "Ингредиенты",
        check=lambda c: c.count_where(lambda r: _any_keyword(_ingredient_names(r), _ONION_KEYWORDS)) >= 15,
    ),
    Achievement(
        "dragons_breath", "Драконье дыхание",
        "Приготовьте 10 острых блюд.",
        "🌶", "Ингредиенты",
        check=lambda c: c.count_where(lambda r: _any_keyword(_ingredient_names(r), _SPICY_KEYWORDS)) >= 10,
    ),
    Achievement(
        "vampire_ward", "Защита от вампиров",
        "Приготовьте 20 блюд с чесноком.",
        "🧄", "Ингредиенты",
        check=lambda c: c.count_where(lambda r: _any_keyword(_ingredient_names(r), _GARLIC_KEYWORDS)) >= 20,
    ),
    Achievement(
        "alchemist", "Алхимик",
        "Используйте в приготовленных блюдах 15 разных специй и пряностей.",
        "🧪", "Ингредиенты",
        check=lambda c: len({
            name for _, r in c.events for name in _ingredient_names(r)
            if _any_keyword([name], _SPICE_KEYWORDS)
        }) >= 15,
    ),
    Achievement(
        "life_is_pain", "Жизнь — боль (но сладкая)",
        "Приготовьте 5 десертов.",
        "🍰", "Ингредиенты",
        check=lambda c: c.count_where(lambda r: r.category_id is not None and _category_is_dessert(r)) >= 5,
    ),
    Achievement(
        "meat_baron", "Мясной барон",
        "Приготовьте 20 блюд из мяса.",
        "🥩", "Ингредиенты",
        check=lambda c: c.count_where(lambda r: _any_keyword(_ingredient_names(r), _MEAT_KEYWORDS)) >= 20,
    ),

    # --- Темпоральные (только один сезон, остальное отложено) -------------
    Achievement(
        "christmas_spirit", "Дух Рождества",
        "Приготовьте праздничное блюдо в декабре.",
        "🎄", "Сезонные",
        check=lambda c: any(
            log.cooked_at.month == 12 and _any_keyword([r.name.lower()], _CHRISTMAS_NAME_KEYWORDS)
            for log, r in c.events
        ),
    ),

    # --- Скрытые (пасхалки) ------------------------------------------------
    Achievement(
        "groundhog_day", "День сурка",
        "Готовьте одно и то же блюдо 3 дня подряд.",
        "🔁", "Пасхалки", is_hidden=True,
        check=lambda c: c.max_same_recipe_streak >= 3,
    ),
]

_BY_KEY = {a.key: a for a in ACHIEVEMENTS}


def _category_is_dessert(recipe: Recipe) -> bool:
    # category передаётся с selectinload при построении контекста
    return bool(recipe.category and recipe.category.name == "Десерты")


async def build_context(session: AsyncSession, user_id: int) -> AchievementContext:
    result = await session.execute(
        select(CookLog, Recipe)
        .join(Recipe, Recipe.id == CookLog.recipe_id)
        .where(CookLog.user_id == user_id)
        .options(
            selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
            selectinload(Recipe.category),
        )
        .order_by(CookLog.cooked_at)
    )
    events = [(log, recipe) for log, recipe in result.all()]

    favorites_count = (
        await session.execute(select(Favorite).where(Favorite.user_id == user_id))
    ).scalars().all()

    own_recipes_count = (
        await session.execute(select(Recipe).where(Recipe.added_by_user_id == user_id))
    ).scalars().all()

    user = await session.get(User, user_id)

    return AchievementContext(
        events=events,
        favorites_count=len(favorites_count),
        own_recipes_count=len(own_recipes_count),
        shopping_checked_total=user.shopping_items_checked_total if user else 0,
        now=datetime.utcnow(),
    )


async def get_unlocked_keys(session: AsyncSession, user_id: int) -> set[str]:
    result = await session.execute(
        select(UserAchievement.achievement_key).where(UserAchievement.user_id == user_id)
    )
    return {row[0] for row in result.all()}


async def check_and_unlock(session: AsyncSession, user_id: int) -> list[Achievement]:
    """
    Пересчитывает условия всех "вычисляемых" ачивок и выдаёт те, что
    выполнены, но ещё не были выданы. Возвращает список новых ачивок
    (обычно 0-1, но может быть больше при первом же приготовлении).
    """
    already = await get_unlocked_keys(session, user_id)
    context = await build_context(session, user_id)

    newly_unlocked: list[Achievement] = []
    for achievement in ACHIEVEMENTS:
        if achievement.key in already or achievement.key in INSTANT_KEYS:
            continue
        if achievement.check(context):
            session.add(UserAchievement(user_id=user_id, achievement_key=achievement.key))
            newly_unlocked.append(achievement)

    if newly_unlocked:
        await session.commit()
    return newly_unlocked


async def unlock_instant(session: AsyncSession, user_id: int, key: str) -> Achievement | None:
    """Выдаёт "мгновенную" ачивку (см. INSTANT_KEYS), если она ещё не выдана."""
    if key not in INSTANT_KEYS or key not in _BY_KEY:
        return None
    already = await get_unlocked_keys(session, user_id)
    if key in already:
        return None
    session.add(UserAchievement(user_id=user_id, achievement_key=key))
    await session.commit()
    return _BY_KEY[key]
