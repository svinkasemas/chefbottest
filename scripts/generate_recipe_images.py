"""
Подбор ФОТОГРАФИЙ для рецептов (см. подробности в backend/photo_search.py).

Обычный ежедневный запуск (только рецепты без фото):
    python -m scripts.generate_recipe_images

Разовая замена всех фото (кроме photo_source="source_page"):
    python -m scripts.generate_recipe_images --replace-all

Фильтры и пробные запуски:
    --limit N           только первые N рецептов
    --category "A,B"    только категории, чьё название содержит подстроку
    --dry-run           ничего не сохранять в базу и в папку фото
    --preview-dir DIR   сложить найденные картинки в DIR + index.html,
                        чтобы просмотреть их глазами

Неподтверждённые фото (Gemini Vision недоступен, проверить нечем):
- в ежедневном режиме берутся (у рецепта нет фото совсем - лучше так);
- в режиме --replace-all по умолчанию НЕ берутся: у рецепта уже есть фото,
  менять его на непроверенное смысла нет. Включить: --allow-unverified.
Отклонённые проверкой фото не берутся никогда.

--no-ai - не рисовать ИИ-иллюстрацию, даже если не нашлось ни одного фото.

--ids "18,29"       обработать только рецепты с этими id (например, повторить
                    поиск для тех, чьё фото не подошло)

Применение одобренного предпросмотра (без повторного поиска - в базу попадают
ровно те картинки и авторы, что в папке предпросмотра; см. manifest.json):
    python -m scripts.generate_recipe_images --apply-preview /tmp/photo_preview4 \
        --skip-ids "121,264"

Пример: пробный прогон с просмотром результата
    python -m scripts.generate_recipe_images --replace-all --category "Напитки,Соусы" \\
        --dry-run --allow-unverified --preview-dir /tmp/photo_preview
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from backend.database.db import async_session, init_db
from backend.database.models import Recipe, RecipeIngredient
from backend.database.photo_credit_migration import ensure_photo_credit_columns
from backend.photo_search import find_dish_photo, photo_hash, save_photo_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_recipe_images")

REQUEST_DELAY_SECONDS = 3


async def _recipes_to_process(replace_all: bool) -> list[Recipe]:
    async with async_session() as session:
        query = (
            select(Recipe)
            .where(Recipe.is_active.is_(True))
            .options(
                selectinload(Recipe.category),
                selectinload(Recipe.ingredient_links).selectinload(RecipeIngredient.ingredient),
            )
        )
        if replace_all:
            # is_distinct_from - NULL-безопасное «не равно» (см. историю файла).
            query = query.where(
                or_(Recipe.photo_path.is_(None), Recipe.photo_source.is_distinct_from("source_page"))
            )
        else:
            query = query.where(Recipe.photo_path.is_(None))
        result = await session.execute(query)
        return list(result.scalars().all())


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")[:60]


def _write_preview_index(preview_dir: Path, rows: list[dict]) -> None:
    cards = []
    for row in rows:
        img = (
            f'<img src="{html.escape(row["file"])}" loading="lazy">'
            if row["file"] else '<div class="empty">нет фото</div>'
        )
        cards.append(
            f'<div class="card {row["status"]}">{img}'
            f'<b>#{row["id"]} {html.escape(row["name"])}</b>'
            f'<span>{html.escape(row["category"] or "")} · {row["status"]}'
            f'{" · " + row["provider"] if row["provider"] else ""}</span>'
            f'<i>{html.escape(row["query"])}</i>'
            f'{"<small>Фото: " + html.escape(row["author"]) + "</small>" if row.get("author") else ""}'
            f'{"<em>" + html.escape(row["reason"]) + "</em>" if row["reason"] else ""}</div>'
        )
    page = f"""<!doctype html><meta charset="utf-8"><title>Предпросмотр фото рецептов</title>
