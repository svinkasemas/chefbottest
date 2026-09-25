"""
Импорт рецепта по ссылке на внешний кулинарный сайт (povarenok.ru,
russianfood.com и, в принципе, любой другой сайт с рецептом на странице).

Подход: скачиваем HTML страницы, вычищаем его до читаемого текста (без тегов,
скриптов, меню), и отдаём этот текст ИИ с просьбой извлечь из него рецепт по
нашей стандартной схеме (backend/ai_recipe.RECIPE_SCHEMA_INSTRUCTIONS) - но,
в отличие от обычной генерации "с нуля", здесь ИИ не придумывает рецепт, а
переносит реальные ингредиенты и шаги со страницы. Это работает на любом
сайте с текстовым рецептом, а не только на тех двух, что перечислены выше.

Отдельно пытаемся вытащить картинку блюда со страницы (og:image) - но её
использует только ручная админская команда /import в bot.py (помечает как
photo_source="source_page"). Автоматические потоки (импорт через поиск,
ежедневный сборщик рецептов) эту картинку не берут - у страницы-источника
могут быть права на своё фото, поэтому для них фото подбирается отдельно из
свободных источников, см. backend/photo_search.py.

Тот же принцип отказоустойчивости, что и везде в проекте: прямое соединение,
при неудаче - через PROXY_URL из .env (тот же прокси, что нужен для доступа к
Telegram и к Gemini/Groq с этого сервера).
"""
from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import urljoin, urlparse

import requests

from backend.ai_recipe import RECIPE_SCHEMA_INSTRUCTIONS, RecipeGenerationError, _call_with_fallback
from backend.config import PROXY_URL, TAVILY_API_KEY
from backend.net_safety import DownloadError, UnsafeUrlError, looks_like_image, safe_get

logger = logging.getLogger("recipe_import")

REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGE_TEXT_CHARS = 9000
# Ограничения на размер скачиваемого (защита от «бесконечных» файлов,
# которыми можно положить сервер по памяти).
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Некоторые сайты (например povarenok.ru) блокируют запросы без обычного
# браузерного User-Agent как заведомо ботов.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


class RecipeImportError(Exception):
    pass


# Украинские сайты не используются как источники рецептов - ни при поиске
# (ежедневный сборщик, «Не нашли блюдо?» в Mini App), ни при импорте по
# ссылке в боте. Все сайты в зоне .ua (включая .com.ua, .kiev.ua и т.п.)
# отсекаются автоматически; украинские сайты на других доменах
# перечисляются вручную в EXCLUDED_SOURCE_DOMAINS (поддомены учитываются).
EXCLUDED_SOURCE_TLDS = (".ua", ".укр")
EXCLUDED_SOURCE_DOMAINS = {
    "klopotenko.com",
    "obozrevatel.com",
    "unian.net",
    "glavred.net",
    "novyny.live",
}


