"""
Поиск настоящих, лицензионно чистых фотографий блюд для рецептов - замена
ИИ-рисованных картинок (раньше scripts/generate_recipe_images.py генерировал
изображения через Pollinations.ai; теперь вместо рисунка вставляется
реальная фотография из свободного источника).

Источники пробуются по очереди, пока не найдётся подходящее фото:
1. Pexels (https://www.pexels.com/api) - основной источник, даёт лучшее
   качество и разнообразие. Нужен бесплатный PEXELS_API_KEY (см. .env.example) -
   если не задан, этот источник просто пропускается.
2. Openverse (https://openverse.org) - агрегатор изображений с открытыми
   лицензиями (Creative Commons и т.п.). Ключ не нужен вообще - работает
   "из коробки", даже если PEXELS_API_KEY не настроен.

Оба источника отдают лицензионно свободные фотографии (можно использовать
без риска нарушить авторские права), в отличие от прямого скрейпинга
случайных сайтов с рецептами.

Перед поиском название блюда переводится на английский (см.
_translate_query_for_search) - Pexels/Openverse проиндексированы в основном
по англоязычным подписям, и поиск по русскому названию часто вообще не
находил совпадений и подставлял случайное/повторяющееся фото не по теме.
"""
from __future__ import annotations

import logging

import requests

from backend.ai_recipe import _call_with_fallback
from backend.config import PEXELS_API_KEY, PROXY_URL

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def _search_pexels(query: str) -> str | None:
    if not PEXELS_API_KEY:
        return None
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 5, "orientation": "landscape"}
    for use_proxy in (False, True):
        try:
            response = requests.get(
                "https://api.pexels.com/v1/search", headers=headers, params=params,
                timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy),
            )
            response.raise_for_status()
            photos = response.json().get("photos", [])
            if photos:
                # "large" - хорошее разрешение под карточку рецепта, не
                # оригинал в полном размере (экономим трафик/место).
                return photos[0]["src"]["large"]
            return None
        except Exception as e:
            logger.warning("Поиск фото «%s» через Pexels не удался (прокси=%s): %s", query, use_proxy, e)
    return None


def _search_openverse(query: str) -> str | None:
    params = {
        "q": query, "page_size": 5, "license_type": "commercial",
        "category": "photograph", "mature": "false",
    }
    for use_proxy in (False, True):
        try:
            response = requests.get(
                "https://api.openverse.org/v1/images/", params=params,
                timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy),
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            if results and results[0].get("url"):
                return results[0]["url"]
            return None
        except Exception as e:
            logger.warning("Поиск фото «%s» через Openverse не удался (прокси=%s): %s", query, use_proxy, e)
    return None


def _translate_query_for_search(dish_name: str, cuisine: str | None, is_drink: bool) -> str:
    """
    Переводит название блюда (и кухню, если есть) на английский перед
    поиском в Pexels/Openverse. Оба сервиса проиндексированы в основном по
    англоязычным подписям к фото - поиск по русскому названию часто не
    находит ничего релевантного и в итоге подставляет случайное/повторяющееся
    фото не по теме. Если перевод не удался (все ИИ-провайдеры недоступны) -
    используем название как есть (хуже релевантность, но поиск не падает).

    is_drink - для рецептов из категории "Напитки" (коктейли, чай, комбуча и
    т.п.) явно просим фото НАПИТКА, а не еды - иначе (см. баг-репорт
    пользователя) для коктейля вроде "Блэк энд Тэн" находится фото боула с
    едой: слово "food" в запросе уводит поиск не в ту сторону.
    """
    subject = "напитка (коктейля/чая/лимонада и т.п.)" if is_drink else "блюда"
    cuisine_part = f", кухня «{cuisine}»" if cuisine else ""
    prompt = (
        f'Название {subject}: «{dish_name}»{cuisine_part}.\n\n'
        f"Переведи название на английский язык и опиши {subject} коротким "
        f"запросом для поиска его ФОТОГРАФИИ в стоковом фотобанке "
        f"(Pexels/Openverse) - 3-6 английских слов, по которым с высокой "
        f"вероятностью найдётся именно фото {subject}, а не общая картинка "
        f'{"напитка/бара" if is_drink else "еды"}. '
        f'Ответь СТРОГО одним JSON-объектом без markdown-разметки: '
        f'{{"query": "english search phrase"}}'
    )
    try:
        data = _call_with_fallback(prompt, error_subject=f"перевод названия «{dish_name}» для поиска фото")
        query = str(data.get("query", "")).strip()
        if query:
            return query
    except Exception as e:
        logger.warning("Не удалось перевести «%s» для поиска фото, ищу как есть: %s", dish_name, e)
    suffix = "drink cocktail" if is_drink else "food"
    return f"{dish_name} {cuisine} {suffix}" if cuisine else f"{dish_name} {suffix}"


def search_dish_photo(dish_name: str, cuisine: str | None = None, category: str | None = None) -> str | None:
    """
    Ищет фотографию блюда в свободных источниках (см. docstring модуля).
    Возвращает прямую ссылку на изображение или None, если ничего не
    нашлось ни в одном источнике - тогда рецепт остаётся без фото до
    следующего запуска scripts/generate_recipe_images.py.

    category - название категории рецепта ("Напитки", "Десерты" и т.п., см.
    backend.database.crud.CATEGORY_KEY_TO_NAME) - используется только чтобы
    отличить напитки от остальных блюд (см. _translate_query_for_search).
    """
    is_drink = category == "Напитки"
    query = _translate_query_for_search(dish_name, cuisine, is_drink)
    for search_fn in (_search_pexels, _search_openverse):
        url = search_fn(query)
        if url:
            return url
    return None


def download_and_store_photo(recipe_id: int, image_url: str) -> str | None:
    """
    Скачивает найденное фото и сохраняет в PHOTOS_DIR как recipe_{id}.jpg -
    та же схема хранения, что и раньше для ИИ-рисованных картинок, так что
    photo_url_for() в backend/main.py менять не нужно.
    """
    from backend.config import PHOTOS_DIR  # локальный импорт - избегаем цикла

    try:
        response = requests.get(image_url, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(False))
        response.raise_for_status()
        image_bytes = response.content
    except Exception as direct_error:
        try:
            response = requests.get(image_url, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(True))
            response.raise_for_status()
            image_bytes = response.content
        except Exception as proxy_error:
            logger.warning(
                "Не удалось скачать фото рецепта %d: прокси=False: %s; прокси=True: %s",
                recipe_id, direct_error, proxy_error,
            )
            return None

    filename = f"recipe_{recipe_id}.jpg"
    (PHOTOS_DIR / filename).write_bytes(image_bytes)
    return filename
