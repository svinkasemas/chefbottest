"""
Подключение к базе данных: асинхронный движок + фабрика сессий.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import DATABASE_URL
from backend.database.models import Base

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _register_unicode_lower(dbapi_connection, connection_record):
    """
    Встроенная LOWER() в SQLite регистронезависима только для ASCII, из-за
    чего поиск по кириллице ('Борщ' -> 'борщ') работает некорректно.
    Подменяем на Python-реализацию с полноценной поддержкой Unicode.
    """
    dbapi_connection.create_function("lower", 1, lambda x: x.lower() if x is not None else None)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    """FastAPI-зависимость: одна сессия на запрос."""
    async with async_session() as session:
        yield session