def is_excluded_source(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        host = host.encode("ascii").decode("idna")   # xn--j1amh -> укр
    except Exception:
        pass
    if host.endswith(EXCLUDED_SOURCE_TLDS):
        return True
    return any(host == d or host.endswith("." + d) for d in EXCLUDED_SOURCE_DOMAINS)


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def fetch_page_html(url: str) -> tuple[str, str]:
    """
    Скачивает страницу с проверками из backend/net_safety.py (только
    публичные адреса, ограничение размера, контроль редиректов). Возвращает
    (итоговый_url_после_редиректов, html). Текст ошибки для пользователя -
    общий, без технических подробностей (они пишутся в лог).
    """
    try:
        final_url, response, body = safe_get(
            url, max_bytes=MAX_PAGE_BYTES, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except UnsafeUrlError as e:
        raise RecipeImportError(str(e)) from e
    except DownloadError as e:
        raise RecipeImportError(f"Не удалось открыть страницу: {e}") from e
    encoding = response.encoding
    if not encoding or encoding.lower() == "iso-8859-1":
        # requests ставит ISO-8859-1 по умолчанию, если сайт не указал
        # кодировку - для русских сайтов это почти всегда неверно.
        try:
            import charset_normalizer
            best = charset_normalizer.from_bytes(body).best()
            encoding = best.encoding if best else "utf-8"
        except Exception:
            encoding = "utf-8"
    return final_url, body.decode(encoding, errors="replace")


def extract_og_image(html_text: str, page_url: str) -> str | None:
    for tag_match in re.finditer(r"<meta[^>]+>", html_text, re.IGNORECASE):
        tag = tag_match.group(0)
        if "og:image" not in tag:
            continue
        content_match = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if content_match:
            return urljoin(page_url, content_match.group(1))
    return None


def html_to_text(html_text: str) -> str:
    """
    Грубая, но достаточная для наших целей очистка HTML до читаемого текста:
    вырезает скрипты/стили/меню/подвал, остальные теги заменяет переносами
    строк (чтобы сохранить структуру списков и абзацев для ИИ), затем
    разэкранирует HTML-сущности и схлопывает лишние пробелы.
    """
    text = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|/p|/li|/tr|/div|/h[1-6])\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    return text[:MAX_PAGE_TEXT_CHARS]


IMPORT_INSTRUCTIONS = """Тебе дан текст, вычищенный со страницы кулинарного сайта - он может \
содержать мусор (меню, реклама, комментарии, похожие рецепты) вперемешку с самим рецептом.

ВАЖНО: ты не придумываешь рецепт, а извлекаешь его из данного текста. Название блюда, \
список ингредиентов с их количествами и порядок шагов приготовления должны максимально \
точно соответствовать тому, что написано на странице - ничего не меняй по существу, только \
убери мусор и приведи к нужной структуре. Если в тексте не найден рецепт вообще (не тот сайт, \
страница ошибки и т.п.) - верни поле "not_found": true вместо остальной схемы.

Если каких-то технических полей (калорийность, уровень сложности, ценовая категория) на \
странице нет - оцени их сам разумно исходя из состава и техники приготовления, это не \
противоречит принципу "не придумывай": придумывать нельзя название, ингредиенты и шаги.

""" + RECIPE_SCHEMA_INSTRUCTIONS


def build_import_prompt(url: str, page_text: str) -> str:
    return (
        f"Вот текст страницы {url}:\n\n---\n{page_text}\n---\n\n{IMPORT_INSTRUCTIONS}"
    )


def import_recipe_from_url(url: str) -> tuple[dict, str | None]:
    """
    Возвращает (recipe_dict, image_url). image_url - None, если на странице
    не нашлось og:image (тогда фото для рецепта досоздастся как обычно,
    через ночную ИИ-генерацию по photo_prompt).
    """
    if is_excluded_source(url):
        raise RecipeImportError("Рецепты с украинских сайтов не добавляются.")
    final_url, html_text = fetch_page_html(url)
    if is_excluded_source(final_url):
        # Сайт мог перенаправить на украинский домен.
        raise RecipeImportError("Рецепты с украинских сайтов не добавляются.")
    image_url = extract_og_image(html_text, final_url)
    page_text = html_to_text(html_text)

    if len(page_text) < 200:
        raise RecipeImportError(
            "На странице почти нет текста - похоже, сайт отдал пустую заглушку "
            "или страницу с защитой от ботов, а не сам рецепт."
        )

    prompt = build_import_prompt(url, page_text)
    try:
        data = _call_with_fallback(prompt, error_subject=f"импорт рецепта из {url}")
    except RecipeGenerationError as e:
        # Подробности (какие ИИ-провайдеры и с какими ошибками) - только в
        # лог, пользователю они ни к чему.
        logger.warning("ИИ не смог обработать страницу %s: %s", url, e)
        raise RecipeImportError("Сервис разбора рецептов сейчас недоступен, попробуйте позже.") from e

    if data.get("not_found"):
        raise RecipeImportError("На странице не удалось найти рецепт (не тот сайт или пустая страница).")

    for field in ("name", "ingredients", "steps"):
        if not data.get(field):
            raise RecipeImportError(f"ИИ не смог извлечь поле «{field}» из страницы - похоже, разметка сайта не подошла.")

    return data, image_url


def download_image_bytes(image_url: str) -> bytes:
    """
    Скачивает картинку (og:image страницы-источника) с теми же проверками,
    что и страницу, и убеждается, что это действительно изображение - иначе
    по «картинке» можно было бы сохранить и раздать всем что угодно.
    """
    try:
        _, _, body = safe_get(
            image_url, max_bytes=MAX_IMAGE_BYTES, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except (UnsafeUrlError, DownloadError) as e:
        raise RecipeImportError(f"Не удалось скачать картинку: {e}") from e
    if not looks_like_image(body):
        raise RecipeImportError("По ссылке на картинку пришла не картинка")
    return body


# Сайты, которые не отдают полезный текст рецепта (видео, соцсети) -
# исключаем из поиска, чтобы не тратить попытку импорта впустую.
_SEARCH_EXCLUDED_DOMAINS = [
    "youtube.com", "youtu.be", "pinterest.com", "pinterest.ru",
    "instagram.com", "tiktok.com", "facebook.com", "vk.com",
]


def search_recipe_url(dish_name: str) -> str | None:
    """
    Ищет в интернете страницу с реальным рецептом блюда через Tavily Search
    API - бесплатно 1000 запросов/мес без карты (https://app.tavily.com), а
    без ключа вообще работает ограниченный бесплатный "keyless"-доступ без
    регистрации. Возвращает первый подходящий URL или None, если ничего не
    нашлось - тогда вызывающий код падает обратно на генерацию ИИ "с нуля".
    """
    headers = {"Content-Type": "application/json"}
    if TAVILY_API_KEY:
        headers["Authorization"] = f"Bearer {TAVILY_API_KEY}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"

    payload = {
        "query": f"рецепт {dish_name}",
        "search_depth": "basic",
        # С запасом: часть результатов может отсеяться как украинские сайты.
        "max_results": 8,
        "exclude_domains": _SEARCH_EXCLUDED_DOMAINS + sorted(EXCLUDED_SOURCE_DOMAINS),
    }

    for use_proxy in (False, True):
        try:
            response = requests.post(
                "https://api.tavily.com/search", json=payload, headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy),
            )
            response.raise_for_status()
            for result in response.json().get("results", []):
                result_url = result.get("url")
                if not result_url:
                    continue
                if is_excluded_source(result_url):
                    logger.info("Пропускаю украинский источник для «%s»: %s", dish_name, result_url)
                    continue
                return result_url
            return None
        except Exception as e:
            logger.warning("Поиск рецепта «%s» через Tavily не удался (прокси=%s): %s", dish_name, use_proxy, e)
    return None
