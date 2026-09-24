"""
Добавляет в существующую таблицу recipes колонки атрибуции фото
(photo_credit_*, см. models.Recipe). create_all() новые колонки в уже
созданную таблицу не добавляет, поэтому делаем ALTER TABLE сами.

Безопасно запускать сколько угодно раз - существующие колонки пропускаются.
Вызывается автоматически при старте бэкенда (main.py, lifespan), но перед
первым перезапуском после обновления лучше выполнить вручную, чтобы бот,
который тоже читает таблицу recipes, не стартовал раньше миграции:

    python -m backend.database.photo_credit_migration
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from backend.database.db import async_session

logger = logging.getLogger(__name__)

_COLUMNS = {
    "photo_credit_provider": "VARCHAR(20)",
    "photo_credit_author": "VARCHAR(200)",
    "photo_credit_author_url": "VARCHAR(500)",
    "photo_credit_page_url": "VARCHAR(500)",
    "photo_credit_license": "VARCHAR(50)",
}


async def ensure_photo_credit_columns() -> list[str]:
    added: list[str] = []
    async with async_session() as session:
        rows = await session.execute(text("PRAGMA table_info(recipes)"))
        existing = {row[1] for row in rows}
        for name, sql_type in _COLUMNS.items():
            if name not in existing:
                await session.execute(text(f"ALTER TABLE recipes ADD COLUMN {name} {sql_type}"))
                added.append(name)
        await session.commit()
    if added:
        logger.info("Добавлены колонки в recipes: %s", ", ".join(added))
    return added


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = asyncio.run(ensure_photo_credit_columns())
    print("Добавлено колонок:", len(result), result or "")
