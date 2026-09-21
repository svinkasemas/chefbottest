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

Ачивка может требовать, чтобы другая (или несколько других) уже была
разблокирована - см. поле prerequisite_keys. Пока хоть одна из них не
выдана, check() для этой ачивки даже не вызывается (проверять нечего -
условие считается невыполненным). Так делаются "цепочки" вида "сначала
базовая ачивка, потом её более узкая продвинутая версия".
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.models import (
    Category,
    CookLog,
    Favorite,
    Recipe,
    RecipeCustomization,
    RecipeIngredient,
    ShoppingListItem,
    User,
    UserAchievement,
)

INSTANT_KEYS = {
    "tactic_garrison", "remote_access",
    # Третья партия - калькулятор порций (по аналогии с tactic_garrison, см.
    # webapp/app.js) и другие действия, неудобные для постфактум-вычисления.
    "great_hall_feast", "north_remembers", "faceless_feast", "feed_the_crew",
    "morty_sauce", "squidwards_tears",
}

# Платформы Telegram-клиента (tg.platform на фронтенде), которые считаем
# "десктопом" - для ачивки "Удалённый доступ". "weba"/"webk" (веб-версия)
# сознательно не включены - с браузера могут заходить и с телефона тоже.
DESKTOP_PLATFORMS = {"tdesktop", "macos"}


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
_DRINKS_SNACKS_CATEGORY_KEYWORDS = ["напитки", "закуски"]

# --- Ключевые слова для второй партии тематических ачивок -----------------
_ALT_CUTS_KEYWORDS = ["мачете", "фланк", "скёрт", "скерт", "денвер"]
_TEMP_KEYWORDS = [
    "°c", "° c", "градус", "rare", "medium", "well done", "well-done", "медиум", "прожарк",
]
_MARINADE_LONG_KEYWORDS = ["сутки", "суток", "24 час"]
_CAPICOLA_SANDWICH_KEYWORDS = ["капикола", "прошутто", "проволоне"]
_LAMB_KEYWORDS = ["баранина", "ягнён", "ягнят"]
_GRILL_KEYWORDS = ["гриль", "мангал", "открытый огонь", "углях", "углях"]
_PATE_KEYWORDS = ["паштет", "рийет", "намазка"]
_PIZZA_NAME_KEYWORDS = ["пицца"]
_CHEESE_KEYWORD = ["сыр"]
_BURGER_NAME_KEYWORDS = ["бургер"]
_SECRET_KEYWORDS = ["секрет"]
_MILKSHAKE_KEYWORDS = ["коктейль молочный", "милкшейк", "молочный коктейль"]
_BACON_KEYWORDS = ["бекон"]
_PANCAKE_KEYWORDS = ["блин", "панкейк"]
_TEA_NAME_KEYWORDS = ["чай"]
_ROYALE_KEYWORDS = ["роял", "royale"]

# --- Ключевые слова для третьей партии тематических ачивок -----------------
_VEG_KEYWORDS = [
    "морковь", "капуст", "свёкл", "свекл", "картофел", "лук", "перец болгарск",
    "кабачок", "баклажан", "огурец", "огурц", "помидор", "томат", "брокколи",
    "тыкв", "редис", "сельдерей", "шпинат", "зелен", "салат лист",
]
_CREAM_CHEESE_KEYWORDS = ["сливочный сыр", "крем-сыр", "филадельфия"]
_SAUSAGE_HOTDOG_KEYWORDS = ["сосиск", "хот-дог", "хотдог"]
_BELL_PEPPER_KEYWORDS = ["перец болгарск", "болгарский перец"]
_EGG_KEYWORDS = ["яйцо", "яйца", "яичн"]
_QUICK_SNACK_CATEGORY_KEYWORDS = ["закуск", "перекус"]
_MARTINI_GIN_KEYWORDS = ["мартини", "джин"]
_VODKA_KEYWORDS = ["водка"]
_SHAKE_KEYWORDS = ["взболта", "шейкер"]
_DRINK_ME_PHRASE = ["выпей меня"]
_CHERRY_KEYWORDS = ["вишн"]
_COFFEE_KEYWORDS = ["кофе"]
_PORK_BACON_RIBS_KEYWORDS = ["свинина", "бекон", "ребрышк", "рёбрышк", "ребра", "рёбра"]
_BEER_KEYWORDS = ["пиво"]
_DONUT_KEYWORDS = ["пончик", "глазурь для пончик"]
_SWEDISH_MEATBALL_KEYWORDS = ["фрикадельк", "тефтел"]
_SWEET_BUN_KEYWORDS = ["булочк", "плюшк"]
_RAMEN_KEYWORDS = ["рамен"]
_BENTO_KEYWORDS = ["бэнто", "бенто"]
_TAMAGOYAKI_KEYWORDS = ["тамагояки", "яичница с беконом"]
_CROISSANT_KEYWORDS = ["круассан"]
_RIBEYE_KEYWORDS = ["рибай", "стриплойн"]
_LIVER_KEYWORDS = ["печень", "печёнк", "печенк"]
_BEANS_KEYWORDS = ["фасол", "боб"]
_ORGAN_MEAT_KEYWORDS = ["сердце", "лёгкие", "легкие", "язык"]
_FRENCH_TERMS_KEYWORDS = ["фламбировать", "фламбе", "су-вид", "сувид", "деглясировать", "жюльен"]
_THIN_GARLIC_PHRASE_KEYWORDS = ["нарезать чеснок очень тонко", "чеснок лезвием", "чеснок тонко", "чеснок прозрачно"]
_TOMATO_KEYWORDS = ["томат", "помидор"]
_SUGAR_KEYWORDS = ["сахар"]
_MEATBALLS_SAUSAGE_KEYWORDS = ["колбаск", "тефтел", "фрикадельк"]
_FRIED_CHICKEN_KEYWORDS = ["фритюр", "хрустящ", "остр", "панировк", "жарен"]
_FRUIT_KEYWORDS = ["яблок", "банан", "клубник", "малин", "груш", "апельсин", "мандарин", "ананас", "персик", "абрикос"]
_SWEET_ADDITION_KEYWORDS = ["сахар", "сироп", "мёд", "мед"]
_TURKEY_CHICKEN_PIE_KEYWORDS = ["индейк", "куриц", "пирог"]
_MEAT_PIE_KEYWORDS = ["мясной пирог", "пирог с мясом"]
_ASIAN_DISH_KEYWORDS = ["дамплинг", "пельмен", "лапш", "рис"]
_SEAFOOD_BONE_IN_KEYWORDS = ["рыба", "морепродукт", "креветк", "кальмар", "на кости", "ребра", "рёбра"]


