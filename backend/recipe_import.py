"""
Импорт рецепта по ссылке на внешний кулинарный сайт (povarenok.ru,
russianfood.com и, в принципе, любой другой сайт с рецептом на странице).

Подход: скачиваем HTML страницы, вычищаем его до читаемого текста (без тегов,
скриптов, меню), и отдаём этот текст ИИ с просьбой извлечь из него рецепт по
нашей стандартной схеме (backend/ai_recipe.RECIPE_SCHEMA_INSTRUCTIONS) - но,
в отличие от обычной генерации "с нуля", здесь ИИ не придумывает рецепт, а
переносит реальные ингредиенты и шаги со страницы. Это работает на любом
сайте с текстовым рецептом, а не только на тех двух, что перечислены выше.

Отдельно пытаемся вытащить картинку блюда со страницы (og:image) - тогда не
нужно ждать ночной генерации фото Pollinations-ом, фото будет настоящее.

Тот же принцип отказоустойчивости, что и везде в проекте: прямое соединение,
при неудаче - через PROXY_URL из .env (тот же прокси, что нужен для доступа к
Telegram и к Gemini/Groq с этого сервера).
"""
from __future__ import annotations

import html as html_module
import logging
import re
from urllib.parse import urljoin

import requests

from backend.ai_recipe import RECIPE_SCHEMA_INSTRUCTIONS, RecipeGenerationError, _call_with_fallback
from backend.config import PROXY_URL

logger = logging.getLogger("recipe_import")

REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGE_TEXT_CHARS = 9000

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


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def fetch_page_html(url: str) -> str:
    """Прямое соединение, при неудаче - через прокси."""
    errors: list[str] = []
    for use_proxy in (False, True):
        try:
            response = requests.get(
                url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except Exception as e:
            errors.append(f"прокси={use_proxy}: {e}")
    raise RecipeImportError(f"Не удалось скачать страницу {url}: " + "; ".join(errors))


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
    html_text = fetch_page_html(url)
    image_url = extract_og_image(html_text, url)
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
        raise RecipeImportError(str(e)) from e

    if data.get("not_found"):
        raise RecipeImportError("На странице не удалось найти рецепт (не тот сайт или пустая страница).")

    for field in ("name", "ingredients", "steps"):
        if not data.get(field):
            raise RecipeImportError(f"ИИ не смог извлечь поле «{field}» из страницы - похоже, разметка сайта не подошла.")

    return data, image_url


def download_image_bytes(image_url: str) -> bytes:
    errors: list[str] = []
    for use_proxy in (False, True):
        try:
            response = requests.get(
                image_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
            )
            response.raise_for_status()
            return response.content
        except Exception as e:
            errors.append(f"прокси={use_proxy}: {e}")
    raise RecipeImportError(f"Не удалось скачать картинку {image_url}: " + "; ".join(errors))
