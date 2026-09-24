"""
Подбор фото для рецептов: ищем настоящую, лицензионно чистую фотографию
блюда в свободных источниках и проверяем её через Gemini Vision.

Реальное фото ищется по очереди в:
1. Unsplash (нужен UNSPLASH_ACCESS_KEY, иначе источник пропускается).
2. Pexels (нужен PEXELS_API_KEY, иначе источник пропускается).
3. Openverse (без ключа).

(Google Custom Search закрыт для новых клиентов - _search_google_images
оставлена в файле, но в поиске не используется.)

Название блюда перед поиском переводится на английский
(_translate_query_for_search). Кухню в запрос НЕ добавляем: модель
превращала её в «Russian mayonnaise salad plate», «Chinese kombucha drink»
и т.п., и поиск уходил не туда.

Проверка релевантности: каждый кандидат скачивается и проверяется через
Gemini Vision (_photo_matches_dish). Возможные исходы:
- подтверждён -> берём его;
- отклонён -> НЕ используем ни при каких условиях (раньше здесь был баг:
  если Gemini отклонил всех кандидатов, первый из них всё равно
  возвращался как «неподтверждённый»);
- проверить не удалось (квота/сеть) -> кандидат считается
  неподтверждённым. Брать ли такие, решает вызывающий код
  (allow_unverified).

ИИ-иллюстрация (Pollinations) рисуется только если allow_ai=True и
источники не нашли вообще ни одного кандидата.

Сеть. С сервера часть сервисов напрямую недоступна (гео-блок/таймауты), а
через прокси работает. Чтобы не тратить 15-30 секунд на заведомо
провальный прямой запрос для каждого рецепта, модуль запоминает хосты, где
прямой запрос несколько раз подряд не удался, и какое-то время ходит к ним
сразу через прокси (_route_order).

Gemini Vision. Если Gemini несколько раз подряд отвечает 429 (квота ключа,
прокси тут не поможет), проверка отключается на время
(_GEMINI_DISABLE_SECONDS) - кандидаты сразу считаются неподтверждёнными,
без лишних запросов и пауз.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass

import requests

from backend.ai_recipe import _call_with_fallback
from backend.config import (
    GEMINI_API_KEY,
    GOOGLE_SEARCH_API_KEY,
    GOOGLE_SEARCH_CX,
    PEXELS_API_KEY,
    PROXY_URL,
    UNSPLASH_ACCESS_KEY,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15
CANDIDATES_PER_SOURCE = 5

# --- Маршрутизация: напрямую или через прокси -------------------------------

# Сколько прямых неудач подряд для хоста, после которых начинаем ходить к нему
# сразу через прокси, и на сколько (процесс бэкенда живёт долго - прямой
# доступ может со временем восстановиться).
_DIRECT_FAILS_BEFORE_PROXY_FIRST = 2
_PROXY_FIRST_SECONDS = 30 * 60

# HTTP-коды, при которых повтор через прокси бессмысленен: ошибка в запросе,
# ключе или квоте ключа, а не в сети/гео-блоке.
_NO_RETRY_STATUSES = {400, 401, 429}

_route_lock = threading.Lock()
_direct_fail_counts: dict[str, int] = {}
_proxy_first_until: dict[str, float] = {}


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""


def _route_order(url: str) -> tuple[bool, ...]:
    if not PROXY_URL:
        return (False,)
    host = _host(url)
    with _route_lock:
        if _proxy_first_until.get(host, 0) > time.monotonic():
            return (True, False)
    return (False, True)


def _note_direct_result(url: str, ok: bool) -> None:
    host = _host(url)
    with _route_lock:
        if ok:
            _direct_fail_counts[host] = 0
            return
        fails = _direct_fail_counts.get(host, 0) + 1
        if fails >= _DIRECT_FAILS_BEFORE_PROXY_FIRST:
            _direct_fail_counts[host] = 0
            _proxy_first_until[host] = time.monotonic() + _PROXY_FIRST_SECONDS
            logger.info("%s напрямую не отвечает - дальше хожу к нему сначала через прокси", host)
        else:
            _direct_fail_counts[host] = fails


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def _status_of(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _http(method: str, url: str, label: str, **kwargs) -> requests.Response:
    """
    Запрос с учётом маршрутизации (_route_order). Поднимает последнее
    исключение, если не удался ни один маршрут.
    """
    last_exc: Exception | None = None
    for use_proxy in _route_order(url):
        try:
            response = requests.request(
                method, url, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy), **kwargs
            )
            response.raise_for_status()
            if not use_proxy:
                _note_direct_result(url, ok=True)
            return response
        except Exception as e:
            last_exc = e
            status = _status_of(e)
            logger.warning("%s не удалось (прокси=%s): %s", label, use_proxy, e)
            if status in _NO_RETRY_STATUSES:
                break
            if not use_proxy:
                _note_direct_result(url, ok=False)
    assert last_exc is not None
    raise last_exc


# --- Gemini: общий лимит и автоотключение проверки --------------------------

_GEMINI_MIN_INTERVAL_SECONDS = 4.5
_GEMINI_429_BEFORE_DISABLE = 3
_GEMINI_DISABLE_SECONDS = 60 * 60

_gemini_rate_lock = threading.Lock()
_gemini_last_call_at = 0.0
_gemini_429_streak = 0
_gemini_disabled_until = 0.0


def _gemini_disabled() -> bool:
    return _gemini_disabled_until > time.monotonic()


def _throttle_gemini() -> None:
    global _gemini_last_call_at
    if _gemini_disabled():
        # Gemini всё равно отвечает 429 - паузы ради него не нужны.
        return
    with _gemini_rate_lock:
        wait = _gemini_last_call_at + _GEMINI_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _gemini_last_call_at = time.monotonic()


def _note_gemini_result(status: int | None) -> None:
    global _gemini_429_streak, _gemini_disabled_until
    with _gemini_rate_lock:
        if status == 429:
            _gemini_429_streak += 1
            if _gemini_429_streak >= _GEMINI_429_BEFORE_DISABLE:
                _gemini_429_streak = 0
                _gemini_disabled_until = time.monotonic() + _GEMINI_DISABLE_SECONDS
                logger.warning(
                    "Gemini %d раз подряд ответил 429 (квота ключа) - отключаю проверку фото на %d мин",
                    _GEMINI_429_BEFORE_DISABLE, _GEMINI_DISABLE_SECONDS // 60,
                )
        elif status is None or status < 400:
            _gemini_429_streak = 0


# --- Поиск кандидатов -------------------------------------------------------

def _search_google_images(query: str) -> list[str]:
    if not (GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX):
        return []
    params = {
        "key": GOOGLE_SEARCH_API_KEY, "cx": GOOGLE_SEARCH_CX, "q": query,
        "searchType": "image", "num": CANDIDATES_PER_SOURCE, "safe": "active",
    }
    try:
        response = _http("GET", "https://www.googleapis.com/customsearch/v1",
                         f"Поиск фото «{query}» через Google Images", params=params)
        return [item["link"] for item in response.json().get("items", []) if item.get("link")]
    except Exception:
        return []


def _search_unsplash(query: str) -> list[str]:
    if not UNSPLASH_ACCESS_KEY:
        return []
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {"query": query, "per_page": CANDIDATES_PER_SOURCE, "orientation": "landscape"}
    try:
        response = _http("GET", "https://api.unsplash.com/search/photos",
                         f"Поиск фото «{query}» через Unsplash", headers=headers, params=params)
        results = response.json().get("results", [])
        return [r["urls"]["regular"] for r in results if r.get("urls", {}).get("regular")]
    except Exception:
        return []


def _search_pexels(query: str) -> list[str]:
    if not PEXELS_API_KEY:
        return []
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": CANDIDATES_PER_SOURCE, "orientation": "landscape"}
    try:
        response = _http("GET", "https://api.pexels.com/v1/search",
                         f"Поиск фото «{query}» через Pexels", headers=headers, params=params)
        photos = response.json().get("photos", [])
        return [p["src"]["large"] for p in photos if p.get("src", {}).get("large")]
    except Exception:
        return []


def _search_openverse(query: str) -> list[str]:
    params = {
        "q": query, "page_size": CANDIDATES_PER_SOURCE, "license_type": "commercial",
        "category": "photograph", "mature": "false",
    }
    try:
        response = _http("GET", "https://api.openverse.org/v1/images/",
                         f"Поиск фото «{query}» через Openverse", params=params)
        return [r["url"] for r in response.json().get("results", []) if r.get("url")]
    except Exception:
        return []


_PROVIDERS = (
    ("unsplash", _search_unsplash),
    ("pexels", _search_pexels),
    ("openverse", _search_openverse),
)


def _fetch_image_bytes(url: str) -> bytes | None:
    try:
        return _http("GET", url, "Скачивание изображения").content
    except Exception:
        return None


def _image_mime(data: bytes) -> str | None:
    """MIME по сигнатуре файла; None - это не картинка (HTML-заглушка и т.п.)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def photo_hash(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()


# --- Перевод запроса --------------------------------------------------------

def _translate_query_for_search(dish_name: str, category: str | None, is_drink: bool) -> str:
    """
    Переводит название на английский и делает из него короткий запрос для
    фотобанка. Кухню сознательно не передаём (см. docstring модуля);
    категория передаётся как подсказка (соус это или напиток).
    """
    subject = "напитка (коктейля/чая/лимонада и т.п.)" if is_drink else "блюда"
    category_part = f", категория рецепта «{category}»" if category else ""
    prompt = (
        f"Название {subject}: «{dish_name}»{category_part}.\n\n"
        f"Переведи название на английский и сделай из него запрос для поиска "
        f"ФОТОГРАФИИ в стоковом фотобанке (Unsplash/Pexels) - 2-5 английских "
        f"слов, по которым найдётся именно это {'напиток' if is_drink else 'блюдо'}. "
        f"Правила: не добавляй страну или национальность кухни (Russian, Italian, "
        f"Chinese и т.п.), если она не входит в само общепринятое название "
        f"(как в «French toast»). Для соуса пиши, что это соус (например "
        f"«mayonnaise sauce in bowl»), чтобы не найти блюдо, которое им заправлено. "
        f"Если название авторское и непереводимое, опиши, что это по сути "
        f"(например «creamy garlic sauce»). "
        f'Ответь СТРОГО одним JSON-объектом без markdown-разметки: '
        f'{{"query": "english search phrase"}}'
    )
    try:
        _throttle_gemini()
        data = _call_with_fallback(prompt, error_subject=f"перевод названия «{dish_name}» для поиска фото")
        query = str(data.get("query", "")).strip()
        if query:
            return query
    except Exception as e:
        logger.warning("Не удалось перевести «%s» для поиска фото, ищу как есть: %s", dish_name, e)
    return f"{dish_name} {'drink' if is_drink else 'food'}"


# --- Проверка через Gemini Vision -------------------------------------------

def _call_gemini_vision(prompt: str, image_bytes: bytes, mime: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан")
    _throttle_gemini()
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    headers = {"X-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode("ascii")}},
            ]
        }]
    }
    try:
        response = _http("POST", url, "Проверка фото через Gemini Vision", json=payload, headers=headers)
    except Exception as e:
        _note_gemini_result(_status_of(e))
        raise
    _note_gemini_result(response.status_code)
    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


