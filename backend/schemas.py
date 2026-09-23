from __future__ import annotations

from pydantic import BaseModel


class CategoryOut(BaseModel):
    id: int
    name: str
    emoji: str
    color: str
    recipe_count: int = 0


class RecipeShort(BaseModel):
    id: int
    name: str
    emoji: str
    time_minutes: int
    difficulty: int
    price_level: str
    calories: int
    is_favorite: bool = False
    photo_url: str | None = None
    # Сколько всего пользователей (не только текущий) добавили рецепт в
    # избранное - показывается как индикатор популярности блюда.
    favorites_count: int = 0
    # Содержит ингредиент(ы) из пищевых ограничений пользователя (см.
    # backend/dietary.py) - карточка на фронтенде блюрится с предупреждением.
    is_restricted: bool = False
    restricted_labels: list[str] = []


class SeasonalShelf(BaseModel):
    title: str
    recipes: list[RecipeShort]


class IngredientOut(BaseModel):
    name: str
    amount: float
    unit: str
    is_optional: bool = False


class StepOut(BaseModel):
    step_number: int
    text: str
    timer_minutes: int | None = None
    note: str | None = None


class RecipeDetail(BaseModel):
    id: int
    name: str
    category_id: int
    category_name: str
    emoji: str
    time_minutes: int
    difficulty: int
    calories: int
    price_level: str
    cuisine: str
    description: str
    base_portions: int
    portions: int
    ingredients: list[IngredientOut]
    steps: list[StepOut]
    is_favorite: bool
    favorites_count: int = 0
    photo_url: str | None = None
    custom_time_minutes: int | None = None
    source_url: str | None = None
    personal_note: str | None = None
    is_restricted: bool = False
    restricted_labels: list[str] = []


class RecipeCustomizationIn(BaseModel):
    """
    Личные правки пользователя к рецепту, отправляемые при сохранении в
    редакторе (см. PUT /api/recipes/{id}/customize). step_notes - словарь
    {номер_шага_строкой: текст_заметки}, пустые заметки можно не включать.
    """
    time_minutes: int | None = None
    step_notes: dict[str, str] = {}
    platform: str | None = None


class CookIn(BaseModel):
    """Тело запроса POST /api/recipes/{id}/cook - см. backend/achievements.py."""
    via_random: bool = False


class FridgeMatch(BaseModel):
    recipe: RecipeShort
    matched: int
    total: int
    percent: int
    missing: list[str]


class ShoppingItemOut(BaseModel):
    id: int
    ingredient_name: str
    amount: float
    unit: str
    is_checked: bool
    # Кто добавил товар - имя показывается только в общем списке покупок
    # (Co-op режим), см. GET /api/shopping-list.
    added_by_name: str | None = None


class JoinShoppingGroupIn(BaseModel):
    invite_code: str


class ToggleFavoriteIn(BaseModel):
    recipe_id: int


class GenerateRecipeIn(BaseModel):
    name: str
    # Платформа Telegram-клиента (tg.platform на фронтенде) - для ачивки
    # "Удалённый доступ" за работу с десктопа, см. backend/achievements.py.
    platform: str | None = None


class FridgeMatchIn(BaseModel):
    ingredients: list[str]


class AddToShoppingListIn(BaseModel):
    recipe_id: int
    portions: int


class AddCustomShoppingItemIn(BaseModel):
    name: str
    amount: float = 0
    unit: str = ""


class RecipeNoteIn(BaseModel):
    """Тело запроса POST /api/recipes/{id}/note - свободная личная заметка."""
    note: str = ""


class DietaryOptionOut(BaseModel):
    key: str
    label: str
    active: bool


class DietarySettingsOut(BaseModel):
    options: list[DietaryOptionOut]
    custom: list[str] = []


class DietarySettingsIn(BaseModel):
    keys: list[str] = []
    custom: list[str] = []
