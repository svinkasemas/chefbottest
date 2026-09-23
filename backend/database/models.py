"""
Модели базы данных RecipeApp.

Таблицы:
- categories          — категории блюд (Первые, Вторые, Салаты ...)
- recipes              — рецепты
- ingredients          — справочник ингредиентов (уникальные названия)
- recipe_ingredients   — связь рецепт <-> ингредиент (с количеством)
- recipe_steps         — пошаговые инструкции приготовления
- users                — пользователи мини-приложения (идентифицируются по telegram_id)
- favorites            — избранные рецепты пользователя
- shopping_list        — список покупок пользователя
- recipe_customizations — личные правки пользователя к рецепту (время, заметки к шагам)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    emoji: Mapped[str] = mapped_column(String(8), default="🍽")
    color: Mapped[str] = mapped_column(String(16), default="#B5462F")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Кто из пользователей первым создал эту категорию (добавив в неё рецепт
    # с новым названием категории) - для ачивки "Епархия вкуса". None для
    # категорий из seed_recipes.json.
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    recipes: Mapped[list["Recipe"]] = relationship(back_populates="category")


class Ingredient(Base):
    __tablename__ = "ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    recipe_links: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="ingredient"
    )


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    photo_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Откуда взято фото - "web_search" (настоящая фотография из свободного
    # источника, подтверждённая Gemini Vision, см. backend/photo_search.py),
    # "ai_generated" (ИИ-иллюстрация - запасной вариант, когда ни один
    # найденный кандидат не подтвердился как настоящее фото этого блюда),
    # "source_page" (og:image со страницы-источника при ручном импорте через
    # /import в bot.py) или NULL (старое фото, ещё не обработанное новым
    # скриптом - либо не задано вовсе). "source_page" никогда не
    # перезаписывается автоматическим подбором (--replace-all его
    # пропускает; "ai_generated" и NULL, наоборот, пробуются заново - вдруг
    # теперь найдётся настоящее подтверждённое фото).
    photo_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Ссылка на исходный сайт, если рецепт был импортирован по URL
    # (см. backend/recipe_import.py) - для указания авторства/источника.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Кто из пользователей добавил рецепт (по ссылке или через поиск блюда,
    # которого не было в базе) - для модерации, см. /admin в bot.py.
    # None для рецептов из seed_recipes.json и добавленных вручную админом.
    added_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Сколько раз рецепт отправляли через кнопку "Поделиться" (см.
    # POST /api/recipes/{id}/share) - для ачивки "Притча во языцех".
    share_count: Mapped[int] = mapped_column(Integer, default=0)

    time_minutes: Mapped[int] = mapped_column(Integer, default=30)
    difficulty: Mapped[int] = mapped_column(Integer, default=2)  # 1..5
    calories: Mapped[int] = mapped_column(Integer, default=0)
    price_level: Mapped[str] = mapped_column(String(20), default="Недорого")
    cuisine: Mapped[str] = mapped_column(String(64), default="Русская")

    base_portions: Mapped[int] = mapped_column(Integer, default=4)
    description: Mapped[str] = mapped_column(Text, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    category: Mapped["Category"] = relationship(back_populates="recipes")
    ingredient_links: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    steps: Mapped[list["RecipeStep"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeStep.step_number",
    )
    added_by: Mapped["User | None"] = relationship(foreign_keys=[added_by_user_id])


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"
    __table_args__ = (UniqueConstraint("recipe_id", "ingredient_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id"))

    amount: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="")
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False)

    recipe: Mapped["Recipe"] = relationship(back_populates="ingredient_links")
    ingredient: Mapped["Ingredient"] = relationship(back_populates="recipe_links")


class RecipeStep(Base):
    __tablename__ = "recipe_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    step_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    timer_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    recipe: Mapped["Recipe"] = relationship(back_populates="steps")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Для статистики использования бота (см. /admin в bot.py): каждое
    # обращение к API увеличивает счётчик и обновляет время последнего
    # захода - грубая, но полезная оценка того, кто и как часто пользуется.
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Сколько товаров в списке покупок пользователь всего отметил купленными
    # за всё время (не сбрасывается при очистке списка) - для ачивки
    # "Запасливый", см. backend/achievements.py.
    shopping_items_checked_total: Mapped[int] = mapped_column(Integer, default=0)
    # Сколько раз пользователь лично нажимал кнопку "Поделиться" рецептом
    # (в отличие от Recipe.share_count, который считает пересылки любого
    # рецепта кем угодно) - для ачивки "Правило бойцовского клуба".
    shares_initiated_total: Mapped[int] = mapped_column(Integer, default=0)
    # Если задано - пользователь состоит в общем списке покупок (см.
    # ShoppingGroup) и видит/редактирует список вместе с остальными
    # участниками, а не только свой собственный.
    shopping_group_id: Mapped[int | None] = mapped_column(ForeignKey("shopping_groups.id"), nullable=True)
    # Пищевые ограничения/аллергии, отмеченные один раз в настройках профиля
    # (например ["nuts", "pork"] - ключи из backend/dietary.py). Рецепты,
    # содержащие эти ингредиенты, помечаются как is_restricted во всех
    # списках рецептов, чтобы фронтенд мог их заблюрить с предупреждением.
    dietary_restrictions: Mapped[list] = mapped_column(JSON, default=list)
    # Свои аллергены/продукты, вписанные вручную (не из готового списка в
    # backend/dietary.py) - например "кинза". Работают так же, как обычные
    # ограничения: совпадение по подстроке с названием/ингредиентами рецепта.
    custom_allergens: Mapped[list] = mapped_column(JSON, default=list)


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShoppingListItem(Base):
    __tablename__ = "shopping_list"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ingredient_name: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="")
    is_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RecipeCustomization(Base):
    """
    Личные правки пользователя к рецепту - своё время приготовления и/или
    заметки к отдельным шагам (например "добавить лимон"). Не меняют сам
    рецепт в общей базе - видны только тому, кто их сделал.
    """
    __tablename__ = "recipe_customizations"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    custom_time_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Заметки к шагам: {"3": "добавить лимон"} - ключ это step_number строкой
    # (JSON-объекты всегда со строковыми ключами).
    step_notes: Mapped[dict] = mapped_column(JSON, default=dict)
    # Свободная личная заметка под рецептом в целом (не привязана к шагу) -
    # например "Готовил 12 октября, жене понравилось, в следующий раз
    # добавить больше чеснока". Видна только автору, см. tpl-recipe.
    personal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CookLog(Base):
    """
    Запись о том, что пользователь довёл рецепт до конца в режиме готовки
    (нажал "Готово!" на последнем шаге). Основа для ачивок - см.
    backend/achievements.py.
    """
    __tablename__ = "cook_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    cooked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Был ли рецепт открыт через кнопку "Случайный рецепт" - для ачивки
    # "Шаг в неизвестность".
    via_random: Mapped[bool] = mapped_column(Boolean, default=False)


class ShoppingGroup(Base):
    """
    Общий список покупок для "Co-op режима" - см. POST /api/shopping-list/share
    и /api/shopping-list/join в backend/main.py. Все пользователи с одним
    и тем же User.shopping_group_id видят и отмечают один и тот же список
    (ShoppingListItem по-прежнему хранит user_id того, кто добавил конкретный
    товар - для отображения "кто добавил", но выборка идёт по всем
    участникам группы, см. crud.get_shopping_member_ids).
    """
    __tablename__ = "shopping_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Короткий код приглашения для диплинка t.me/BOT?startapp=join_<код>,
    # который рассылается через switch_inline_query (см. webapp/app.js).
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserAchievement(Base):
    """Разблокированные ачивки пользователя. См. backend/achievements.py."""
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "achievement_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    achievement_key: Mapped[str] = mapped_column(String(64))
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
