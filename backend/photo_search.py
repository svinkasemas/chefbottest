"""
Подбор фото для рецептов без фото: сначала пробуем найти настоящую,
лицензионно чистую фотографию блюда в свободных источниках, а если для
блюда не нашлось ничего подходящего - рисуем иллюстрацию через ИИ
(Pollinations.ai) как запасной вариант, чтобы у рецепта в любом случае была
подходящая по смыслу картинка, а не пустое место или случайное чужое фото.

Реальное фото ищется по очереди в:
1. Google Custom Search (Programmable Search Engine, поиск картинок) -
   обычно самый точный источник для нишевых домашних блюд, ищет по
   заранее настроенному списку кулинарных сайтов и фотобанков (Google с
   2023 года не даёт новым поисковым системам искать "по всему
   интернету" - поэтому список сайтов, а не открытый веб-поиск). Нужны
   GOOGLE_SEARCH_API_KEY и GOOGLE_SEARCH_CX (см. .env.example) - если не
   заданы, источник пропускается.
2. Pexels (https://www.pexels.com/api) - даёт хорошее качество и
   разнообразие. Нужен бесплатный PEXELS_API_KEY (см. .env.example) -
   если не задан, этот источник просто пропускается.
3. Openverse (https://openverse.org) - агрегатор изображений с открытыми
   лицензиями (Creative Commons и т.п.). Ключ не нужен вообще - работает
   "из коробки", даже если ничего из вышеперечисленного не настроено.

Название блюда перед поиском переводится на английский (см.
_translate_query_for_search) - Pexels/Openverse проиндексированы в основном
по англоязычным подписям, и поиск по русскому названию часто вообще не
находил совпадений.

ВАЖНО - проверка релевантности (см. баг-репорт пользователя: для «Куриных
сердечек в сметане» находились сырники, для «Чили кон тыква» - фото
рассыпанных специй, для нескольких разных супов - одна и та же фотография
борща): поиск по ключевым словам у обоих источников иногда находит
формально похожее, но по сути случайное фото. Поэтому КАЖДЫЙ найденный
кандидат перед принятием скачивается и проверяется через Gemini Vision
(_photo_matches_dish) - действительно ли на нём изображено именно это
блюдо. Берётся первый кандидат (из нескольких на запрос, по очереди у
каждого источника), который подтверждён.

Если Gemini Vision недоступен (нет ключа, рейтлимит и т.п.) - это НЕ
считается отклонением кандидата: используется первый найденный кандидат
без подтверждения (см. баг-репорт: при массовом рейтлимите 429 старая
версия рисовала ИИ-картинку для КАЖДОГО рецепта подряд, потому что
недоступность проверки трактовалась как "фото не подошло" - это
превращало временный сбой одного провайдера в полный откат от "настоящих
фото" к "рисуем всё подряд"). ИИ-иллюстрация рисуется только тогда, когда
источники вообще не нашли ни одного кандидата. Такое фото помечается
photo_source="ai_generated" (в отличие от "web_search" - найденное
настоящее фото, подтверждённое или нет), чтобы --replace-all мог впоследствии попробовать найти
для него настоящее фото ещё раз, когда результаты поиска станут лучше.
"""
from __future__ import annotations

import base64
import logging
import urllib.parse

import requests

from backend.ai_recipe import _call_with_fallback
from backend.config import GEMINI_API_KEY, GOOGLE_SEARCH_API_KEY, GOOGLE_SEARCH_CX, PEXELS_API_KEY, PROXY_URL

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15
# Сколько кандидатов запрашивать у каждого источника - чтобы было из чего
# выбирать, если первый результат не подтвердится проверкой релевантности.
CANDIDATES_PER_SOURCE = 5


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def _search_google_images(query: str) -> list[str]:
    if not (GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX):
        return []
    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_SEARCH_CX,
        "q": query,
        "searchType": "image",
        "num": CANDIDATES_PER_SOURCE,
        "safe": "active",
    }
    for use_proxy in (False, True):
        try:
            response = requests.get(
                "https://www.googleapis.com/customsearch/v1", params=params,
                timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy),
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            return [item["link"] for item in items if item.get("link")]
        except Exception as e:
            logger.warning("Поиск фото «%s» через Google Images не удался (прокси=%s): %s", query, use_proxy, e)
    return []