def _ingredient_names(recipe: Recipe) -> list[str]:
    return [link.ingredient.name.lower() for link in recipe.ingredient_links]


def _all_text(recipe: Recipe) -> str:
    """Название + описание + ингредиенты + текст шагов, одной строкой в нижнем регистре."""
    parts = [recipe.name or "", recipe.description or ""]
    parts.extend(_ingredient_names(recipe))
    parts.extend(s.text or "" for s in getattr(recipe, "steps", []) or [])
    return " ".join(parts).lower()


def _any_keyword(text_list: list[str], keywords: list[str]) -> bool:
    return any(any(kw in text for kw in keywords) for text in text_list)


@dataclass
class AchievementContext:
    events: list[tuple[CookLog, Recipe]]  # вся история готовки, старые -> новые
    favorites_count: int
    own_recipes: list[Recipe]  # рецепты, добавленные этим пользователем
    created_categories_count: int  # сколько категорий пользователь создал первым
    has_note_on_own_recipe: bool  # оставил заметку к шагу своего же рецепта
    shopping_checked_total: int
    now: datetime

    # --- добавлено в третьей партии ачивок ---------------------------------
    favorite_recipes: list[tuple[Favorite, Recipe]] = field(default_factory=list)
    shopping_items: list[ShoppingListItem] = field(default_factory=list)
    account_age_days: int = 0
    shares_initiated_total: int = 0

    own_recipes_count: int = 0
    distinct_recipe_ids: set[int] = field(default_factory=set)
    cook_dates: set[date] = field(default_factory=set)
    max_day_streak: int = 0
    max_same_recipe_streak: int = 0
    max_morning_recipe_streak: int = 0

    def __post_init__(self) -> None:
        self.own_recipes_count = len(self.own_recipes)
        self.distinct_recipe_ids = {r.id for _, r in self.events}
        self.cook_dates = {log.cooked_at.date() for log, _ in self.events}
        self.max_day_streak = _longest_streak(sorted(self.cook_dates))

        by_recipe: dict[int, list[date]] = defaultdict(list)
        for log, recipe in self.events:
            by_recipe[recipe.id].append(log.cooked_at.date())
        self.max_same_recipe_streak = max(
            (_longest_streak(sorted(set(days))) for days in by_recipe.values()), default=0
        )

        by_recipe_morning: dict[int, list[date]] = defaultdict(list)
        for log, recipe in self.events:
            if 5 <= log.cooked_at.hour <= 10:
                by_recipe_morning[recipe.id].append(log.cooked_at.date())
        self.max_morning_recipe_streak = max(
            (_longest_streak(sorted(set(days))) for days in by_recipe_morning.values()), default=0
        )

    def count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        return sum(1 for _, r in self.events if predicate(r))

    def distinct_count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        return len({r.id for _, r in self.events if predicate(r)})

    def own_count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        return sum(1 for r in self.own_recipes if predicate(r))

    def max_own_category_count_where(self, predicate: Callable[[Recipe], bool]) -> int:
        """Максимум одноимённых по категории собственных рецептов, подходящих под predicate."""
        counts = Counter(r.category_id for r in self.own_recipes if r.category_id and predicate(r))
        return max(counts.values(), default=0)


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


