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
