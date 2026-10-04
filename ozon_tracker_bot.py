import asyncio
import logging
import re
import sqlite3
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from playwright.async_api import BrowserContext, async_playwright

# ---------------------------------------------------------
# КОНФИГУРАЦИЯ БОТА
# ---------------------------------------------------------
BOT_TOKEN = "8762026289:AAHZ-eUqKIjfuYgZU_V5bv51WcXsyob8DF4"  # Вставьте сюда токен вашего Telegram-бота
ADMIN_CHAT_ID = 7805601948  # Ваш Telegram ID (чтобы бот слал находки только вам)
CHECK_INTERVAL_SECONDS = 300  # Интервал сканирования (300 сек = 5 минут)
DB_PATH = "ozon_radar.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """Создает таблицы для хранения задач и истории отправленных товаров."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Таблица для мониторинга: ключевое слово или готовая ссылка Ozon
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS radar_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                target_url_or_query TEXT NOT NULL,
                min_price REAL NOT NULL,
                max_price REAL NOT NULL,
                stop_words TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1
            )
        """)
        # Таблица для дедупликации (чтобы один товар не присылался дважды)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_products (
                product_id TEXT PRIMARY KEY,
                task_id INTEGER,
                title TEXT,
                price REAL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES radar_tasks(id)
            )
        """)
        conn.commit()

def add_radar_task(title: str, target: str, min_p: float, max_p: float, stop_words: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO radar_tasks (title, target_url_or_query, min_price, max_price, stop_words)
            VALUES (?, ?, ?, ?, ?)
        """, (title, target, min_p, max_p, stop_words))
        conn.commit()
        return cursor.lastrowid

def get_active_tasks() -> List[sqlite3.Row]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM radar_tasks WHERE is_active = 1")
        return cursor.fetchall()

def delete_radar_task(task_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM radar_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0

def is_product_sent(product_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM sent_products WHERE product_id = ?", (product_id,))
        return cursor.fetchone() is not None

def record_sent_product(product_id: str, task_id: int, title: str, price: float):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO sent_products (product_id, task_id, title, price)
            VALUES (?, ?, ?, ?)
        """, (product_id, task_id, title, price))
        conn.commit()

def extract_price(text: str) -> Optional[float]:
    """Извлекает числовую цену в BYN из текстового блока карточки."""
    # Регулярка ловит шаблоны: 7,50 р., 15.99 BYN, 18 р
    match = re.search(r"(\d+[\.,]?\d*)\s*(?:р\.|BYN|руб)", text, re.IGNORECASE)
    if match:
        raw_price = match.group(1).replace(",", ".").replace(" ", "")
        try:
            return float(raw_price)
        except ValueError:
            return None
    return None

async def parse_ozon_page(context: BrowserContext, target: str) -> List[dict]:
    """
    Открывает поисковый запрос или готовую ссылку Ozon.by и извлекает карточки.
    """
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    else:
        # Если передано слово — строим поисковую ссылку с сортировкой по возрастанию цены
        encoded_query = quote_plus(target)
        url = f"https://www.ozon.by/search/?from_global=true&sorting=price&text={encoded_query}"

    page = await context.new_page()
    items = []

    try:
        logger.info(f"Загружаем страницу: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Ждем подгрузки карточек Ozon
        await page.wait_for_timeout(3500)

        # Селекторы карточек Ozon
        cards = await page.query_selector_all("div[data-widget='searchResultsV2'] div[class*='tile']")
        if not cards:
            cards = await page.query_selector_all("div[class*='tile'][data-index]")

        for card in cards[:30]:  # Берем первые 30 самых дешевых предложений
            raw_text = await card.inner_text()
            
            # Пропускаем товары, недоступные для доставки в Беларусь
            if "не доставляется" in raw_text.lower():
                continue

            link_elem = await card.query_selector("a[href*='/product/']")
            if not link_elem:
                continue

            href = await link_elem.get_attribute("href")
            match = re.search(r"/product/[^/]+-(\d+)", href)
            if not match:
                match = re.search(r"/product/(\d+)", href)
            if not match:
                continue

            product_id = match.group(1)
            clean_url = f"https://www.ozon.by/product/{product_id}/"

            # Извлекаем заголовок
            title_elem = await card.query_selector("span.tsBody500Medium, span[class*='title']")
            title = (await title_elem.inner_text()).strip() if title_elem else "Товар Ozon"

            # Извлекаем цену
            price = extract_price(raw_text)
            if price is not None:
                items.append({
                    "id": product_id,
                    "title": title,
                    "price": price,
                    "url": clean_url
                })

    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}")
    finally:
        await page.close()

    return items