<style>
body{{font:14px system-ui,sans-serif;margin:16px;background:#f4f4f4}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.card{{background:#fff;border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:4px;border-top:4px solid #999}}
.card.verified{{border-color:#2a9d4a}}.card.unverified{{border-color:#e0a100}}
.card.ai{{border-color:#7b4ae0}}.card.skipped{{border-color:#c33}}
img,.empty{{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:4px;background:#ddd}}
.empty{{display:flex;align-items:center;justify-content:center;color:#666}}
span{{color:#555}}i{{color:#888;font-size:12px}}em{{color:#c33;font-size:12px}}
</style>
<h2>Предпросмотр: {len(rows)} рецептов</h2>
<p>Зелёный - подтверждено, жёлтый - не подтверждено, фиолетовый - ИИ, красный - пропущено.</p>
<div class="grid">{''.join(cards)}</div>"""
    (preview_dir / "index.html").write_text(page, encoding="utf-8")


def _credit_dict(credit) -> dict | None:
    if credit is None:
        return None
    return {
        "provider": credit.provider, "author": credit.author, "author_url": credit.author_url,
        "page_url": credit.page_url, "license": credit.license,
    }


async def _save_to_db(recipe_id: int, filename: str, source: str | None, credit: dict | None) -> None:
    credit = credit or {}
    async with async_session() as session:
        db_recipe = await session.get(Recipe, recipe_id)
        if db_recipe is None:
            logger.warning("  рецепт id=%d не найден в базе - пропускаю", recipe_id)
            return
        db_recipe.photo_path = filename
        db_recipe.photo_source = source
        db_recipe.photo_credit_provider = credit.get("provider")
        db_recipe.photo_credit_author = credit.get("author")
        db_recipe.photo_credit_author_url = credit.get("author_url")
        db_recipe.photo_credit_page_url = credit.get("page_url")
        db_recipe.photo_credit_license = credit.get("license")
        await session.commit()


async def apply_preview(preview_dir: Path, skip_ids: set[int]) -> None:
    """Записывает в базу картинки из папки предпросмотра по её manifest.json."""
    await init_db()
    await ensure_photo_credit_columns()
    manifest_path = preview_dir / "manifest.json"
    if not manifest_path.exists():
        logger.error("Нет %s - этот предпросмотр сделан старой версией скрипта, "
                     "сделайте новый --dry-run --preview-dir", manifest_path)
        return
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    applied, skipped, missing = 0, 0, 0
    for entry in entries:
        recipe_id = entry["id"]
        if recipe_id in skip_ids or not entry.get("file"):
            skipped += 1
            continue
        image_path = preview_dir / entry["file"]
        if not image_path.exists():
            logger.warning("  #%d: файла %s нет - пропускаю", recipe_id, entry["file"])
            missing += 1
            continue
        filename = await asyncio.to_thread(save_photo_bytes, recipe_id, image_path.read_bytes())
        await _save_to_db(recipe_id, filename, entry.get("source"), entry.get("credit"))
        applied += 1
        logger.info("  #%d %s -> %s", recipe_id, entry.get("name", ""), filename)
    logger.info("Применено %d, пропущено %d, файлов не найдено %d (из %d в manifest.json).",
                applied, skipped, missing, len(entries))


async def main(
    replace_all: bool,
    limit: int | None = None,
    dry_run: bool = False,
    categories: list[str] | None = None,
    allow_unverified: bool | None = None,
    allow_ai: bool = True,
    preview_dir: Path | None = None,
    only_ids: set[int] | None = None,
) -> dict:
    """Возвращает счётчики {"verified", "unverified", "ai", "skipped"} - для сводки админам."""
    await init_db()
    await ensure_photo_credit_columns()
    if allow_unverified is None:
        allow_unverified = not replace_all

    recipes = await _recipes_to_process(replace_all)

    if categories:
        needles = [c.strip().lower() for c in categories if c.strip()]
        recipes = [
            r for r in recipes
            if r.category and any(needle in r.category.name.lower() for needle in needles)
        ]

    if only_ids:
        recipes = [r for r in recipes if r.id in only_ids]

    if not recipes:
        logger.info("Обрабатывать нечего - подходящих рецептов не нашлось (с учётом фильтров).")
        return {"verified": 0, "unverified": 0, "ai": 0, "skipped": 0}

    if limit is not None:
        recipes = recipes[:limit]

    if preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Рецептов для обработки: %d (replace_all=%s, limit=%s, dry_run=%s, allow_unverified=%s, allow_ai=%s)",
        len(recipes), replace_all, limit, dry_run, allow_unverified, allow_ai,
    )

    counts = {"verified": 0, "unverified": 0, "ai": 0, "skipped": 0}
    needs_review: list[str] = []
    used_hashes: set[str] = set()
    preview_rows: list[dict] = []
    manifest: list[dict] = []

    for i, recipe in enumerate(recipes, start=1):
        logger.info("[%d/%d] %s", i, len(recipes), recipe.name)
        category_name = recipe.category.name if recipe.category else None
        ingredient_names = [link.ingredient.name for link in recipe.ingredient_links if link.ingredient]

        result = await asyncio.to_thread(
            find_dish_photo, recipe.name, recipe.cuisine, category_name, ingredient_names,
            allow_unverified=allow_unverified, allow_ai=allow_ai, exclude_hashes=used_hashes,
        )

        if result.image_bytes is None:
            status = "skipped"
            needs_review.append(f"#{recipe.id} {recipe.name} - {result.reason or 'ничего не взято'}")
        elif result.source == "ai_generated":
            status = "ai"
        elif result.verified:
            status = "verified"
        else:
            status = "unverified"
            needs_review.append(f"#{recipe.id} {recipe.name} - не подтверждено ({result.provider})")
        counts[status] += 1

        preview_file = ""
        if result.image_bytes is not None:
            used_hashes.add(photo_hash(result.image_bytes))
            if preview_dir is not None:
                preview_file = f"{recipe.id}_{status}_{_safe_name(recipe.name)}.jpg"
                (preview_dir / preview_file).write_bytes(result.image_bytes)

            if dry_run:
                logger.info("  [dry-run, не сохранено] %s, %s, %d байт",
                            status, result.provider, len(result.image_bytes))
            else:
                filename = await asyncio.to_thread(save_photo_bytes, recipe.id, result.image_bytes)
                await _save_to_db(recipe.id, filename, result.source, _credit_dict(result.credit))
                logger.info("  сохранено (%s, %s): %s", status, result.provider, filename)

        preview_rows.append({
            "id": recipe.id, "name": recipe.name, "category": category_name, "status": status,
            "provider": result.provider or "", "query": result.query, "reason": result.reason,
            "author": (result.credit.author or "") if result.credit else "",
            "file": preview_file,
        })
        manifest.append({
            "id": recipe.id, "name": recipe.name, "file": preview_file, "status": status,
            "source": result.source, "credit": _credit_dict(result.credit),
        })
        if preview_dir is not None:
            # Пишем index.html и manifest.json после каждого рецепта - можно
            # смотреть, не дожидаясь конца (а при обрыве применить то, что есть).
            _write_preview_index(preview_dir, preview_rows)
            (preview_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        if i < len(recipes):
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Готово (из %d): подтверждено %d, не подтверждено %d, ИИ %d, пропущено %d.",
        len(recipes), counts["verified"], counts["unverified"], counts["ai"], counts["skipped"],
    )
    if needs_review:
        logger.info("Стоит проверить вручную (scripts/set_manual_photos.py):\n  %s", "\n  ".join(needs_review))
    if preview_dir is not None:
        logger.info("Предпросмотр: %s", preview_dir / "index.html")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replace-all", action="store_true",
                        help="Заменить все фото, кроме photo_source=source_page")
    parser.add_argument("--limit", type=int, default=None, help="Обработать только первые N рецептов")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не сохранять в базу и в папку фото")
    parser.add_argument("--category", type=str, default=None,
                        help='Только категории, чьё название содержит подстроку, например "Напитки,Соусы"')
    unverified = parser.add_mutually_exclusive_group()
    unverified.add_argument("--allow-unverified", dest="allow_unverified", action="store_true", default=None,
                            help="Брать фото, которые не удалось проверить через Gemini")
    unverified.add_argument("--no-unverified", dest="allow_unverified", action="store_false",
                            help="Не брать непроверенные фото даже в ежедневном режиме")
    parser.add_argument("--no-ai", action="store_true", help="Не рисовать ИИ-иллюстрацию вообще")
    parser.add_argument("--preview-dir", type=Path, default=None,
                        help="Сложить найденные картинки и index.html в эту папку")
    parser.add_argument("--ids", type=str, default=None,
                        help='Обработать только рецепты с этими id, например "121,264"')
    parser.add_argument("--apply-preview", type=Path, default=None,
                        help="Записать в базу картинки из папки предпросмотра (по manifest.json), без поиска")
    parser.add_argument("--skip-ids", type=str, default=None,
                        help='С --apply-preview: не применять эти id, например "121,264"')
    args = parser.parse_args()

    def _parse_ids(value: str | None) -> set[int]:
        return {int(x) for x in value.replace(" ", "").split(",") if x} if value else set()

    if args.apply_preview is not None:
        asyncio.run(apply_preview(args.apply_preview, _parse_ids(args.skip_ids)))
    else:
        categories = args.category.split(",") if args.category else None
        asyncio.run(main(
            args.replace_all, args.limit, args.dry_run, categories,
            args.allow_unverified, not args.no_ai, args.preview_dir, _parse_ids(args.ids) or None,
        ))
