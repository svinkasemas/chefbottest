"""
Ищет в логах бота (journalctl) записи об импорте рецепта по ссылке и
пытается сопоставить их с рецептами в базе, у которых ещё не проставлен
source_url - по времени создания (импорт и создание рецепта происходят
практически одновременно). Нужен, чтобы задним числом восстановить
источник для рецептов, добавленных по ссылке до того, как в проекте
появилось поле source_url.

Ничего не меняет в базе - только печатает найденные пары (ссылка + похожий
по времени рецепт) и готовую команду scripts/set_recipe_source.py для
каждой, чтобы вы могли проверить глазами и подтвердить перед сохранением
(автоматическое сопоставление по времени не стопроцентно надёжно, особенно
если несколько импортов делались быстро один за другим).

Запуск (из корня проекта, с активированным venv):
    python -m scripts.find_import_sources
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timedelta

from sqlalchemy import select

from backend.database.db import async_session
from backend.database.models import Recipe

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
    r"Запрос «импорт рецепта из (?P<url>\S+)»"
)
# Импорт и создание рецепта в базе происходят практически в одну секунду -
# этого окна с запасом хватает даже с учётом задержек на сеть/ИИ-провайдера.
MATCH_WINDOW = timedelta(minutes=3)


def find_import_log_entries() -> list[tuple[datetime, str]]:
    result = subprocess.run(
        ["journalctl", "-u", "chefbot-bot", "--no-pager", "-o", "cat"],
        capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.splitlines():
        m = LOG_LINE_RE.search(line)
        if m:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            entries.append((ts, m.group("url")))
    return entries


async def main() -> None:
    try:
        entries = find_import_log_entries()
    except FileNotFoundError:
        print("Команда journalctl не найдена - скрипт нужно запускать на сервере.")
        return
    except subprocess.CalledProcessError as e:
        print(f"Не удалось прочитать логи chefbot-bot: {e}")
        return

    if not entries:
        print("В логах chefbot-bot не нашлось записей об импорте рецепта по ссылке.")
        return

    async with async_session() as session:
        candidates = list((await session.execute(
            select(Recipe).where(Recipe.source_url.is_(None)).order_by(Recipe.created_at)
        )).scalars().all())

    print(f"Найдено записей в логах об импорте по ссылке: {len(entries)}\n")

    used_ids: set[int] = set()
    matched = 0
    for ts, url in entries:
        best, best_diff = None, None
        for r in candidates:
            if r.id in used_ids:
                continue
            diff = abs(r.created_at - ts)
            if diff <= MATCH_WINDOW and (best_diff is None or diff < best_diff):
                best, best_diff = r, diff

        if best is not None:
            used_ids.add(best.id)
            matched += 1
            print(f"[{best.id}] {best.name!r}  (создан {best.created_at}, разница с логом {best_diff})")
            print(f"  Ссылка: {url}")
            print(f'  Команда: python -m scripts.set_recipe_source --id {best.id} --url "{url}"\n')
        else:
            print(f"Не нашлось подходящего рецепта без source_url для ссылки (лог в {ts}):")
            print(f"  {url}\n")

    print(f"Итого сопоставлено: {matched} из {len(entries)}.")
    if len(candidates) > matched:
        print(
            f"Рецептов без source_url осталось непроверенных: {len(candidates) - matched} "
            f"(либо не импортировались по ссылке, либо лог за это время уже не хранится)."
        )


if __name__ == "__main__":
    asyncio.run(main())