def _search_pexels(query: str) -> list[str]:
    if not PEXELS_API_KEY:
        return []
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": CANDIDATES_PER_SOURCE, "orientation": "landscape"}
    for use_proxy in (False, True):
        try:
            response = requests.get(
                "https://api.pexels.com/v1/search", headers=headers, params=params,
                timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy),
            )
            response.raise_for_status()
            photos = response.json().get("photos", [])
            # "large" - хорошее разрешение под карточку рецепта, не оригинал
            # в полном размере (экономим трафик/место).
            return [p["src"]["large"] for p in photos if p.get("src", {}).get("large")]
        except Exception as e:
            logger.warning("Поиск фото «%s» через Pexels не удался (прокси=%s): %s", query, use_proxy, e)
    return []


def _search_openverse(query: str) -> list[str]:
    params = {
        "q": query, "page_size": CANDIDATES_PER_SOURCE, "license_type": "commercial",
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
            return [r["url"] for r in results if r.get("url")]
        except Exception as e:
            logger.warning("Поиск фото «%s» через Openverse не удался (прокси=%s): %s", query, use_proxy, e)
    return []


def _fetch_image_bytes(url: str) -> bytes | None:
    for use_proxy in (False, True):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy))
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.warning("Не удалось скачать изображение (прокси=%s): %s", use_proxy, e)
    return None


