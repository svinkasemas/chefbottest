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

from backend.config import GEMINI_API_KEY, GROQ_API_KEY, PROXY_URL

logger = logging.getLogger("ai_recipe")

REQUEST_TIMEOUT_SECONDS = 60

RECIPE_SCHEMA_INSTRUCTIONS = """Ответь СТРОГО одним JSON-объектом, без markdown-разметки, \
без пояснений до или после, по следующей схеме:
{
  "category": "soups|mains|salads|baking|desserts|drinks",
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
  "steps": [{"text": "подробный шаг с конкретной техникой", "timer_minutes": число или null}]
}
Дай 5-9 ингредиентов и 5-8 подробных шагов приготовления (не общими фразами, \
а с конкретными техниками, температурой, временем)."""


class RecipeGenerationError(Exception):
    pass


def build_prompt(dish_name: str) -> str:
    return f"Составь подробный кулинарный рецепт блюда «{dish_name}».\n\n{RECIPE_SCHEMA_INSTRUCTIONS}"


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


def generate_recipe_dict(dish_name: str) -> dict:
    """
    Пробует по очереди: Gemini напрямую, Gemini через прокси,
    Groq напрямую, Groq через прокси. Возвращает распарсенный JSON
    от первого провайдера, который ответил успешно.
    """
    prompt = build_prompt(dish_name)
    attempts = [
        ("gemini", False, call_gemini),
        ("gemini", True, call_gemini),
        ("groq", False, call_groq),
        ("groq", True, call_groq),
    ]
    errors: list[str] = []

    for provider_name, use_proxy, call_fn in attempts:
        try:
            text = call_fn(prompt, use_proxy)
            data = extract_json(text)
            logger.info(
                "Рецепт «%s» сгенерирован провайдером %s (прокси=%s)", dish_name, provider_name, use_proxy
            )
            return data
        except Exception as e:
            label = f"{provider_name}(прокси={use_proxy})"
            logger.warning("Не удалось получить рецепт через %s: %s", label, e)
            errors.append(f"{label}: {e}")

    raise RecipeGenerationError(
        f"Не удалось сгенерировать рецепт «{dish_name}» ни одним провайдером: " + "; ".join(errors)
    )