async def background_radar_worker(bot: Bot):
    """Фоновый цикл проверки всех сохраненных радаров."""
    logger.info("Фоновый воркер радара запущен.")
    
    async with async_playwright() as p:
        # Запускаем браузер с реалистичным отпечатком
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ru-BY",
            viewport={"width": 1280, "height": 800}
        )

        while True:
            tasks = get_active_tasks()
            if not tasks:
                logger.info("Список радаров пуст. Ожидание задач...")
                await asyncio.sleep(30)
                continue

            for task in tasks:
                t_id = task["id"]
                t_title = task["title"]
                t_target = task["target_url_or_query"]
                min_p = task["min_price"]
                max_p = task["max_price"]
                raw_stop_words = task["stop_words"] or ""
                stop_words = [w.strip().lower() for w in raw_stop_words.split(",") if w.strip()]

                logger.info(f"Проверка радара #{t_id} [{t_title}] диапазон {min_p} - {max_p} BYN...")
                found_products = await parse_ozon_page(context, t_target)

                for item in found_products:
                    p_id = item["id"]
                    price = item["price"]
                    p_title = item["title"]
                    p_url = item["url"]

                    # 1. Проверяем, не отправляли ли мы этот товар ранее
                    if is_product_sent(p_id):
                        continue

                    # 2. Проверяем стоп-слова
                    lower_title = p_title.lower()
                    if any(sw in lower_title for sw in stop_words):
                        continue

                    # 3. Проверяем индивидуальную вилку цен (min_p <= price <= max_p)
                    if min_p <= price <= max_p:
                        # Фиксируем отправку
                        record_sent_product(p_id, t_id, p_title, price)

                        # Сообщение в единый чат со ссылкой именно на этот товар
                        msg = (
                            f"🎯 <b>НАХОДКА В ВАШЕМ ДИАПАЗОНЕ!</b>\n\n"
                            f"📌 <b>Радар:</b> {t_title}\n"
                            f"📦 <b>Товар:</b> {p_title}\n"
                            f"💰 <b>Цена:</b> <code>{price:.2f} BYN</code> (в вилке {min_p}–{max_p} BYN)\n"
                            f"🚚 <b>Доставка:</b> Доступно для Ozon Беларусь\n\n"
                            f"👉 <a href='{p_url}'>Перейти к товару на Ozon.by</a>"
                        )

                        if ADMIN_CHAT_ID != 0:
                            try:
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=msg,
                                    parse_mode="HTML",
                                    disable_web_page_preview=False
                                )
                            except Exception as err:
                                logger.error(f"Не удалось отправить уведомление: {err}")

                # Пауза между разными ссылками/запросами
                await asyncio.sleep(5)

            # Ожидание перед следующим кругом
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    chat_id = message.chat.id
    help_text = (
        f"👋 <b>Добро пожаловать в Ozon Deal Radar (Беларусь)!</b>\n\n"
        f"Ваш Chat ID: <code>{chat_id}</code>\n"
        f"<i>(Укажите его в коде бота в переменной ADMIN_CHAT_ID, чтобы получать находки)</i>\n\n"
        f"<b>Как добавить товар на слежение:</b>\n"
        f"Каждый радар имеет свой индивидуальный диапазон цен!\n\n"
        f"🔹 <b>По слову:</b>\n"
        f"<code>/add Гирлянда 5 9 крючки,батарейка,удлинитель</code>\n"
        f"<i>(Будут искаться гирлянды строго от 5 до 9 BYN)</i>\n\n"
        f"🔹 <b>Для другого товара с другим диапазоном:</b>\n"
        f"<code>/add Наушники 15 35 чехол,провод,насадки</code>\n"
        f"<i>(Будут искаться наушники строго от 15 до 35 BYN)</i>\n\n"
        f"🔹 <b>По готовой ссылке с Ozon:</b>\n"
        f"<code>/add Смартфон_Скидки https://ozon.by/category/... 100 250 чехол,стекло</code>\n\n"
        f"<b>Другие команды:</b>\n"
        f"/list — список всех активных радаров\n"
        f"/del [ID] — удалить радар"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("add"))
