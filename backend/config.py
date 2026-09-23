from dotenv import load_dotenv
load_dotenv()

"""
Конфигурация RecipeApp backend.
Чувствительные данные читаются из переменных окружения (.env).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # корень проекта RecipeApp/

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
# Юзернейм бота без @ (например eattomeat_bot) - нужен для диплинков вида
# t.me/USERNAME?startapp=recipe_42, см. GET /api/config в backend/main.py
# и кнопку "Поделиться" на экране рецепта в webapp/app.js.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

# Прокси для подключения бота к api.telegram.org — нужен, если провайдер
# блокирует/режет доступ к этому домену напрямую. Полный URL со схемой,
# например: http://логин:пароль@хост:порт или socks5://логин:пароль@хост:порт
# Если не задан — бот подключается напрямую, без прокси.
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

# Для генерации рецептов через ИИ, если их нет в базе (см. backend/ai_recipe.py)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip() or None
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip() or None
# Запасные провайдеры на случай, если у Gemini/Groq закончился дневной лимит
# бесплатного тарифа (что при росте числа пользователей будет случаться чаще) -
# все дают щедрый бесплатный доступ без банковской карты:
# Cerebras: https://cloud.cerebras.ai (~1 млн токенов/день)
# OpenRouter: https://openrouter.ai/keys (бесплатные модели через "openrouter/free")
# NVIDIA NIM: https://build.nvidia.com (пробные кредиты, ~40 запросов/мин)
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip() or None
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip() or None
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip() or None

# Поиск реального рецепта в интернете по названию блюда (backend/recipe_import.py),
# вместо того чтобы просить ИИ придумать рецепт по памяти. Бесплатно 1000
# запросов/мес без карты: https://app.tavily.com - без ключа тоже работает,
# но с более жёстким лимитом ("keyless"-доступ).
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip() or None

# Поиск настоящих фотографий блюд в свободных источниках для рецептов без
# фото (backend/photo_search.py, scripts/generate_recipe_images.py) - замена
# ИИ-рисованных картинок через Pollinations. Бесплатно, без карты:
# https://www.pexels.com/api - без ключа поиск всё равно работает через
# Openverse (агрегатор изображений с открытыми лицензиями, ключ не нужен),
# но с ключом Pexels результаты обычно качественнее и разнообразнее.
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip() or None

# Google Custom Search JSON API (поиск картинок) - ещё один источник кандидатов,
# обычно точнее Openverse для нишевых домашних блюд. Нужен API-ключ
# (Google Cloud Console - Credentials) и cx - id Programmable Search Engine
# (https://programmablesearchengine.google.com/), настроенной на поиск
# картинок по списку кулинарных сайтов и фотобанков (см. README - "поиск по
# всему интернету" для новых поисковых систем Google больше не даёт включить).
# Бесплатно 100 запросов/день, дальше платно - см. GOOGLE_SEARCH_API_KEY в .env.
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY", "").strip() or None
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX", "").strip() or None

# Разрешить работу без проверки подписи Telegram (ТОЛЬКО для локальной разработки
# в браузере, где нет initData). На проде должно быть False.
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# --- База данных ---
DB_PATH = BASE_DIR / "database.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

# --- Папки ---
PHOTOS_DIR = BASE_DIR / "webapp" / "photos"
DATA_DIR = BASE_DIR / "data"
WEBAPP_DIR = BASE_DIR / "webapp"

for d in (PHOTOS_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- CORS (для локальной разработки фронтенда отдельно от бэкенда) ---
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

DEFAULT_PORTIONS = 4
PORTIONS_OPTIONS = [2, 4, 6, 8]
