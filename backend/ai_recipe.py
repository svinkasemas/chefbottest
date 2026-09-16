"""
Генерация рецепта с помощью ИИ, если его нет в базе.

Тот же принцип отказоустойчивости, что в BeerNewsBot: основной провайдер —
Gemini, запасной — Groq, и каждый из них сначала пробует прямое соединение,
а при отказе — через PROXY_URL из .env.

Модель обязана вернуть валидный JSON в нашей схеме рецепта, включая поле
photo_prompt — точное описание внешнего вида блюда на английском, которое
использует scripts/generate_recipe_images.py для фотогенерации. Так каждый
новый рецепт сам приносит с собой хорошее описание для фото, и не нужно
вручную пополнять общий словарь при каждом новом блюде.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from backend.config import CEREBRAS_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY, OPENROUTER_API_KEY, PROXY_URL

logger = logging.getLogger("ai_recipe")

REQUEST_TIMEOUT_SECONDS = 60

RECIPE_SCHEMA_INSTRUCTIONS = """Ответь СТРОГО одним JSON-объектом, без markdown-разметки, \
без пояснений до или после, по следующей схеме:
{
  "category": "soups|mains|salads|baking|desserts|drinks|sauces",
  "name": "название блюда на русском",
  "time_minutes": число,
  "difficulty": число от 1 до 5,
  "calories": число (ккал на порцию),
  "price_level": "Эконом|Недорого|Средне|Праздничный",
  "cuisine": "название кухни на русском",
  "base_portions": число,
  "description": "1-2 предложения описания на русском",
  "photo_prompt": "точное описание внешнего вида блюда НА АНГЛИЙСКОМ для фотогенерации, например: 'Ukrainian borscht, deep red beet soup with sour cream on top'",
  "ingredients": [{"name": "...", "amount": число, "unit": "г|мл|шт.|ст.л.|ч.л.|по вкусу"}],
  "steps": [{"text": "подробный шаг с конкретной техникой", "timer_minutes": целое число или null}]
}
"timer_minutes" - ЦЕЛОЕ число минут (не дробное). Если по смыслу шага время меньше \
минуты (например, "обжаривайте 30 секунд") - округли до 1, а не указывай 0.5 или 0.
Дай 5-9 ингредиентов. Количество шагов и их детализация должны зависеть от \
реальной сложности блюда, а не быть фиксированными:
- difficulty 1-2 (простое блюдо, например салат или бутерброд): 4-6 шагов;
- difficulty 3 (обычное блюдо среднего уровня): 6-9 шагов;
- difficulty 4-5 (сложное блюдо ресторанного уровня или высокой кухни, \
например Веллингтон, тартар, суфле, многослойный торт с несколькими кремами): \
12-20 шагов, где каждая техника и подэтап - это отдельный шаг (например, для \
Веллингтона отдельными шагами должны идти: обжарка мяса, приготовление и \
уваривание дюкселя, охлаждение мяса, раскатка теста, сборка рулета, охлаждение \
перед выпечкой, смазка яйцом, выпечка, время отдыха перед нарезкой - а не всё \
это одним общим шагом "заверните и запеките").
Ни один шаг не должен объединять в себе несколько разных техник или крупных \
подэтапов - если для сложного блюда описание шага получается длинным и \
охватывает несколько действий, разбейте его на несколько отдельных шагов.
Категория "sauces" — это соусы, заправки и холодные/горячие закуски (брускетты, \
намазки, роллы-закуски и т.п.), не являющиеся отдельным первым или вторым блюдом."""


class RecipeGenerationError(Exception):
    pass


def build_prompt(dish_name: str) -> str:
    return (
        f"Составь подробный кулинарный рецепт блюда «{dish_name}».\n\n"
        f"Важно: это реальное, конкретное блюдо со своей историей и кухней. "
        f"Прежде чем писать рецепт, вспомни, что это блюдо представляет собой "
        f"на самом деле, и не путай его с другим блюдом, которое звучит или "
        f"пишется похоже (например, «Шаньга» — это русская дрожжевая лепёшка "
        f"с картофельной или творожной начинкой, а не блюдо восточной кухни "
        f"с похожим на слух названием). Поле \"cuisine\" должно отражать "
        f"кухню именно этого блюда, а не кухню, к которой оно может показаться "
        f"похожим по звучанию названия. Если существует несколько блюд с таким "
        f"названием — опиши наиболее известный, классический вариант.\n\n"
        f"{RECIPE_SCHEMA_INSTRUCTIONS}"
    )


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise RecipeGenerationError(f"В ответе ИИ не найден JSON: {text[:200]!r}")
    return json.loads(match.group(0))


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def call_gemini(prompt: str, use_proxy: bool) -> str:
    if not GEMINI_API_KEY:
        raise RecipeGenerationError("GEMINI_API_KEY не задан")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    headers = {"X-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_groq(prompt: str, use_proxy: bool) -> str:
    if not GROQ_API_KEY:
        raise RecipeGenerationError("GROQ_API_KEY не задан")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_cerebras(prompt: str, use_proxy: bool) -> str:
    """
    Cerebras даёт щедрый бесплатный тариф (~1 млн токенов/день, без карты) -
    запасной провайдер на случай, если у Gemini и Groq кончился дневной лимит.
    API полностью совместим с форматом OpenAI/Groq. Получить ключ:
    https://cloud.cerebras.ai
    """
    if not CEREBRAS_API_KEY:
        raise RecipeGenerationError("CEREBRAS_API_KEY не задан")
    url = "https://api.cerebras.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}"}
    payload = {
        "model": "llama-3.3-70b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_openrouter(prompt: str, use_proxy: bool) -> str:
    """
    OpenRouter даёт доступ к десяткам бесплатных моделей через один ключ.
    Модель "openrouter/free" сама выбирает доступную бесплатную модель, так
    что не нужно вручную следить, какая именно ещё не устарела/не убрана.
    Получить ключ: https://openrouter.ai/keys
    """
    if not OPENROUTER_API_KEY:
        raise RecipeGenerationError("OPENROUTER_API_KEY не задан")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    payload = {
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_nvidia(prompt: str, use_proxy: bool) -> str:
    """
    NVIDIA NIM - пробные бесплатные кредиты (без карты), API совместим
    с форматом OpenAI. Получить ключ: https://build.nvidia.com
    """
    if not NVIDIA_API_KEY:
        raise RecipeGenerationError("NVIDIA_API_KEY не задан")
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {NVIDIA_API_KEY}"}
    payload = {
        "model": "meta/llama-3.1-70b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def generate_recipe_dict(dish_name: str) -> dict:
    """
    Пробует по очереди: Gemini напрямую, Gemini через прокси,
    Groq напрямую, Groq через прокси. Возвращает распарсенный JSON
    от первого провайдера, который ответил успешно.
    """
    return _call_with_fallback(build_prompt(dish_name), error_subject=f"рецепт «{dish_name}»")


VERIFY_SCHEMA_INSTRUCTIONS = """Ответь СТРОГО одним JSON-объектом, без markdown-разметки, \
без пояснений до или после, по следующей схеме:
{
  "matches": true/false,
  "correct_cuisine": "правильное название кухни на русском",
  "issue": "если matches=false - краткое описание в чём ошибка (1 предложение на русском), иначе пустая строка"
}"""


def build_verify_prompt(name: str, cuisine: str, description: str) -> str:
    return (
        f"В базе данных кулинарного приложения есть такая запись:\n"
        f"Название блюда: «{name}»\n"
        f"Кухня: «{cuisine}»\n"
        f"Описание: «{description}»\n\n"
        f"Проверь, действительно ли название соответствует реальному, известному блюду "
        f"с такой кухней и таким описанием - а не было спутано с каким-то другим блюдом, "
        f"которое звучит или пишется похоже (например, «Шаньга» - это русская дрожжевая "
        f"лепёшка, а не блюдо восточной кухни с похожим по звучанию названием).\n\n"
        f"{VERIFY_SCHEMA_INSTRUCTIONS}"
    )


def verify_recipe_dict(name: str, cuisine: str, description: str) -> dict:
    """
    Просит ИИ проверить, не спутаны ли название, кухня и описание рецепта,
    уже сохранённого в базе, с другим похожим по звучанию блюдом.
    Используется scripts/audit_recipes.py.
    """
    prompt = build_verify_prompt(name, cuisine, description)
    return _call_with_fallback(prompt, error_subject=f"проверка «{name}»")


def _call_with_fallback(prompt: str, error_subject: str) -> dict:
    """
    Пробует по очереди: Gemini напрямую, Gemini через прокси,
    Groq напрямую, Groq через прокси. Возвращает распарсенный JSON
    от первого провайдера, который ответил успешно.
    """
    attempts = [
        ("gemini", False, call_gemini),
        ("gemini", True, call_gemini),
        ("groq", False, call_groq),
        ("groq", True, call_groq),
        ("cerebras", False, call_cerebras),
        ("cerebras", True, call_cerebras),
        ("openrouter", False, call_openrouter),
        ("openrouter", True, call_openrouter),
        ("nvidia", False, call_nvidia),
        ("nvidia", True, call_nvidia),
    ]
    errors: list[str] = []

    for provider_name, use_proxy, call_fn in attempts:
        try:
            text = call_fn(prompt, use_proxy)
            data = extract_json(text)
            logger.info(
                "Запрос «%s» выполнен провайдером %s (прокси=%s)", error_subject, provider_name, use_proxy
            )
            return data
        except Exception as e:
            label = f"{provider_name}(прокси={use_proxy})"
            logger.warning("Не удалось выполнить «%s» через %s: %s", error_subject, label, e)
            errors.append(f"{label}: {e}")

    raise RecipeGenerationError(
        f"Не удалось выполнить «{error_subject}» ни одним провайдером: " + "; ".join(errors)
    )
