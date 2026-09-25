"""
Простое ограничение частоты действий на пользователя (в памяти процесса).

Нужно для дорогих действий, доступных любому пользователю: генерация рецепта
по названию (POST /api/recipes/generate) и импорт по ссылке в боте. Каждое
такое действие тратит общие квоты Tavily/Gemini/Groq и добавляет рецепт в
общую базу, поэтому без лимита один человек мог бы скриптом залить сотни
рецептов и исчерпать квоты для всех.

Бэкенд и бот - разные процессы, у каждого свой счётчик; после перезапуска
счётчики обнуляются. Для проекта такого размера этого достаточно.
Админы (ADMIN_IDS) лимитом не ограничиваются.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from backend.config import ADMIN_IDS


class RateLimiter:
    def __init__(self, limits: list[tuple[int, int]]):
        """limits - список (сколько_раз, за_сколько_секунд), например [(5, 3600), (20, 86400)]."""
        self.limits = sorted(limits, key=lambda x: x[1])
        self._window = max(seconds for _, seconds in limits)
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, telegram_id: int) -> int | None:
        """
        Регистрирует попытку. Возвращает None, если действие разрешено, или
        через сколько секунд можно повторить, если лимит исчерпан (в этом
        случае попытка не засчитывается).
        """
        if telegram_id in ADMIN_IDS:
            return None
        now = time.time()
        with self._lock:
            events = self._events[telegram_id]
            while events and now - events[0] > self._window:
                events.popleft()
            for count, seconds in self.limits:
                recent = [t for t in events if now - t <= seconds]
                if len(recent) >= count:
                    return int(seconds - (now - recent[0])) + 1
            events.append(now)
            return None


def format_wait(seconds: int) -> str:
    if seconds < 90:
        return "минуту"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} мин"
    return f"{round(minutes / 60)} ч"


# Добавление новых рецептов (генерация по названию и импорт по ссылке):
# не больше 5 в час и 20 в сутки на пользователя.
NEW_RECIPE_LIMITS = [(5, 3600), (20, 86400)]
