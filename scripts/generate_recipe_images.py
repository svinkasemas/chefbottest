"""
Генерация иллюстраций для рецептов через бесплатный API Pollinations.ai
(без ключей, без регистрации — https://image.pollinations.ai).

Проходит по всем рецептам без фото (photo_path IS NULL), строит запрос по
названию и кухне блюда, скачивает картинку и сохраняет в webapp/photos/,
затем прописывает путь в базе.

Если прямое соединение не удаётся (сервис недоступен из вашей сети) —
автоматически пробует через PROXY_URL из .env, тем же способом, что и бот
подключается к Telegram (прямое соединение -> запасной путь через прокси).

Анонимный доступ Pollinations ограничен одним запросом в 15 секунд — скрипт
сам выдерживает паузу между запросами, торопить его не нужно.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.generate_recipe_images
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from urllib.parse import quote

import requests
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.config import PROXY_URL
from backend.database.db import async_session, init_db
from backend.database.models import Recipe, RecipeIngredient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_recipe_images")

PHOTOS_DIR = Path(__file__).resolve().parent.parent / "webapp" / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_DELAY_SECONDS = 16
REQUEST_TIMEOUT_SECONDS = 40


# Кириллические названия блюд плохо распознаются моделью Pollinations —
# она обучена в основном на английских описаниях. Поэтому для каждого
# блюда даём точное английское описание того, как оно ВЫГЛЯДИТ на самом
# деле, а не полагаемся на перевод/распознавание названия моделью.
# При добавлении новых рецептов (в том числе через еженедельный бэклог)
# сюда стоит дописывать соответствующую строку — иначе для них будет
# использован более общий запасной вариант по ингредиентам.
CURATED_VISUAL_DESCRIPTIONS: dict[str, str] = {
    "Борщ украинский": "Ukrainian borscht, deep red beet and cabbage soup in a bowl, with a dollop of sour cream on top",
    "Куриный суп с лапшой": "chicken noodle soup, golden broth with visible egg noodles, carrot slices and chicken pieces",
    "Грибной суп-пюре": "creamy pale beige mushroom cream soup, smooth pureed texture, garnished with a mushroom slice",
    "Гороховый суп с копчёностями": "yellow-green split pea soup with visible chunks of smoked meat",
    "Солянка сборная мясная": "Russian solyanka soup, rich orange-red broth with sliced sausage, olives and a lemon slice on top",
    "Курица с картофелем в духовке": "whole roasted chicken with golden-brown skin surrounded by roasted potato wedges on a baking tray",
    "Котлеты из говяжьего фарша": "pan-fried beef cutlets (kotlety), golden-brown oval patties stacked on a plate",
    "Рыба запечённая с овощами": "baked white fish fillet with roasted zucchini, tomato and onion slices on a tray",
    "Гуляш венгерский": "Hungarian goulash, rich red-brown beef stew with bell pepper chunks in a thick paprika sauce",
    "Плов с курицей": "Uzbek plov, golden pilaf rice with visible carrot strips, chicken pieces and a whole garlic head, in a wide round bowl",
    "Паста карбонара": "Italian spaghetti carbonara, pasta coated in creamy egg sauce with bacon bits and cracked black pepper",
    "Драники картофельные": "crispy golden-brown potato pancakes (draniki) stacked on a plate with a dollop of sour cream",
    "Жаркое по-домашнему": "Russian style beef stew with potato chunks in a thick brown gravy, in a clay pot",
    "Оливье классический": "Russian Olivier salad, diced potato, carrot, pickles and sausage mixed with mayonnaise in a bowl",
    "Винегрет": "Russian vinegret salad, diced beetroot, potato, carrot and pickles in a bowl, vibrant deep red-purple color",
    "Салат Цезарь с курицей": "Caesar salad with grilled chicken strips, croutons, parmesan shavings and lettuce",
    "Греческий салат": "Greek salad with tomato and cucumber chunks, olives, red onion and a block of feta cheese on top",
    "Салат с курицей и ананасом": "layered salad with chicken, pineapple chunks and grated cheese, topped with mayonnaise",
    "Пицца домашняя": "homemade round pizza with melted cheese, sausage slices and tomato sauce, on a wooden board",
    "Ватрушки творожные": "round Russian vatrushka buns with sweet cottage cheese filling in the center, golden crust",
    "Блины классические": "thin Russian pancakes (blini) stacked on a plate",
    "Хачапури по-аджарски": "Georgian adjaruli khachapuri, boat-shaped bread filled with melted cheese and a raw egg yolk in the center",
    "Пирожки с картошкой": "fried Russian pirozhki stuffed buns with potato filling, golden-brown crust, stacked on a plate",
    "Шарлотка с яблоками": "sliced Russian apple charlotte cake showing soft apple filling, dusted with powdered sugar",
    "Медовик": "Russian medovik honey cake, tall multi-layer cake with cream between thin honey layers, a slice cut showing the layers",
    "Панкейки": "stack of fluffy American pancakes with syrup dripping down the sides and a pat of butter on top",
    "Тарт с ягодами": "French berry tart, round pastry shell filled with cream and topped with fresh mixed berries",
    "Профитроли": "French profiteroles, small choux pastry balls filled with cream, stacked and drizzled with chocolate sauce",
    "Тирамису классический": "Italian tiramisu dessert in a glass, layered coffee-soaked ladyfingers and mascarpone cream, dusted with cocoa powder on top",
    "Морс клюквенный": "dark red cranberry morse drink served in a tall glass with ice",
    "Компот из сухофруктов": "amber-colored dried fruit compote in a glass jug with visible pieces of dried apricot and prunes",
    "Лимонад домашний": "homemade lemonade in a glass pitcher with lemon slices and mint leaves, ice cubes visible",
    "Смузи ягодный": "purple-pink berry smoothie in a glass with a straw, topped with fresh berries",
    "Глинтвейн безалкогольный": "warm spiced mulled drink in a glass mug with orange slices and a cinnamon stick",
}


def build_prompt(recipe: Recipe) -> str:
    if recipe.photo_prompt:
        visual = recipe.photo_prompt
    elif CURATED_VISUAL_DESCRIPTIONS.get(recipe.name):
        visual = CURATED_VISUAL_DESCRIPTIONS[recipe.name]
    else:
        ingredient_names = [link.ingredient.name for link in recipe.ingredient_links[:4]]
        ingredients_part = f", made with {', '.join(ingredient_names)}" if ingredient_names else ""
        visual = f"a plate of {recipe.cuisine} cuisine dish{ingredients_part}"

    return (
        f"professional food photography, {visual}, "
        f"appetizing, top-down view, natural light, high detail, realistic"
    )


def fetch_image_bytes(prompt: str) -> bytes:
    seed = random.randint(1, 2_000_000_000)
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=800&height=600&nologo=true&seed={seed}"

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.content
    except requests.RequestException as direct_error:
        if not PROXY_URL:
            raise
        logger.warning("Прямое соединение не удалось (%s), пробую через прокси...", direct_error)
        proxies = {"http": PROXY_URL, "https": PROXY_URL}
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, proxies=proxies)
        response.raise_for_status()
        return response.content


async def main() -> None:
    await init_db()

    async with async_session() as session:
        result = await session.execute(
            select(Recipe)
            .where(Recipe.photo_path.is_(None), Recipe.is_active.is_(True))
            .options(selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient))
        )
        recipes = list(result.scalars().all())

    if not recipes:
        logger.info("У всех активных рецептов уже есть фото — генерировать нечего.")
        return

    logger.info("Рецептов без фото: %d", len(recipes))

    for i, recipe in enumerate(recipes, start=1):
        prompt = build_prompt(recipe)
        filename = f"recipe_{recipe.id}.jpg"
        filepath = PHOTOS_DIR / filename

        logger.info("[%d/%d] %s -> %s", i, len(recipes), recipe.name, prompt)

        try:
            image_bytes = fetch_image_bytes(prompt)
            filepath.write_bytes(image_bytes)

            async with async_session() as session:
                db_recipe = await session.get(Recipe, recipe.id)
                db_recipe.photo_path = filename
                await session.commit()

            logger.info("  сохранено: %s (%d байт)", filepath, len(image_bytes))
        except Exception as e:
            logger.error("  не удалось сгенерировать фото для «%s»: %s", recipe.name, e)

        if i < len(recipes):
            time.sleep(REQUEST_DELAY_SECONDS)

    logger.info("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