async def cmd_add(message: Message):
    """
    Формат команды:
    /add <Название_или_Ссылка> <мин_цена> <макс_цена> [стоп-слова через запятую]
    """
    parts = message.text.split(maxsplit=4)
    if len(parts) < 4:
        await message.answer(
            "⚠️ <b>Неверный формат команды!</b>\n\n"
            "Пример:\n"
            "<code>/add Гирлянда 5 9 крючки,батарейка</code>\n"
            "или\n"
            "<code>/add Наушники 15 30 чехол,кабель</code>",
            parse_mode="HTML"
        )
        return

    target = parts[1].strip()
    try:
        min_p = float(parts[2].replace(",", "."))
        max_p = float(parts[3].replace(",", "."))
    except ValueError:
        await message.answer("❌ Ошибка: минимальная и максимальная цена должны быть числами (BYN).")
        return

    stop_words = parts[4].strip() if len(parts) > 4 else ""
    title = target if not target.startswith("http") else "Ссылка Ozon"

    task_id = add_radar_task(title, target, min_p, max_p, stop_words)

    response = (
        f"✅ <b>Радар #{task_id} успешно добавлен!</b>\n\n"
        f"🎯 <b>Цель:</b> {target}\n"
        f"📊 <b>Индивидуальный диапазон:</b> от <code>{min_p:.2f}</code> до <code>{max_p:.2f} BYN</code>\n"
        f"🚫 <b>Стоп-слова:</b> {stop_words if stop_words else 'нет'}\n\n"
        f"<i>Бот начнет проверку в ближайшем цикле и пришлет прямую ссылку на товар при обнаружении цены в этом окне.</i>"
    )
    await message.answer(response, parse_mode="HTML")

@dp.message(Command("list"))
async def cmd_list(message: Message):
    tasks = get_active_tasks()
    if not tasks:
        await message.answer("📭 У вас пока нет активных радаров. Добавьте через <code>/add</code>", parse_mode="HTML")
        return

    lines = ["📋 <b>Ваши активные радары:</b>\n"]
    for t in tasks:
        lines.append(
            f"<b>#{t['id']} {t['title']}</b>\n"
            f"   💰 Диапазон: <code>{t['min_price']} - {t['max_price']} BYN</code>\n"
            f"   🚫 Стоп-слова: {t['stop_words'] or 'нет'}\n"
            f"   Удалить: /del_{t['id']}\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(F.text.startswith("/del_"))
async def cmd_del_short(message: Message):
    try:
        task_id = int(message.text.replace("/del_", "").strip())
        if delete_radar_task(task_id):
            await message.answer(f"🗑 Радар #{task_id} удален.")
        else:
            await message.answer("❌ Радар с таким ID не найден.")
    except ValueError:
        await message.answer("❌ Неверный ID.")

async def main():
    init_database()
    bot = Bot(token=BOT_TOKEN)

    # Запускаем фоновый парсер в параллельной задаче
    asyncio.create_task(background_radar_worker(bot))

    logger.info("Запуск Telegram-бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