def _shopping_is_exactly(items: list[ShoppingListItem], keywords_a: list[str], keywords_b: list[str]) -> bool:
    """Список покупок состоит ровно из двух позиций, по одной на каждый набор ключевых слов (в любом порядке)."""
    if len(items) != 2:
        return False
    names = [i.ingredient_name.lower() for i in items]
    return (
        (_any_keyword([names[0]], keywords_a) and _any_keyword([names[1]], keywords_b))
        or (_any_keyword([names[0]], keywords_b) and _any_keyword([names[1]], keywords_a))
    )


def _same_day_favorite_pair(
    favorite_recipes: list[tuple[Favorite, Recipe]], keywords_a: list[str], keywords_b: list[str]
) -> bool:
    """В избранное в один и тот же день попали блюда из обоих наборов ключевых слов."""
    by_date: dict[date, list[bool]] = defaultdict(lambda: [False, False])
    for fav, recipe in favorite_recipes:
        d = fav.added_at.date()
        name = recipe.name.lower()
        if _any_keyword([name], keywords_a):
            by_date[d][0] = True
        if _any_keyword([name], keywords_b):
            by_date[d][1] = True
    return any(a and b for a, b in by_date.values())


@dataclass
class Achievement:
    key: str
    title: str
    description: str
    emoji: str
    category: str
    is_hidden: bool = False
    check: Callable[[AchievementContext], bool] = None
    # Другие ачивки (по ключу), которые должны быть уже разблокированы,
    # прежде чем условие этой вообще начнёт проверяться - см. docstring файла.
    prerequisite_keys: tuple[str, ...] = ()


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

    # --- Прогрессия базы (за собственный вклад в общую базу рецептов) -----
    Achievement(
        "wizard_guild", "Гильдия магов отстроена",
        "Добавьте минимум по одному рецепту в 5 разных категорий.",
        "🏛", "Прогрессия базы",
        check=lambda c: len({r.category_id for r in c.own_recipes if r.category_id}) >= 5,
    ),
    Achievement(
        "culinary_catechism", "Кулинарный катехизис",
        "Самостоятельно добавьте 50 рецептов.",
        "📖", "Прогрессия базы",
        check=lambda c: c.own_recipes_count >= 50,
    ),
    Achievement(
        "covenant_tablets", "Скрижали завета",
        "Лично добавьте 100 рецептов в базу.",
        "📜", "Прогрессия базы",
        check=lambda c: c.own_recipes_count >= 100,
    ),
    Achievement(
        "diocese_of_taste", "Епархия вкуса",
        "Создайте 5 собственных уникальных категорий и наполните их.",
        "⛪", "Прогрессия базы",
        check=lambda c: c.created_categories_count >= 5,
    ),

    # --- За качество и дотошность данных ------------------------------------
    Achievement(
        "engineering_precision", "Инженерная точность",
        "Добавьте рецепт с фото, кухней, временем и калориями, и все шаги расписаны.",
        "📐", "Качество данных",
        check=lambda c: c.own_count_where(
            lambda r: bool(r.photo_path) and bool(r.cuisine) and r.time_minutes > 0
            and r.calories > 0 and len(r.steps) > 0 and all(s.text.strip() for s in r.steps)
        ) >= 1,
    ),
    Achievement(
        "alchemical_formula", "Алхимическая формула",
        "Добавьте сложный рецепт из 15 и более ингредиентов.",
        "⚗️", "Качество данных",
        check=lambda c: c.own_count_where(lambda r: len(r.ingredient_links) >= 15) >= 1,
    ),
    Achievement(
        "art_of_minimalism", "Искусство минимализма",
        "Добавьте рецепт из 2 ингредиентов максимум с 2 шагами.",
        "🍳", "Качество данных",
        check=lambda c: c.own_count_where(
            lambda r: len(r.ingredient_links) <= 2 and len(r.steps) <= 2
        ) >= 1,
    ),
    Achievement(
        "talk_of_the_town", "Притча во языцех",
        "Одним из ваших рецептов поделились более 5 раз.",
        "📣", "Качество данных",
        check=lambda c: c.own_count_where(lambda r: r.share_count > 5) >= 1,
    ),

    # --- Технические (способы добавления рецептов) -------------------------
    Achievement(
        "network_scout", "Сетевой разведчик",
        "Добавьте 10 рецептов с помощью импорта по ссылке с других сайтов.",
        "🌐", "Технические",
        check=lambda c: c.own_count_where(lambda r: bool(r.source_url)) >= 10,
    ),

    # --- Тематические и нишевые ---------------------------------------------
    Achievement(
        "family_business", "Семейное дело",
        "Добавьте 10 больших блюд, рассчитанных на компанию от 6 человек.",
        "👨‍👩‍👧‍👦", "Нишевые",
        check=lambda c: c.own_count_where(lambda r: r.base_portions >= 6) >= 10,
    ),
    Achievement(
        "pub_chronicles", "Хроники паба",
        "Добавьте 5 рецептов в категорию «Напитки» или «Закуски».",
        "🍻", "Нишевые",
        check=lambda c: c.own_count_where(
            lambda r: r.category is not None
            and _any_keyword([r.category.name.lower()], _DRINKS_SNACKS_CATEGORY_KEYWORDS)
        ) >= 5,
    ),
    Achievement(
        "voice_crying_out", "Глас вопиющего",
        "Оставьте первую заметку к шагу своего же добавленного рецепта.",
        "📢", "Нишевые",
        check=lambda c: c.has_note_on_own_recipe,
    ),

    # --- Мясная кухня (профильные) ------------------------------------------
    Achievement(
        "meat_sommelier", "Мясной сомелье",
        "Добавьте 10 рецептов с альтернативными отрубами (мачете, фланк, скёрт, денвер).",
        "🥩", "Мясная кухня",
        prerequisite_keys=("meat_baron",),
        check=lambda c: c.own_count_where(lambda r: _any_keyword(_ingredient_names(r), _ALT_CUTS_KEYWORDS)) >= 10,
    ),
    Achievement(
        "grill_maestro", "Маэстро прожарки",
        "Укажите точную температуру или степень прожарки мяса в 5 рецептах.",
        "🌡", "Мясная кухня",
        check=lambda c: c.own_count_where(lambda r: _any_keyword([_all_text(r)], _TEMP_KEYWORDS)) >= 5,
    ),
    Achievement(
        "marinade_alchemy", "Алхимия маринада",
        "Добавьте рецепт, где маринование занимает более 24 часов.",
        "🧫", "Мясная кухня",
        check=lambda c: c.own_count_where(
            lambda r: "марин" in _all_text(r) and _any_keyword([_all_text(r)], _MARINADE_LONG_KEYWORDS)
        ) >= 1,
    ),

    # --- Поп-культура (пасхалки по названию/составу) ------------------------
    Achievement(
        "pizza_party", "Кавабанга!",
        "Добавьте 4 разных рецепта пиццы.",
        "🍕", "Поп-культура",
        check=lambda c: c.own_count_where(lambda r: _any_keyword([r.name.lower()], _PIZZA_NAME_KEYWORDS)) >= 4,
    ),
    Achievement(
        "secret_ingredient_nothing", "Секретный ингредиент — ничего",
        "Приготовьте суп или бульон из 3 ингредиентов или меньше.",
        "🥣", "Поп-культура",
        check=lambda c: c.count_where(
            lambda r: r.category is not None and "перв" in r.category.name.lower() and len(r.ingredient_links) <= 3
        ) >= 1,
    ),
    Achievement(
        "spicy_weasel", "Пряная ласка",
        "Добавьте рецепт с 7 и более разными специями.",
        "🌶", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: len({n for n in _ingredient_names(r) if _any_keyword([n], _SPICE_KEYWORDS)}) >= 7
        ) >= 1,
    ),
    Achievement(
        "scooby_snack", "Скуби-Снэк",
        "Добавьте бутерброд или бургер из 10 и более ингредиентов.",
        "🥪", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _BURGER_NAME_KEYWORDS + ["сэндвич", "бутерброд"])
            and len(r.ingredient_links) >= 10
        ) >= 1,
    ),
    Achievement(
        "lembas_bread", "Второе пришествие Лембаса",
        "Добавьте выпечку с калорийностью от 600 ккал.",
        "🍞", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: r.category is not None and "выпечк" in r.category.name.lower() and r.calories >= 600
        ) >= 1,
    ),
    Achievement(
        "five_dollar_shake", "Пятидолларовый шейк",
        "Добавьте молочный коктейль из 5 и более ингредиентов.",
        "🥤", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _MILKSHAKE_KEYWORDS) and len(r.ingredient_links) >= 5
        ) >= 1,
    ),
    Achievement(
        "ogres_have_layers", "Огры имеют слои",
        "Добавьте рецепт, где лук упоминается 4 и более раз.",
        "🧅", "Поп-культура",
        check=lambda c: c.own_count_where(lambda r: _all_text(r).count("лук") >= 4) >= 1,
    ),
    Achievement(
        "pancakes_and_bacon", "Блинчики с беконом",
        "Добавьте рецепт ровно из блинов и бекона.",
        "🥓", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: len(r.ingredient_links) == 2
            and _any_keyword(_ingredient_names(r), _PANCAKE_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _BACON_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "cheese_moon", "Сырная луна",
        "Добавьте рецепт с 4 и более разными сортами сыра.",
        "🧀", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: len({n for n in _ingredient_names(r) if _any_keyword([n], _CHEESE_KEYWORD)}) >= 4
        ) >= 1,
    ),
    Achievement(
        "royale_with_cheese", "Метрическая система",
        "Добавьте рецепт чизбургера со словом «Роял» в названии или составе.",
        "🍔", "Поп-культура",
        check=lambda c: c.own_count_where(lambda r: _any_keyword([_all_text(r)], _ROYALE_KEYWORDS)) >= 1,
    ),
    Achievement(
        "jasmine_dragon", "Жасминовый дракон",
        "Добавьте 3 разных рецепта заварочного чая в категорию «Напитки».",
        "🍵", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: r.category is not None and "напитки" in r.category.name.lower()
            and _any_keyword([r.name.lower()], _TEA_NAME_KEYWORDS)
        ) >= 3,
    ),
    Achievement(
        "bobs_burger", "Бургер от Боба",
        "Добавьте бургер с названием длиннее 35 символов.",
        "🍟", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _BURGER_NAME_KEYWORDS) and len(r.name) > 35
        ) >= 1,
    ),
    Achievement(
        "secret_formula", "Секретная формула",
        "Добавьте бургер с ингредиентом «секретный соус» без граммовки.",
        "🦀", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _BURGER_NAME_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _SECRET_KEYWORDS)
        ) >= 1,
    ),

    # --- Скрытые (пасхалки) ------------------------------------------------
    Achievement(
        "groundhog_day", "День сурка",
        "Готовьте одно и то же блюдо 3 дня подряд.",
        "🔁", "Пасхалки", is_hidden=True,
        check=lambda c: c.max_same_recipe_streak >= 3,
    ),
    Achievement(
        "gabagool", "Габагул!",
        "Добавьте сэндвич с капиколой, прошутто и проволоне одновременно.",
        "🥪", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: all(kw in _all_text(r) for kw in _CAPICOLA_SANDWICH_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "hoppy_assimilation", "Хмельная ассимиляция",
        "Трижды добавьте рецепт, где пиво используется для тушения или маринада.",
        "🍺", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(lambda r: _any_keyword(_ingredient_names(r), ["пиво"])) >= 3,
    ),
    Achievement(
        "lamb_to_slaughter", "Агнец на заклание",
        "Добавьте первый рецепт из баранины на открытом огне или гриле.",
        "🔥", "Пасхалки", is_hidden=True,
        prerequisite_keys=("meat_baron",),
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _LAMB_KEYWORDS) and _any_keyword([_all_text(r)], _GRILL_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "thermal_paste_replacement", "Горячая замена термопасты",
        "Добавьте 5 рецептов густых паштетов, рийетов или мясных намазок.",
        "🫙", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(lambda r: _any_keyword([r.name.lower()], _PATE_KEYWORDS)) >= 5,
    ),
    Achievement(
        "cloistered_silence", "Келейное безмолвие",
        "Добавьте постный рецепт на 1 порцию из 3 ингредиентов или меньше.",
        "🍵", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: r.base_portions == 1 and len(r.ingredient_links) <= 3
            and not _any_keyword(_ingredient_names(r), _MEAT_KEYWORDS + _FISH_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "remote_access", "Удалённый доступ",
        "Добавьте или отредактируйте рецепт с десктопного клиента Telegram.",
        "🖥", "Пасхалки", is_hidden=True,
        check=lambda c: False,  # мгновенная, см. INSTANT_KEYS
    ),

    # === Третья партия ачивок ===============================================

    # --- Видимые (Поп-культура) ---------------------------------------------
    Achievement(
        "swanson_pyramid", "Пирамида Свонсона",
        "Добавьте рецепт исключительно из мяса и сливочного масла — никаких овощей.",
        "🍖", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: bool(_ingredient_names(r))
            and _any_keyword(_ingredient_names(r), _MEAT_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _BUTTER_KEYWORDS)
            and not any(_any_keyword([n], _VEG_KEYWORDS) for n in _ingredient_names(r))
        ) >= 1,
    ),
    Achievement(
        "breakfast_of_champions", "Завтрак чемпиона",
        "Готовьте одно и то же утреннее блюдо (с 5 до 10 утра) 5 дней подряд.",
        "🥞", "Поп-культура",
        check=lambda c: c.max_morning_recipe_streak >= 5,
    ),
    Achievement(
        "vesuvio_archivist", "Архивариус Везувио",
        "Самостоятельно добавьте 15 и более рецептов итало-американской кухни.",
        "🍝", "Кухни мира",
        prerequisite_keys=("vesuvio_dinner",),
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.cuisine.lower()], _ITALO_AMERICAN_CUISINES)
        ) >= 15,
    ),
    Achievement(
        "anyone_can_cook", "Каждый может готовить",
        "Приготовьте свой самый первый рецепт сразу с максимальным уровнем сложности.",
        "🐭", "Поп-культура",
        check=lambda c: bool(c.events) and c.events[0][1].difficulty == 5,
    ),
    Achievement(
        "hate_mondays", "Ненавижу понедельники",
        "Приготовьте лазанью (или запечённую пасту) в понедельник.",
        "🐱", "Поп-культура",
        check=lambda c: any(
            log.cooked_at.weekday() == 0 and _any_keyword([r.name.lower()], _ZITI_NAME_KEYWORDS)
            for log, r in c.events
        ),
    ),
    Achievement(
        "ichiraku_regular", "Завсегдатай Ичираку",
        "Трижды приготовьте рамен в режиме пошаговой готовки.",
        "🍜", "Кухни мира",
        prerequisite_keys=("wok_master",),
        check=lambda c: c.count_where(lambda r: _any_keyword([r.name.lower()], _RAMEN_KEYWORDS)) >= 3,
    ),
    Achievement(
        "damn_good_coffee", "Чертовски хороший кофе",
        "Добавьте в базу и вишнёвый пирог, и классический чёрный кофе.",
        "☕", "Поп-культура",
        check=lambda c: (
            c.own_count_where(lambda r: "вишн" in r.name.lower() and "пирог" in r.name.lower()) >= 1
            and c.own_count_where(lambda r: "кофе" in r.name.lower()) >= 1
        ),
    ),
    Achievement(
        "pollos_hermanos", "Братья Цыплята",
        "Добавьте в одну категорию 3 разных рецепта курицы во фритюре или в острой панировке.",
        "🍗", "Мясная кухня",
        prerequisite_keys=("meat_baron",),
        check=lambda c: c.max_own_category_count_where(
            lambda r: "куриц" in _all_text(r) and _any_keyword([_all_text(r)], _FRIED_CHICKEN_KEYWORDS)
        ) >= 3,
    ),
    Achievement(
        "miyazaki_food", "Еда от Миядзаки",
        "Добавьте рецепт рамена, бэнто или яичницы с беконом с фотографией готового блюда.",
        "🐷", "Поп-культура",
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _RAMEN_KEYWORDS + _BENTO_KEYWORDS + _TAMAGOYAKI_KEYWORDS)
            and bool(r.photo_path)
        ) >= 1,
    ),

    # --- Скрытые (пасхалки, третья партия) ----------------------------------
    Achievement(
        "lincoln_log_sandwich", "Сэндвичи с бревнами",
        "Добавьте рецепт ровно из двух ингредиентов: сосисок (или хот-догов) и сливочного сыра.",
        "🌭", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: len(r.ingredient_links) == 2
            and _any_keyword(_ingredient_names(r), _SAUSAGE_HOTDOG_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _CREAM_CHEESE_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "junior_order", "Заказ Джуниора",
        "Добавьте рецепт из болгарского перца и яиц с тегом «быстрый перекус».",
        "🍳", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _BELL_PEPPER_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _EGG_KEYWORDS)
            and r.category is not None
            and _any_keyword([r.category.name.lower()], _QUICK_SNACK_CATEGORY_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "shaken_not_stirred", "Смешать, но не взбалтывать",
        "Добавьте коктейль с водкой и мартини (или джином), где упоминается «взболтать» или «шейкер».",
        "🍸", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _VODKA_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _MARTINI_GIN_KEYWORDS)
            and _any_keyword([_all_text(r)], _SHAKE_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "drink_me", "Выпей меня",
        "Добавьте рецепт в категорию «Напитки» с фразой «выпей меня».",
        "🍾", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: r.category is not None and "напитки" in r.category.name.lower()
            and _any_keyword([_all_text(r)], _DRINK_ME_PHRASE)
        ) >= 1,
    ),
    Achievement(
        "who_killed_laura_palmer", "Кто убил Лору Палмер?",
        "Внесите в список покупок ровно два товара: вишню (или вишнёвый пирог) и чёрный кофе.",
        "🦉", "Пасхалки", is_hidden=True,
        check=lambda c: _shopping_is_exactly(c.shopping_items, _CHERRY_KEYWORDS, _COFFEE_KEYWORDS),
    ),
    Achievement(
        "homer_diet", "Диета Гомера",
        "Внесите в список покупок ровно два товара: свинину (бекон/рёбра) и пиво.",
        "🍩", "Пасхалки", is_hidden=True,
        check=lambda c: _shopping_is_exactly(c.shopping_items, _PORK_BACON_RIBS_KEYWORDS, _BEER_KEYWORDS),
    ),
    Achievement(
        "donut_friday", "Ммм... пончики",
        "Внесите пончики (или ингредиенты для них) в список покупок вечером в пятницу.",
        "🍩", "Пасхалки", is_hidden=True,
        check=lambda c: any(
            _any_keyword([i.ingredient_name.lower()], _DONUT_KEYWORDS)
            and i.added_at.weekday() == 4 and i.added_at.hour >= 18
            for i in c.shopping_items
        ),
    ),
    Achievement(
        "swedish_favorites", "В меру упитанный мужчина",
        "Сохраните в избранное шведские фрикадельки и сладкие булочки в один день.",
        "🚁", "Пасхалки", is_hidden=True,
        check=lambda c: _same_day_favorite_pair(c.favorite_recipes, _SWEDISH_MEATBALL_KEYWORDS, _SWEET_BUN_KEYWORDS),
    ),
    Achievement(
        "ignorance_is_bliss", "Неведение — благо",
        "Сохраните в избранное классический стейк рибай или стриплойн.",
        "🥩", "Пасхалки", is_hidden=True,
        check=lambda c: any(_any_keyword([recipe.name.lower()], _RIBEYE_KEYWORDS) for _, recipe in c.favorite_recipes),
    ),
    Achievement(
        "clemenza_sauce", "Соус Клеменцы",
        "Добавьте томатный соус с чесноком, колбасками, вином и щепоткой сахара.",
        "🍅", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _GARLIC_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _TOMATO_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _MEATBALLS_SAUSAGE_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _WINE_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _SUGAR_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "census_taker", "Перепись населения",
        "Добавьте блюдо, где одновременно есть печень, фасоль и вино.",
        "🍷", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _LIVER_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _BEANS_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _WINE_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "hannibals_plating", "Эстетика Ганнибала",
        "Добавьте сложный рецепт (6+ шагов или изысканная техника) с субпродуктами.",
        "🍽", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _ORGAN_MEAT_KEYWORDS)
            and (len(r.steps) >= 6 or _any_keyword([_all_text(r)], _FRENCH_TERMS_KEYWORDS))
        ) >= 1,
    ),
    Achievement(
        "garlic_blade", "Лезвие для чеснока",
        "В шагах рецепта соуса или пасты напишите, что чеснок нарезан очень тонко.",
        "🔪", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword(_ingredient_names(r), _GARLIC_KEYWORDS)
            and _any_keyword([_all_text(r)], _THIN_GARLIC_PHRASE_KEYWORDS)
        ) >= 1,
    ),
    Achievement(
        "secret_ingredient_water", "Секретный ингредиент — вода",
        "Добавьте суп без единой соли или специи — только базовые овощи/мясо и вода.",
        "💧", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: r.category is not None and "перв" in r.category.name.lower()
            and bool(_ingredient_names(r))
            and not _any_keyword(_ingredient_names(r), _SPICE_KEYWORDS + ["соль"])
        ) >= 1,
    ),
    Achievement(
        "last_ziti_karen", "Последний зити Карен",
        "Добавьте зити (или лазанью) с пометкой «заморозить впрок» или «на чёрный день».",
        "🥘", "Пасхалки", is_hidden=True,
        check=lambda c: c.own_count_where(
            lambda r: _any_keyword([r.name.lower()], _ZITI_NAME_KEYWORDS)
            and _any_keyword([_all_text(r)], ["заморозить впрок", "на чёрный день", "на черный день"])
        ) >= 1,
    ),
    Achievement(
        "fight_club_rule", "Правило бойцовского клуба",
        "Не делитесь рецептами через кнопку «Поделиться» весь первый месяц использования.",
        "🥊", "Пасхалки", is_hidden=True,
        check=lambda c: c.account_age_days >= 30 and c.shares_initiated_total == 0,
    ),
    Achievement(
        "squidwards_tears", "Слёзы Сквидварда",
        "Уберите из избранного своё же блюдо из морепродуктов.",
        "😢", "Пасхалки", is_hidden=True,
        check=lambda c: False,  # мгновенная, см. INSTANT_KEYS
    ),
    Achievement(
        "mabel_juice", "Мэйбл-сок",
        "Через «Случайный рецепт» приготовьте блюдо, смешавшее мясо, фрукт и сахар/сироп.",
        "🥤", "Пасхалки", is_hidden=True,
        check=lambda c: any(
            log.via_random
            and _any_keyword(_ingredient_names(r), _MEAT_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _FRUIT_KEYWORDS)
            and _any_keyword(_ingredient_names(r), _SWEET_ADDITION_KEYWORDS)
            for log, r in c.events
        ),
    ),
    Achievement(
        "breakfast_at_tiffanys", "Завтрак у Тиффани",
        "Приготовьте круассаны или кофе между 5 и 7 часами утра.",
        "🥐", "Пасхалки", is_hidden=True,
        check=lambda c: any(
            5 <= log.cooked_at.hour < 7 and _any_keyword([r.name.lower()], _CROISSANT_KEYWORDS + _COFFEE_KEYWORDS)
            for log, r in c.events
        ),
    ),

    # --- Мгновенные (калькулятор порций и поиск, см. INSTANT_KEYS) ---------
    Achievement(
        "great_hall_feast", "Пир в Большом зале",
        "Увеличьте рецепт запечённой курицы, индейки или пирога до 12+ персон.",
        "🍗", "Исследование",
        check=lambda c: False,
    ),
    Achievement(
        "north_remembers", "Север помнит",
        "Увеличьте рецепт мясного пирога до гигантской толпы — 20+ персон.",
        "🐺", "Исследование", is_hidden=True,
        prerequisite_keys=("great_hall_feast",),
        check=lambda c: False,
    ),
    Achievement(
        "feed_the_crew", "Накормить команду",
        "Увеличьте рецепт с морепродуктами или мясом на кости минимум на 15 персон.",
        "🍖", "Исследование",
        check=lambda c: False,
    ),
    Achievement(
        "faceless_feast", "Пир для Безликого",
        "Увеличьте рецепт азиатского блюда (дамплинги, лапша, рис) на 20+ персон.",
        "🍜", "Исследование", is_hidden=True,
        prerequisite_keys=("feed_the_crew",),
        check=lambda c: False,
    ),
    Achievement(
        "morty_sauce", "97 лет, Морти!",
        "Введите в поиске по рецептам фразу «сычуаньский соус».",
        "🥫", "Пасхалки", is_hidden=True,
        check=lambda c: False,
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

    own_recipes = list((
        await session.execute(
            select(Recipe)
            .where(Recipe.added_by_user_id == user_id)
            .options(
                selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
                selectinload(Recipe.category),
                selectinload(Recipe.steps),
            )
        )
    ).scalars().all())

    created_categories_count = (
        await session.execute(
            select(func.count(Category.id)).where(Category.created_by_user_id == user_id)
        )
    ).scalar_one()

    own_recipe_ids = {r.id for r in own_recipes}
    has_note_on_own_recipe = False
    if own_recipe_ids:
        notes = (
            await session.execute(
                select(RecipeCustomization).where(
                    RecipeCustomization.user_id == user_id,
                    RecipeCustomization.recipe_id.in_(own_recipe_ids),
                )
            )
        ).scalars().all()
        has_note_on_own_recipe = any(n.step_notes for n in notes)

    user = await session.get(User, user_id)

    from backend.database import crud  # локальный импорт - избегаем цикла на уровне модуля

    favorite_recipes = await crud.get_favorites_with_dates(session, user_id)

    shopping_items = list((
        await session.execute(select(ShoppingListItem).where(ShoppingListItem.user_id == user_id))
    ).scalars().all())

    account_age_days = (datetime.utcnow() - user.created_at).days if user and user.created_at else 0

    return AchievementContext(
        events=events,
        favorites_count=len(favorites_count),
        own_recipes=own_recipes,
        created_categories_count=created_categories_count,
        has_note_on_own_recipe=has_note_on_own_recipe,
        shopping_checked_total=user.shopping_items_checked_total if user else 0,
        now=datetime.utcnow(),
        favorite_recipes=favorite_recipes,
        shopping_items=shopping_items,
        account_age_days=account_age_days,
        shares_initiated_total=user.shares_initiated_total if user else 0,
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

    # Копия, которую пополняем по ходу цикла - так ачивка с prerequisite_keys
    # может открыться в том же самом вызове, что и её "предок" (если порядок
    # в списке ACHIEVEMENTS выше - предок раньше потомка).
    unlocked_so_far = set(already)

    newly_unlocked: list[Achievement] = []
    for achievement in ACHIEVEMENTS:
        if achievement.key in already or achievement.key in INSTANT_KEYS:
            continue
        if achievement.prerequisite_keys and not all(
            p in unlocked_so_far for p in achievement.prerequisite_keys
        ):
            continue
        if achievement.check(context):
            session.add(UserAchievement(user_id=user_id, achievement_key=achievement.key))
            unlocked_so_far.add(achievement.key)
            newly_unlocked.append(achievement)

    if newly_unlocked:
        await session.commit()
    return newly_unlocked


async def unlock_instant(session: AsyncSession, user_id: int, key: str) -> Achievement | None:
    """
    Выдаёт "мгновенную" ачивку (см. INSTANT_KEYS), если она ещё не выдана и
    (для ачивок-продолжений вроде "Север помнит") её предок уже разблокирован.
    """
    if key not in INSTANT_KEYS or key not in _BY_KEY:
        return None
    already = await get_unlocked_keys(session, user_id)
    if key in already:
        return None
    achievement = _BY_KEY[key]
    if achievement.prerequisite_keys and not all(p in already for p in achievement.prerequisite_keys):
        return None
    session.add(UserAchievement(user_id=user_id, achievement_key=key))
    await session.commit()
    return achievement
