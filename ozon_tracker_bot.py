import asyncio
import logging
import os
import re
import sqlite3
from typing import List, Optional
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiohttp import web
from playwright.async_api import BrowserContext, async_playwright

BOT_TOKEN = "8762026289:AAHZ-eUqKIjfuYgZU_V5bv51WcXsyob8DF4"
ADMIN_CHAT_ID = 7805601948
# Быстрый интервал сканирования: 45 секунд между кругами
CHECK_INTERVAL_SECONDS = 45
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
    """Извлекает цену в BYN из текстовых блоков Ozon."""
    clean = text.replace("\xa0", " ").replace("&nbsp;", " ")
    match = re.search(r"(\d+(?:[\s\u00A0]\d+)*[.,]?\d*)\s*(?:р\.|BYN|руб)", clean, re.IGNORECASE)
    if match:
        raw_price = match.group(1).replace(" ", "").replace(",", ".")
        try:
            return float(raw_price)
        except ValueError:
            return None
    return None

async def parse_ozon_page(context: BrowserContext, target: str) -> List[dict]:
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    else:
        encoded_query = quote_plus(target)
        url = f"https://www.ozon.by/search/?from_global=true&sorting=price&text={encoded_query}"

    page = await context.new_page()
    
    # ТУРБО-РЕЖИМ: Блокируем картинки, медиа и рекламу для мгновенной загрузки
    await page.route(
        "**/*",
        lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"]
        or "google-analytics" in route.request.url
        or "yandex" in route.request.url
        else route.continue_()
    )

    items = []

    try:
        # domcontentloaded вместо networkidle для молниеносного ответа
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(1200)
        await page.evaluate("window.scrollBy(0, 500)")
        await page.wait_for_timeout(800)

        cards = await page.query_selector_all("div[data-widget='searchResultsV2'] > div > div, div[class*='tile']")
        if not cards:
            cards = await page.query_selector_all("div[class*='tile'][data-index]")

        seen_ids = set()

        for card in cards[:35]:
            raw_text = await card.inner_text()
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
            if product_id in seen_ids:
                continue
            seen_ids.add(product_id)

            clean_url = f"https://www.ozon.by/product/{product_id}/"

            title_elem = await card.query_selector("span.tsBody500Medium, span[class*='title'], span.tsBodyL")
            title = (await title_elem.inner_text()).strip() if title_elem else "Товар Ozon"

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
    logger.info("Фоновый турбо-воркер радара запущен.")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-BY",
            viewport={"width": 1280, "height": 800}
        )

        while True:
            tasks = get_active_tasks()
            if not tasks:
                await asyncio.sleep(15)
                continue

            for task in tasks:
                t_id = task["id"]
                t_title = task["title"]
                t_target = task["target_url_or_query"]
                min_p = task["min_price"]
                max_p = task["max_price"]
                raw_stop_words = task["stop_words"] or ""
                stop_words = [w.strip().lower() for w in raw_stop_words.split(",") if w.strip()]

                found_products = await parse_ozon_page(context, t_target)
                
                # Логируем минимальную цену в каталоге для наглядности
                if found_products:
                    lowest_now = min(p["price"] for p in found_products)
                    logger.info(f"🔎 [{t_title}] Найдено {len(found_products)} шт. Самая низкая цена: {lowest_now:.2f} BYN (Ваш фильтр: {min_p}–{max_p} BYN)")
                else:
                    logger.info(f"🔎 [{t_title}] Карточки пока не распознаны Ozon.")

                for item in found_products:
                    p_id = item["id"]
                    price = item["price"]
                    p_title = item["title"]
                    p_url = item["url"]

                    if is_product_sent(p_id):
                        continue

                    lower_title = p_title.lower()
                    if any(sw in lower_title for sw in stop_words):
                        continue

                    if min_p <= price <= max_p:
                        record_sent_product(p_id, t_id, p_title, price)

                        msg = (
                            f"⚡️ <b>СВЕЖАЯ НАХОДКА!</b>\n\n"
                            f"📌 <b>Радар:</b> {t_title}\n"
                            f"📦 <b>Товар:</b> {p_title}\n"
                            f"💰 <b>Цена:</b> <code>{price:.2f} BYN</code> (в вилке {min_p}–{max_p} BYN)\n"
                            f"🚚 <b>Доставка:</b> Доступно для Ozon Беларусь\n\n"
                            f"👉 <a href='{p_url}'>Купить на Ozon.by прямо сейчас</a>"
                        )

                        if ADMIN_CHAT_ID != 0:
                            try:
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=msg,
                                    parse_mode="HTML",
                                    disable_web_page_preview=False
                                )
                                logger.info(f"Уведомление отправлено в чат по товару #{p_id}")
                            except Exception as err:
                                logger.error(f"Не удалось отправить уведомление: {err}")

                await asyncio.sleep(2)

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

async def start_web_server():
    async def handle_ping(request):
        return web.Response(text="Ozon Radar Bot is running (Live)!")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер ответа Render запущен на порту {port}")

dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    chat_id = message.chat.id
    help_text = (
        f"👋 <b>Добро пожаловать в Ozon Deal Radar (Беларусь)!</b>\n\n"
        f"Ваш Chat ID: <code>{chat_id}</code>\n\n"
        f"<b>Как добавить товар на скоростной мониторинг:</b>\n\n"
        f"🔹 <b>По слову:</b>\n"
        f"<code>/add Гирлянда 5 9 крючки,батарейка,удлинитель</code>\n\n"
        f"🔹 <b>По готовой ссылке с Ozon:</b>\n"
        f"<code>/add Смартфон_Скидки https://ozon.by/category/... 100 250 чехол,стекло</code>\n\n"
        f"<b>Другие команды:</b>\n"
        f"/list — список активных радаров\n"
        f"/del [ID] — удалить радар"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("add"))
async def cmd_add(message: Message):
    parts = message.text.split(maxsplit=4)
    if len(parts) < 4:
        await message.answer(
            "⚠️ <b>Неверный формат команды!</b>\n\n"
            "Пример:\n"
            "<code>/add Гирлянда 5 9 крючки,батарейка</code>",
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
        f"✅ <b>Радар #{task_id} добавлен в скоростной мониторинг!</b>\n\n"
        f"🎯 <b>Цель:</b> {target}\n"
        f"📊 <b>Диапазон:</b> от <code>{min_p:.2f}</code> до <code>{max_p:.2f} BYN</code>\n"
        f"🚫 <b>Стоп-слова:</b> {stop_words if stop_words else 'нет'}\n\n"
        f"<i>Интервал проверки: ~45 секунд. Ссылка придет моментально при появлении товара!</i>"
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

    # 1. Запуск встроенного веб-сервера для Render (зеленый статус Live)
    await start_web_server()

    # 2. Фоновый скоростной сканер Ozon
    asyncio.create_task(background_radar_worker(bot))

    # 3. Слушатель сообщений Telegram
    logger.info("Запуск Telegram-бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
