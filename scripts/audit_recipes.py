"""
Проверяет уже сохранённые в базе рецепты на то, не спутано ли название
блюда с каким-то другим, похожим по звучанию (как случилось с «Шаньгой»,
которая получила описание китайской лапши вместо русской лепёшки).

Для каждого рецепта отправляет его название, кухню и описание в ИИ
(тот же Gemini/Groq с прокси-запасным путём, что и генерация рецептов) с
вопросом "это точно то же самое блюдо, а не перепутано с похожим по
звучанию названием?". Ничего не меняет в базе - только печатает список
подозрительных рецептов с пояснением ИИ, что именно не так, и предложенной
правильной кухней.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.audit_recipes

Обработка идёт с паузой между запросами, чтобы не упереться в лимит
бесплатного тарифа ИИ-провайдера - на 109 рецептов уйдёт несколько минут,
торопить скрипт не нужно.
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from backend.ai_recipe import RecipeGenerationError, verify_recipe_dict
from backend.database.db import async_session
from backend.database.models import Recipe

PAUSE_BETWEEN_REQUESTS_SECONDS = 3


async def main() -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Recipe).where(Recipe.is_active.is_(True)).order_by(Recipe.id)
        )
        recipes = list(result.scalars().all())

    print(f"Проверяю {len(recipes)} рецептов...\n")

    suspicious: list[tuple[Recipe, dict]] = []
    for i, r in enumerate(recipes, start=1):
        print(f"[{i}/{len(recipes)}] {r.name} ({r.cuisine})...", end=" ", flush=True)
        try:
            verdict = await asyncio.to_thread(verify_recipe_dict, r.name, r.cuisine, r.description)
        except RecipeGenerationError as e:
            print(f"ошибка проверки: {e}")
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
            continue

        if verdict.get("matches", True):
            print("ок")
        else:
            print("ПОДОЗРИТЕЛЬНО")
            suspicious.append((r, verdict))

        time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)

    print(f"\nГотово. Подозрительных рецептов: {len(suspicious)}\n")
    for r, verdict in suspicious:
        print(f"--- [{r.id}] {r.name!r} ---")
        print(f"  В базе:      кухня={r.cuisine!r}  описание={r.description!r}")
        print(f"  Проблема:    {verdict.get('issue', '(не указана)')}")
        print(f"  ИИ предлагает кухню: {verdict.get('correct_cuisine', '?')}")
        print(f"  Пересоздать рецепт:  python -m scripts.regenerate_recipe --id {r.id}\n")


if __name__ == "__main__":
    asyncio.run(main())
