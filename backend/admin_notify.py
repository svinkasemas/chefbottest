"""
Уведомления админам о рецептах, которые добавили обычные пользователи
(генерация по названию в Mini App, импорт по ссылке в боте).

Такие рецепты сразу видны всем, поэтому админ получает сообщение с кнопкой
«🚫 Скрыть» - нажатие обрабатывает бот (callback hide_recipe:<id> в
bot/bot.py) и скрывает рецепт так же, как /delete_recipe.

Бэкенд - отдельный процесс без aiogram, поэтому сообщение отправляется
напрямую через HTTP API Telegram. С сервера api.telegram.org доступен только
через прокси, поэтому сначала пробуем через PROXY_URL, потом напрямую.
Ошибка отправки не должна ломать сам запрос пользователя - только логируется.
"""
from __future__ import annotations

import html
import json
import logging

import requests

from backend.config import ADMIN_IDS, BOT_TOKEN, PROXY_URL

logger = logging.getLogger("admin_notify")


def moderation_keyboard(recipe_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "🚫 Скрыть", "callback_data": f"hide_recipe:{recipe_id}"}]]}


def moderation_text(recipe_id: int, recipe_name: str, who: str, how: str, source_url: str | None) -> str:
    text = (
        f"🆕 Новый рецепт от пользователя\n"
        f"#{recipe_id} «{html.escape(recipe_name)}»\n"
        f"Кто: {html.escape(who)}\n"
        f"Как: {html.escape(how)}"
    )
    if source_url:
        text += f"\nИсточник: {html.escape(source_url)}"
    return text


def notify_admins_new_recipe(recipe_id: int, recipe_name: str, who: str, how: str, source_url: str | None) -> None:
    """Синхронная функция - вызывать через asyncio.to_thread."""
    if not ADMIN_IDS or not BOT_TOKEN:
        return
    payload_base = {
        "text": moderation_text(recipe_id, recipe_name, who, how, source_url),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": json.dumps(moderation_keyboard(recipe_id)),
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    routes = [True, False] if PROXY_URL else [False]
    for admin_id in ADMIN_IDS:
        for use_proxy in routes:
            proxies = {"http": PROXY_URL, "https": PROXY_URL} if use_proxy else None
            try:
                response = requests.post(url, data={**payload_base, "chat_id": admin_id}, timeout=15, proxies=proxies)
                response.raise_for_status()
                break
            except Exception as e:
                # Текст исключения requests содержит URL с токеном бота - не логируем его.
                logger.warning("Не удалось уведомить админа %s о рецепте #%d (прокси=%s): %s",
                               admin_id, recipe_id, use_proxy, type(e).__name__)
