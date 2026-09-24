"""
Разовая РУЧНАЯ простановка фото для конкретных рецептов по списку
"название -> ссылка на картинку", подобранному вручную (пользователь сам
нашёл подходящие фото через поиск картинок для тех блюд, для которых
автоматический подбор - Pexels/Openverse + проверка через Gemini Vision,
см. backend/photo_search.py - не справился).

Каждое фото скачивается напрямую по указанной ссылке (без поиска и без
проверки через Gemini Vision - ссылка уже выбрана человеком) и помечается
photo_source="source_page" - как при ручном импорте через /import в
bot.py, это защищает фото от перезаписи при следующем автоматическом
python -m scripts.generate_recipe_images --replace-all.

Сопоставление названия из списка с рецептом в базе: сначала точное
совпадение (без учёта регистра/пробелов по краям), если не нашлось -
приблизительное (backend.database.crud.names_are_similar, тот же алгоритм,
что использует сам проект для отсева дублей). Рецепты, которые не удалось
однозначно сопоставить, и картинки, которые не скачались - не трогаются и
выводятся отдельным списком в конце, чтобы поправить вручную (опечатка в
названии, сайт заблокировал скачивание и т.п.).

Запуск (из корня проекта, с активированным venv):
    python -m scripts.set_manual_photos
"""
from __future__ import annotations

import asyncio
import logging

import requests
from sqlalchemy import select

from backend.database.crud import names_are_similar
from backend.database.db import async_session, init_db
from backend.database.models import Recipe
from backend.photo_search import save_photo_bytes
from backend.config import PROXY_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("set_manual_photos")

REQUEST_TIMEOUT_SECONDS = 20