def _translate_query_for_search(dish_name: str, cuisine: str | None, is_drink: bool) -> str:
    """
    Переводит название блюда (и кухню, если есть) на английский перед
    поиском в Pexels/Openverse (см. docstring модуля). Если перевод не
    удался (все ИИ-провайдеры недоступны) - используем название как есть
    (хуже релевантность, но поиск не падает).

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


def _call_gemini_vision(prompt: str, image_bytes: bytes, use_proxy: bool) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    headers = {"X-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ]
        }]
    }
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _photo_matches_dish(
    image_bytes: bytes,
    dish_name: str,
    cuisine: str | None,
    is_drink: bool,
    ingredients: list[str] | None = None,
) -> bool | None:
    """
    Просит Gemini Vision посмотреть на найденную картинку и подтвердить,
    что на ней действительно изображено это блюдо/напиток, а не что-то
    случайное, найденное по формальному совпадению слов в поисковом запросе
    (см. docstring модуля).

    ingredients - состав РЕЦЕПТА (не абстрактного блюда с таким названием) -
    если задан, Gemini дополнительно сверяет, что видно на фото, с составом:
    один и тот же общий "суккоташ" в разных источниках выглядит по-разному
    (где-то с зелёным горошком, где-то без), и нужен снимок именно этого
    рецепта, а не любой картинки с похожим названием (см. баг-репорт: фото
    с преобладающим горошком для рецепта без горошка в составе).

    Возвращает True/False, если Gemini дал ответ, или None, если проверить
    не удалось вообще (нет ключа, лимит исчерпан, сеть недоступна). None -
    это НЕ "нет": вызывающий код (get_dish_photo) при None не отклоняет
    кандидата, а использует его без подтверждения - иначе (см. баг-репорт:
    при массовом рейтлимите Gemini 429 ИИ рисовало картинку для КАЖДОГО
    рецепта подряд, хотя реальные фото были в порядке) недоступность
    проверки превращается в "рисуем всё подряд", а не просто в отсутствие
    лишней подстраховки.
    """
    subject = "напиток" if is_drink else "готовое блюдо"
    cuisine_part = f" ({cuisine} кухня)" if cuisine else ""
    ingredients_part = ""
    if ingredients:
        ingredients_part = (
            f' Состав по рецепту: {", ".join(ingredients[:10])}.'
            f' Учти это при проверке: если на фото явно преобладает ингредиент, '
            f'которого нет в этом списке (например, много зелёного горошка на фото, '
            f'хотя горошка нет в составе) - это несовпадение, мелкие детали '
            f'(специи, украшение, посуда) можно не учитывать.'
        )
    prompt = (
        f'На фотографии должно быть изображено {subject} «{dish_name}»{cuisine_part}.{ingredients_part} '
        f'Это действительно оно? Ответь СТРОГО одним словом на русском: "да" или "нет". '
        f'Если на фото не готовое блюдо/напиток (сырые ингредиенты крупным планом, '
        f'специи, посторонний предмет, явно другое блюдо) - отвечай "нет".'
    )
    for use_proxy in (False, True):
        try:
            text = _call_gemini_vision(prompt, image_bytes, use_proxy)
            return text.strip().lower().startswith("да")
        except Exception as e:
            logger.warning(
                "Проверка фото для «%s» через Gemini Vision не удалась (прокси=%s): %s", dish_name, use_proxy, e
            )
    return None


def _generate_ai_photo(search_query: str) -> bytes | None:
    """
    Рисует иллюстрацию через Pollinations.ai (бесплатно, без ключа) -
    запасной вариант на случай, если не нашлось ни одной подтверждённо
    похожей настоящей фотографии (см. docstring модуля). search_query - уже
    переведённый на английский запрос из _translate_query_for_search,
    переиспользуем его вместо повторного перевода.
    """
    prompt = f"professional food photography of {search_query}, appetizing, realistic, high quality"
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=800&height=600&nologo=true"
    return _fetch_image_bytes(url)


def get_dish_photo(
    dish_name: str,
    cuisine: str | None = None,
    category: str | None = None,
    ingredients: list[str] | None = None,
) -> tuple[bytes | None, str]:
    """
    Главная точка входа для подбора фото рецепта (см. docstring модуля).

    Возвращает (image_bytes, source):
    - source="web_search" - настоящее найденное фото: либо подтверждённое
      Gemini Vision, либо (если проверить не удалось - см.
      _photo_matches_dish) первый найденный кандидат без подтверждения.
    - source="ai_generated" - источники не нашли вообще ни одного
      кандидата - нарисовано вместо этого. image_bytes при этом может быть
      None, только если не получилось вообще ничего - ни найти, ни
      нарисовать (например, сеть недоступна).

    category - название категории рецепта ("Напитки", "Десерты" и т.п., см.
    backend.database.crud.CATEGORY_KEY_TO_NAME) - используется только чтобы
    отличить напитки от остальных блюд при формировании запроса.
    ingredients - состав РЕЦЕПТА (список названий ингредиентов) - передаётся
    в проверку через Gemini Vision, чтобы отклонять фото с явно другим
    составом (см. _photo_matches_dish). Необязателен.
    """
    is_drink = category == "Напитки"
    query = _translate_query_for_search(dish_name, cuisine, is_drink)

    first_candidate_bytes: bytes | None = None
    gemini_unavailable = False

    for search_fn in (_search_google_images, _search_pexels, _search_openverse):
        if gemini_unavailable and first_candidate_bytes is not None:
            break
        for candidate_url in search_fn(query):
            image_bytes = _fetch_image_bytes(candidate_url)
            if image_bytes is None:
                continue
            if first_candidate_bytes is None:
                first_candidate_bytes = image_bytes
            if gemini_unavailable:
                # Gemini уже недоступен в этом запуске (см. ниже) - нет
                # смысла тратить попытки на остальных кандидатов, первый
                # найденный и так пойдёт в дело как неподтверждённый.
                break
            verdict = _photo_matches_dish(image_bytes, dish_name, cuisine, is_drink, ingredients)
            if verdict is True:
                return image_bytes, "web_search"
            if verdict is None:
                gemini_unavailable = True
                break

    if first_candidate_bytes is not None:
        logger.info(
            "Настоящее фото для «%s» найдено, но не подтверждено (Gemini Vision недоступен) - "
            "использую его как есть", dish_name,
        )
        return first_candidate_bytes, "web_search"

    logger.info("Ни одного фото для «%s» не нашлось ни в одном источнике - рисую ИИ-иллюстрацию взамен", dish_name)
    return _generate_ai_photo(query), "ai_generated"


def save_photo_bytes(recipe_id: int, image_bytes: bytes) -> str:
    """
    Сохраняет уже полученные (скачанные и, для настоящих фото, проверенные)
    байты картинки в PHOTOS_DIR как recipe_{id}.jpg.
    """
    from backend.config import PHOTOS_DIR  # локальный импорт - избегаем цикла

    filename = f"recipe_{recipe_id}.jpg"
    (PHOTOS_DIR / filename).write_bytes(image_bytes)
    return filename