def _photo_matches_dish(
    image_bytes: bytes,
    mime: str,
    dish_name: str,
    cuisine: str | None,
    is_drink: bool,
    ingredients: list[str] | None = None,
) -> bool | None:
    """
    True/False - ответ Gemini; None - проверить не удалось (нет ключа,
    квота, сеть, проверка временно отключена). None - это НЕ «нет».
    """
    if not GEMINI_API_KEY or _gemini_disabled():
        return None
    subject = "напиток" if is_drink else "готовое блюдо"
    cuisine_part = f" ({cuisine} кухня)" if cuisine else ""
    ingredients_part = ""
    if ingredients:
        ingredients_part = (
            f' Состав по рецепту: {", ".join(ingredients[:10])}.'
            f" Учти это при проверке: если на фото явно преобладает ингредиент, "
            f"которого нет в этом списке (например, много зелёного горошка на фото, "
            f"хотя горошка нет в составе) - это несовпадение, мелкие детали "
            f"(специи, украшение, посуда) можно не учитывать."
        )
    prompt = (
        f"На фотографии должно быть изображено {subject} «{dish_name}»{cuisine_part}.{ingredients_part} "
        f'Это действительно оно? Ответь СТРОГО одним словом на русском: "да" или "нет". '
        f"Если на фото не готовое блюдо/напиток (сырые ингредиенты крупным планом, "
        f'специи, посторонний предмет, явно другое блюдо) - отвечай "нет".'
    )
    try:
        text = _call_gemini_vision(prompt, image_bytes, mime)
    except Exception:
        return None
    return text.strip().lower().startswith("да")


