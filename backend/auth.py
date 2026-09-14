"""
Проверка подлинности данных, которые Telegram передаёт мини-приложению
(Telegram.WebApp.initData), согласно официальному алгоритму:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Коротко: initData — это query-строка с полями пользователя и полем hash.
Мы пересчитываем HMAC-SHA256 по остальным полям с секретным ключом,
выведенным из токена бота, и сравниваем с присланным hash. Если не совпало —
запрос не от настоящего Telegram, отклоняем.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from backend.config import ADMIN_IDS, BOT_TOKEN, DEV_MODE

MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60  # сутки


class TelegramUser:
    def __init__(self, telegram_id: int, username: str | None, full_name: str | None):
        self.telegram_id = telegram_id
        self.username = username
        self.full_name = full_name
        self.is_admin = telegram_id in ADMIN_IDS


def _validate_init_data(init_data: str) -> dict:
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Отсутствует hash в initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("Неверная подпись initData")

    auth_date = int(parsed.get("auth_date", 0))
    if auth_date and (time.time() - auth_date) > MAX_INIT_DATA_AGE_SECONDS:
        raise ValueError("initData устарела")

    return parsed


async def get_current_user(x_telegram_init_data: str | None = Header(default=None)) -> TelegramUser:
    """
    FastAPI-зависимость: достаёт и проверяет пользователя из заголовка
    X-Telegram-Init-Data, который фронтенд берёт из Telegram.WebApp.initData.
    """
    if DEV_MODE and not x_telegram_init_data:
        # Локальная разработка вне Telegram (открыли index.html в браузере)
        return TelegramUser(telegram_id=1, username="dev", full_name="Dev User")

    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Нет данных авторизации Telegram")

    try:
        parsed = _validate_init_data(x_telegram_init_data)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user_json = parsed.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="В initData нет данных пользователя")

    user_data = json.loads(user_json)
    full_name = " ".join(filter(None, [user_data.get("first_name"), user_data.get("last_name")]))
    return TelegramUser(
        telegram_id=user_data["id"],
        username=user_data.get("username"),
        full_name=full_name or None,
    )