# Некоторые сайты (russianfood.com, povarenok.ru и т.п.) блокируют запросы
# без обычного браузерного User-Agent как заведомо ботов - та же причина,
# по которой он уже используется в backend/recipe_import.py.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# (название рецепта в базе, ссылка на фото) - подобрано пользователем вручную.
MANUAL_PHOTOS: list[tuple[str, str]] = [
    ("Борщ станичный", "https://biwork.ru/picture/111946/1800x1000.webp"),
    ("Гаспачо с шашлычком из креветок", "https://www.russianfood.com/dycontent/images_upl/2/big_1620.jpg"),
    ('Густой немецкий суп "Пихельштайнер"', "https://www.povarenok.ru/data/cache/2015feb/24/35/1045404_30126-710x550x.jpg"),
    ("Кабачковый суп-пюре с плавленым сыром", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTjq50R_BHxDtoGhKMPsNbSE4HU_ceHRoJmtutNVlIfDw&s=10"),
    ("Овощной суп с сырными шариками", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTdzajZ-fwPN4RcT1_n1bG271lzf-zNbZ4EaO0E8v46qg&s=10"),
    ("Паста э Фаджоли", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRXegw_QTYhQp_WeLjHYui26HDAckhE18PU4QOPEoVxCQ&s=10"),
    ("Рамен с пряной свининой", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRvGP0ye9QZXXdDcRYZ4RY4eAY1UbQvvJXieTfHxUreVw&s=10"),
    ("Рисовый суп с капустой и яблоком", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcStYf6s7j97KFo_x_RtExKfsWShvHx7bujlPV0u37jq_g&s=10"),
    ("Стерляжья уха на шампанском с паюсной икрой", "https://swn.ru/upload/medialibrary/fcd/9cncq7o6wdq3nocrd5schb9qu9me4q9x.jpg"),
    ("Суп «Строганов» с курицей и шампиньонами", "https://www.russianfood.com/dycontent/images_upl/213/big_212675.jpg"),
    ("Суккоташ с овощами", "https://www.povarenok.ru/data/cache/2015sep/17/34/1268318_67843-710x550x.jpg"),
    ("Суп «Харчо» (куриный)", "https://cdn.food.ru/unsigned/fit/640/480/ce/0/czM6Ly9tZWRpYS9waWN0dXJlcy8yMDIzMTAxMC80TjJRd3UuanBlZw.webp"),
    ("Суп Геркулес", "https://www.russianfood.com/dycontent/images_upl/21/big_20275.jpg"),
    ("Суп-пюре из кабачков", "https://gotovim-doma.ru/images/recipe/c/38/c38a5b7b8b2cd87cd233882de464eb59_l.jpg"),
    ("Сырный суп с грибами", "https://images.gastronom.ru/vfowcoms2KHq7HsNh_aKHOxdd-7BExYaxl7FCsK_kqY/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzAyMWRlNmQ4LWY2OGItNDk5Ni04MTk2LWViODgxZTJjZWY4Ni5qcGc.webp"),
    ("Сырный суп с копчёными колбасками, картофелем и рисом", "https://www.russianfood.com/dycontent/images_upl/326/big_325432.jpg"),
    ("Тюря с печёным луком на квасе", "https://www.russianfood.com/dycontent/images_upl/518/big_517602.jpg"),
    ("Финский сливочный рыбный суп", "https://images.gastronom.ru/RP_llXshw1DFvjb56l4Z9kErb3DRkhDah_z_JZf2at4/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzQxMDg2YmVlLWU4ZjEtNDM1Zi04ZGU4LTNjZmE2MGY0ZTJhMS5qcGc.webp"),
    ("Холодный огуречный суп", "https://www.russianfood.com/dycontent/images_upl/437/big_436226.jpg"),
    ('Щи кислые "Обыкновенные"', "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRCw2vvWFFojvF4PNHQQdbgO00vW-5rVf3ZnFUBkmUM0A&s=10"),
    ("Бутерброды с авокадо и лососем", "https://lefood.menu/wp-content/uploads/w_images/2022/04/recept-39584-1240x827.jpg"),
    ("Габагул", "https://upload.wikimedia.org/wikipedia/commons/9/92/Coppa_di_Parma.jpg"),
    ("Говядина Веллингтон", "https://bonduelle.ru/760x760/storage/recipes/82531b4bd94ba81cfb8e0d535f853b72.jpeg"),
    ("Домашние пельмени с говядиной и свининой", "https://img.delo-vcusa.ru/2013/02/DSC_1195.jpg"),
    ("Жаркое из курицы с картошкой", "https://images.gastronom.ru/aaRBTPHs8opsPPaGbGNRcWzFTkH-_Eo4fSBn6ofLLrw/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzA5ZDg5ZTEwLWI4ZjEtNDIxMC1iY2M1LTMxYmI1ZTQzMmExOS5qcGc.webp"),
    ("Жюльен с курицей и грибами", "https://nasha.severnaya.ru/upload/iblock/83c/vkmfd9i1eh36toi28eecqioqj1pzzx4r.webp"),
    ("Кабачково-картофельные котлеты с курицей", "https://www.povarenok.ru/data/cache/2020jul/25/38/2749624_82939-710x550x.jpg"),
    ("Капичола (Габагул) домашняя", "https://stranatur.ru/img7/52_1327_post_media_m4FY.png"),
    ("Куриное филе, тушенное в томатном соусе с консервированной фасолью", "https://www.russianfood.com/dycontent/images_upl/408/big_407843.jpg"),
    ("Курочка карри", "https://images.gastronom.ru/58Vy20jaPZ31gTGg9x0cVgwFwHMltRsxMywhrQ9eqcA/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzdlNzE2Y2FhLTZkYjAtNGM3NC1iNTEwLTU2Yzk1MjFiYTBhZC5qcGc.webp"),
    ("Лапша с индейкой в арахисовом соусе", "https://pava-pava.com/upload/resize_cache/iblock/36d/550_550_0/4x4spex5bae0y73l9v0xlo31oc9q7v36.jpg"),
    ("Лапша удон с курицей и овощами в соусе терияки", "https://www.chefmarket.ru/blog/wp-content/uploads/2021/01/ramen-soup-with-noodles--e1610544205600.jpg"),
    ("Лепешки из кабачков с сыром", "https://1001receipt.net/wp-content/uploads/2022/08/lepeshki-iz-kabachkov-s-syrom-na-skovorode-1024x576.jpg"),
    ("Паста аль форно из Сорренто", "https://images.gastronom.ru/Yp0wWvopIWUaBUewqt290LmylLk-J_G41kRdfhGUWzk/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzIwM2E4NTQ5LWY2MTMtNDM2MS05MzJmLTQwZmQ5Y2RkNTUyMi5qcGc.webp"),
    ("Паста с грибами и овощами", "https://www.russianfood.com/dycontent/images_upl/328/big_327961.jpg"),
    ("Паста с чечевицей", "https://www.russianfood.com/dycontent/images_upl/592/big_591903.jpg"),
    ("Перловая каша с мясом", "https://povarenok.by/uploads/images/recepty/big/rassypchataya-perlovaya-kasha-s-myasom--gribami-i-ovoschami.jpg"),
    ("Пулярка в пузыре", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTCRvEE6I7FFwzRfyNFon8UdvzIW1i-hQBXF5xBQLfRng&s=10"),
    ("Пшенная каша с кольраби и карри", "https://images.gastronom.ru/IY7ab7fJ1JUXyRqeaIjSu5PICKkILe2kaEF194_ZHpU/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzL2ViYzRjY2FiLTgwNjQtNDZlYi05M2Q4LTc2MGFiMDFhZGM0Ny5qcGc.webp"),
    ("Пшённая каша с тыквой", "https://www.povarenok.ru/data/cache/2016sep/18/48/1711222_61732-710x550x.jpg"),
    ("Свиные шницели в пряном кляре", "https://cdn.food.ru/unsigned/fit/640/480/ce/0/czM6Ly9tZWRpYS9waWN0dXJlcy8yMDI2MDgyMC91eW5IZm0uanBlZw.jpg"),
    ("Стеклянная лапша с курицей, овощами и соусом чапче", "https://image.danilovskymarket.ru/w:656/h:440/aHR0cHM6Ly9iYWNrLmRhbmlsb3Zza3ltYXJrZXQucnUvZmlsZXMvOTAwNTgyMDMtNjY1OS00ZTZmLTljOTEtYTVhZmY3ODEzNDJiJUQwJUExJUQxJTgyJUQwJUI1JUQwJUJBJUQwJUJCJUQxJThGJUQwJUJEJUQwJUJEJUQwJUIwJUQxJThGJTIwJUQwJUJCJUQwJUIwJUQwJUJGJUQxJTg4JUQwJUIwJTIwJUQxJTgxJTIwJUQwJUJBJUQxJTgzJUQxJTgwJUQwJUI4JUQxJTg2JUQwJUI1JUQwJUI4JUNDJTg2LkpQRw=="),
    ("Тальятелле с печёной тыквой, чесноком и грецкими орехами", "https://images.gastronom.ru/TBmdc6rPxn1nqR2ISCCNTcfv5mKL1_MP2s1PiKOHxiw/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzL2YwMDdiN2I5LTdhMDktNDlkNi04Y2E1LWRmMDdhNDY2NDMwMS5qcGc.webp"),
    ("Фугу в соевом соусе", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ6Z74L243CCR5I5JIRV8jUZr7-bstTT3ieBCUuJFkSkQ&s"),
    ("Чечевица с беконом, яблоками и овощами", "https://images.gastronom.ru/e3xurz0xg3BRd_HzFCzXQvwqJSAqGZ7hssSg21P0S-Q/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzNmODM3ZGE2LTE3YTEtNDBlOS04MTM5LWUyM2Y5OTMzZWM2ZS5qcGc.webp"),
    ("Чили кон тыква", "https://www.povarenok.ru/data/cache/2015nov/20/29/1352928_99601-710x550.jpg"),
    ("Шницель из курицы", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTsCeOm4aSutSu5-EfPmMfg59nz0jZFGSRxGcT3BfldyfOIf4wBXTTtngwk&s=10"),
    ("Баварский картофельный салат", "https://img.povar.ru/640w/f5/81/c8/a9/kartofelnii_salat_bavarskii-78351.jpg"),
    ("Греческий (Хориатики)", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTLBIXupxlqX4rTstVfSiwExiGX-5Adff4sudnehIWKgQ&s=10"),
    ("Кобб-салат", "https://e0.edimdoma.ru/data/recipes/0005/5382/55382-ed4_wide.jpg"),
    ("Охотничий салат", "https://images.gastronom.ru/rbyPtB77zbWWLJ0MGueimWDHaJDyQCF6dnHolEqTZGI/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzU1ZDNmNzNiLWEzOTgtNDNiOC05Mzk2LWY1ODRiZWIzZDRjNi5qcGc.webp"),
    ("Оливье классический", "https://www.abri-kos.ru/upload/images/salat_olive.jpg"),
    ("Перигорский салат", "https://images.gastronom.ru/mB9fGBxkCPeCm5BnJy2V3N3c5JIYkhkd7HkMpPwLvLE/pr:recipe-cover-image/g:ce/rs:auto:0:0:0/L2Ntcy9hbGwtaW1hZ2VzLzk3ZjlmNTMwLTE1YjQtNDVkNS1iZGU1LTYwY2U2MWExYTUzZC5qcGc.webp"),
    ("Салат Винегрет с квашеной капустой", "https://www.russianfood.com/dycontent/images_upl/254/big_253422.jpg"),
    ("Салат с хрустящими баклажанами", "https://media.ovkuse.ru/images/recipes/ce397a83-2b54-4f67-8023-d4a0a5cb9cfa/ce397a83-2b54-4f67-8023-d4a0a5cb9cfa_420_420.webp"),
    ("Блины с красной икрой", "https://prostokvashino.ru/upload/resize_cache/iblock/389/800_800_0/k2sb8tzkq8rgvphxyc5rcbl4u5pn026w.jpg"),
    ("Блины с нутеллой и бананом", "https://img.povar.ru/320w/10/c1/b9/ba/blini_s_nutelloi_i_bananom-726611.JPG"),
    ("Бяли с луком", "https://www.1001eda.com/wp-content/uploads/2014/06/410_02_06_2014_5807.jpg"),
    ("Кальцоне", "https://www.russianfood.com/dycontent/images_upl/99/big_98477.jpg"),
    ("Картофельная запеканка с грибами и сыром", "https://www.russianfood.com/dycontent/images_upl/537/big_536488.jpg"),
    ("Кулебяка с мясом и рыбой", "https://www.povarenok.ru/data/cache/2019mar/19/32/2508054_15545-710x550x.jpg"),
    ("Кулебяка с рыбой, грибами, яйцом и рисом", "https://media.ovkuse.ru/images/step_attachments/a4ddc5d3-833b-4c32-9447-7b0510716dbf/a4ddc5d3-833b-4c32-9447-7b0510716dbf.jpg"),
    ("Овсяные оладушки на сливках с брусничным соусом", "https://www.chefmarket.ru/blog/wp-content/uploads/2019/01/pancake-e1547121523884.jpg"),
    ("Пирог с рикоттой и шпинатом", "https://volshebnaya-eda.ru/wp-content/uploads/2024/07/sloenyj-pirog-s-rikottoj-i-shpinatom-13.jpg"),
    ("Пирожки с капустой", "https://www.russianfood.com/dycontent/images_upl/477/big_476498.jpg"),
    ("Шаньга с картофельной начинкой", "https://www.russianfood.com/dycontent/images_upl/242/big_241403.jpg"),
    ("Блины с творогом и сметаной", "https://vilkin.pro/wp-content/uploads/2021/09/blini-s-tvorogom-i-smetanoi-770x513.jpg"),
    ("Мороженое из маскарпоне", "https://cdn.food.ru/unsigned/fit/640/480/ce/0/czM6Ly9tZWRpYS9waWN0dXJlcy8yMDI0MDUyNi80M3ZVWEsuanBlZw.webp"),
    ("Пана кота", "https://www.povarenok.ru/data/cache/2025jun/27/00/3182716_85058-710x550x.jpg"),
    ("Панна-котта", "https://thumb.wikimedia.org/wikipedia/commons/thumb/8/80/Panna_Cotta_with_cream_and_garnish.jpg/330px-Panna_Cotta_with_cream_and_garnish.jpg"),
    ("Тирамису", "https://cdn.tveda.ru/thumbs/629/62987e0eb44468ebcd42a09d869f72d1/1f62f9565d84755ee967ac70b9b0c812.jpg"),
    ("Торт Захер", "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTHKiasVQBP4Hh_eWfzI_pIMqFcnjc3x_s-MtMK0Ho7vA&s=10"),
    ("Чизкейк Нью Йорк", "https://annatomilchik.ru/wp-content/uploads/2021/07/chizkejk-nyu-jork.jpg"),
    ("Чизкейк классический", "https://pteat.ru/wp-content/uploads/2025/03/itogovoe-2-1536x1024.jpg.webp"),
    # Напитки и соусы, не найденные в фотобанках (сентябрь 2026)
    ("Кумыс из коровьего молока", "https://img-fotki.yandex.ru/get/5409/acronychal.b/0_73267_5850104d_XXL.jpg"),
    ("Матбуха", "https://nyamkin.ru/images/recepts/medium/5e4f1e59221c0.jpg"),
    ('Соус "Тысяча островов"', "https://www.photorecept.ru/wp-content/uploads/2022/08/recept-sousa-1000-ostrovov-903x1300.jpg"),
    ("Масала-чай", "https://cdn.insales-shop.ru/files/1/4129/97423393/original/masala-tea-1756893337155.jpg"),
]


def _proxies(use_proxy: bool) -> dict | None:
    if use_proxy and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def _looks_like_image(data: bytes) -> bool:
    return (
        data[:3] == b"\xff\xd8\xff"                        # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"                 # PNG
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")   # WebP
        or data[:6] in (b"GIF87a", b"GIF89a")
    )


def download_image_bytes(url: str) -> bytes | None:
    for use_proxy in (False, True):
        try:
            response = requests.get(
                url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, proxies=_proxies(use_proxy)
            )
            response.raise_for_status()
            content = response.content
            if not _looks_like_image(content):
                # Мёртвая ссылка часто отдаёт 200 с HTML-страницей вместо картинки.
                logger.warning("По ссылке %s пришла не картинка (%d байт) - пропускаю", url, len(content))
                return None
            return content
        except Exception as e:
            logger.warning("Не удалось скачать %s (прокси=%s): %s", url, use_proxy, e)
    return None


def find_recipe(name: str, all_recipes: list[Recipe], by_exact_name: dict[str, Recipe]) -> Recipe | None:
    exact = by_exact_name.get(name.strip().lower())
    if exact is not None:
        return exact
    for recipe in all_recipes:
        if names_are_similar(recipe.name, name):
            return recipe
    return None


async def run(only: list[str] | None = None) -> None:
    await init_db()
    items = MANUAL_PHOTOS
    if only:
        # --only: обработать только записи, чьё название содержит одну из подстрок,
        # чтобы не перекачивать весь список ради пары новых фото.
        needles = [o.strip().lower() for o in only if o.strip()]
        items = [(n, u) for n, u in MANUAL_PHOTOS if any(x in n.lower() for x in needles)]
        logger.info("--only: к обработке %d из %d записей", len(items), len(MANUAL_PHOTOS))

    async with async_session() as session:
        all_recipes = list(
            (await session.execute(select(Recipe).where(Recipe.is_active.is_(True)))).scalars().all()
        )
    by_exact_name = {r.name.strip().lower(): r for r in all_recipes}

    saved = 0
    not_found: list[str] = []
    download_failed: list[str] = []

    for name, url in items:
        recipe = find_recipe(name, all_recipes, by_exact_name)
        if recipe is None:
            not_found.append(name)
            logger.warning("Рецепт не найден в базе: «%s»", name)
            continue

        image_bytes = await asyncio.to_thread(download_image_bytes, url)
        if image_bytes is None:
            download_failed.append(name)
            continue

        filename = await asyncio.to_thread(save_photo_bytes, recipe.id, image_bytes)

        async with async_session() as session:
            db_recipe = await session.get(Recipe, recipe.id)
            if db_recipe is not None:
                db_recipe.photo_path = filename
                db_recipe.photo_source = "source_page"
                # Автор прежнего фото из фотобанка к новой картинке не относится.
                db_recipe.photo_credit_provider = None
                db_recipe.photo_credit_author = None
                db_recipe.photo_credit_author_url = None
                db_recipe.photo_credit_page_url = None
                db_recipe.photo_credit_license = None
                await session.commit()

        saved += 1
        logger.info("[%d/%d] «%s» (id=%d) -> %s", saved, len(items), recipe.name, recipe.id, filename)

    logger.info(
        "Готово: сохранено %d, не найдено в базе %d, не скачалось %d (из %d в списке).",
        saved, len(not_found), len(download_failed), len(items),
    )
    if not_found:
        logger.info("Не найдены в базе (проверьте название): %s", "; ".join(not_found))
    if download_failed:
        logger.info("Не скачались (проверьте ссылку/доступность сайта): %s", "; ".join(download_failed))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Проставить фото рецептам вручную из списка MANUAL_PHOTOS")
    parser.add_argument("--only", type=str, default=None,
                        help='Только записи, чьё название содержит подстроку, например "Кумыс,Матбуха"')
    args = parser.parse_args()
    asyncio.run(run(args.only.split(",") if args.only else None))