# --- ИИ-иллюстрация ---------------------------------------------------------

def _generate_ai_photo(search_query: str) -> bytes | None:
    prompt = f"professional food photography of {search_query}, appetizing, realistic, high quality"
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=800&height=600&nologo=true"
    return _fetch_image_bytes(url)


# --- Главная точка входа ----------------------------------------------------

@dataclass
class PhotoResult:
    image_bytes: bytes | None
    # "web_search" - настоящее фото; "ai_generated" - нарисовано;
    # None - ничего не взято (текущее фото рецепта не трогать).
    source: str | None
    provider: str | None = None     # unsplash / pexels / openverse / pollinations
    verified: bool = False          # подтверждено Gemini Vision
    query: str = ""
    reason: str = ""                # почему ничего не взято (для лога/отчёта)


def find_dish_photo(
    dish_name: str,
    cuisine: str | None = None,
    category: str | None = None,
    ingredients: list[str] | None = None,
    *,
    allow_unverified: bool = True,
    allow_ai: bool = True,
    exclude_hashes: set[str] | None = None,
) -> PhotoResult:
    """
    allow_unverified - брать ли кандидата, которого не удалось проверить
    (Gemini недоступен). Отклонённые Gemini кандидаты не берутся никогда.
    allow_ai - рисовать ли иллюстрацию, если кандидатов не нашлось вообще.
    exclude_hashes - хэши картинок, уже назначенных другим рецептам (чтобы
    «Соус для стейка» и «Соус грейви» не получили одно и то же фото).
    """
    is_drink = category == "Напитки"
    query = _translate_query_for_search(dish_name, category, is_drink)
    logger.info("  запрос для поиска: «%s»", query)

    unverified: tuple[bytes, str] | None = None
    any_candidate = False
    rejected = 0
    duplicates = 0
    vision_down = False

    for provider, search_fn in _PROVIDERS:
        if vision_down and unverified is not None:
            break
        for candidate_url in search_fn(query):
            image_bytes = _fetch_image_bytes(candidate_url)
            if image_bytes is None:
                continue
            mime = _image_mime(image_bytes)
            if mime is None:
                continue
            if exclude_hashes and photo_hash(image_bytes) in exclude_hashes:
                duplicates += 1
                logger.info("  кандидат из %s уже назначен другому рецепту - пропускаю", provider)
                continue
            any_candidate = True

            verdict = None if vision_down else _photo_matches_dish(
                image_bytes, mime, dish_name, cuisine, is_drink, ingredients
            )
            if verdict is True:
                return PhotoResult(image_bytes, "web_search", provider, True, query)
            if verdict is False:
                rejected += 1
                logger.info("  кандидат из %s отклонён проверкой Gemini", provider)
                continue
            # Проверить не удалось - дальше проверять других бессмысленно.
            vision_down = True
            if unverified is None:
                unverified = (image_bytes, provider)
            break

    if unverified is not None:
        image_bytes, provider = unverified
        if allow_unverified:
            logger.info("  фото из %s не подтверждено (Gemini недоступен) - беру как есть", provider)
            return PhotoResult(image_bytes, "web_search", provider, False, query)
        logger.info("  фото из %s найдено, но не подтверждено - пропускаю, текущее фото остаётся", provider)
        return PhotoResult(None, None, provider, False, query, reason="не подтверждено")

    if any_candidate:
        logger.info("  все %d кандидатов отклонены проверкой - текущее фото остаётся", rejected)
        return PhotoResult(None, None, None, False, query, reason=f"отклонено кандидатов: {rejected}")

    if duplicates:
        return PhotoResult(None, None, None, False, query, reason="только дубли уже назначенных фото")

    if allow_ai:
        logger.info("  ни одного фото не нашлось - рисую ИИ-иллюстрацию")
        image_bytes = _generate_ai_photo(query)
        if image_bytes is not None:
            return PhotoResult(image_bytes, "ai_generated", "pollinations", False, query)
        return PhotoResult(None, None, None, False, query, reason="не нашлось и не нарисовалось")

    return PhotoResult(None, None, None, False, query, reason="ничего не нашлось")


def get_dish_photo(
    dish_name: str,
    cuisine: str | None = None,
    category: str | None = None,
    ingredients: list[str] | None = None,
) -> tuple[bytes | None, str]:
    """Старый интерфейс (bytes, source) - для остального кода проекта."""
    result = find_dish_photo(dish_name, cuisine, category, ingredients)
    return result.image_bytes, result.source or "ai_generated"


def save_photo_bytes(recipe_id: int, image_bytes: bytes) -> str:
    from backend.config import PHOTOS_DIR  # локальный импорт - избегаем цикла

    filename = f"recipe_{recipe_id}.jpg"
    (PHOTOS_DIR / filename).write_bytes(image_bytes)
    return filename
