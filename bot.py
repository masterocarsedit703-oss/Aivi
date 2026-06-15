import asyncio
import logging
import os
import random
import io
import string
import uuid
from datetime import datetime
from io import BytesIO

import aiohttp
import aiosqlite
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CRYPTO_BOT_TOKEN = os.environ["CRYPTO_BOT_TOKEN"]

ADMIN_IDS = [8761713139]
CHANNEL_ID = "@aivinewschannel"
SUPPORT_USERNAME = "@aiviproj"

DB_PATH = "shop.db"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

PRESET_AMOUNTS = [1, 5, 10, 25, 50]
MIN_TOPUP = 1
MAX_QTY = 50

# ─── STATES ──────────────────────────────────────────────────────────────────

class CaptchaState(StatesGroup):
    waiting_answer = State()

class CatalogStates(StatesGroup):
    waiting_custom_qty = State()

class TopupStates(StatesGroup):
    waiting_custom_amount = State()

class AdminStates(StatesGroup):
    waiting_cat_name = State()
    waiting_cat_desc = State()
    waiting_subcat_cat = State()
    waiting_subcat_name = State()
    waiting_subcat_desc = State()
    waiting_subcat_price = State()
    waiting_items_cat = State()
    waiting_items_subcat = State()
    waiting_items_content = State()
    waiting_balance_user_id = State()
    waiting_balance_amount = State()
    waiting_broadcast_text = State()

# ─── DATABASE ─────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0,
                total_spent REAL DEFAULT 0,
                registered_at TEXT DEFAULT (datetime('now')),
                captcha_passed INTEGER DEFAULT 0,
                subscribed INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subcategories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subcategory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subcategory_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                is_sold INTEGER DEFAULT 0,
                sold_at TEXT,
                order_id TEXT,
                FOREIGN KEY (subcategory_id) REFERENCES subcategories(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                subcategory_id INTEGER NOT NULL,
                subcategory_name TEXT NOT NULL,
                category_name TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL,
                total REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'USDT',
                status TEXT DEFAULT 'pending',
                crypto_invoice_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT
            )
        """)
        await db.commit()


def _generate_order_id():
    chars = string.ascii_uppercase + string.digits
    return "ORD-" + "".join(random.choices(chars, k=8))


async def get_or_create_user(user_id, username, full_name):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO users (id, username, full_name) VALUES (?, ?, ?)", (user_id, username, full_name))
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
        return dict(row)


async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def set_captcha_passed(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET captcha_passed = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def set_subscribed(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET subscribed = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def update_balance(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        await db.commit()


async def get_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT c.*, COALESCE((
                SELECT SUM(CASE WHEN si.is_sold = 0 THEN 1 ELSE 0 END)
                FROM subcategories s LEFT JOIN subcategory_items si ON si.subcategory_id = s.id
                WHERE s.category_id = c.id
            ), 0) as total_stock
            FROM categories c ORDER BY c.id
        """) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_category(cat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def add_category(name, description=""):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO categories (name, description) VALUES (?, ?)", (name, description))
        await db.commit()
        return cur.lastrowid


async def delete_category(cat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM subcategories WHERE category_id = ?", (cat_id,)) as cur:
            sub_ids = [r[0] for r in await cur.fetchall()]
        for sid in sub_ids:
            await db.execute("DELETE FROM subcategory_items WHERE subcategory_id = ?", (sid,))
        await db.execute("DELETE FROM subcategories WHERE category_id = ?", (cat_id,))
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await db.commit()


async def get_subcategories(cat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, COALESCE((
                SELECT COUNT(*) FROM subcategory_items si
                WHERE si.subcategory_id = s.id AND si.is_sold = 0
            ), 0) as stock
            FROM subcategories s WHERE s.category_id = ? ORDER BY s.id
        """, (cat_id,)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_subcategory(sub_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, COALESCE((
                SELECT COUNT(*) FROM subcategory_items si
                WHERE si.subcategory_id = s.id AND si.is_sold = 0
            ), 0) as stock
            FROM subcategories s WHERE s.id = ?
        """, (sub_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def add_subcategory(cat_id, name, description, price):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO subcategories (category_id, name, description, price) VALUES (?, ?, ?, ?)",
            (cat_id, name, description, price),
        )
        await db.commit()
        return cur.lastrowid


async def delete_subcategory(sub_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subcategory_items WHERE subcategory_id = ?", (sub_id,))
        await db.execute("DELETE FROM subcategories WHERE id = ?", (sub_id,))
        await db.commit()


async def add_items_to_subcategory(sub_id, items):
    async with aiosqlite.connect(DB_PATH) as db:
        for item in items:
            if item.strip():
                await db.execute("INSERT INTO subcategory_items (subcategory_id, content) VALUES (?, ?)", (sub_id, item.strip()))
        await db.commit()


async def atomic_purchase(user_id, sub_id, qty=1):
    async with aiosqlite.connect(DB_PATH, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT s.*, c.name as category_name FROM subcategories s "
                "JOIN categories c ON c.id = s.category_id WHERE s.id = ?", (sub_id,)
            ) as cur:
                sub = await cur.fetchone()
            if not sub:
                await db.execute("ROLLBACK")
                return {"error": "no_product"}
            price_each = sub["price"]
            total_price = price_each * qty
            sub_name = sub["name"]
            cat_name = sub["category_name"]
            async with db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)) as cur:
                urow = await cur.fetchone()
            if not urow or urow["balance"] < total_price:
                await db.execute("ROLLBACK")
                return {"error": "balance"}
            async with db.execute(
                "SELECT id, content FROM subcategory_items WHERE subcategory_id = ? AND is_sold = 0 LIMIT ?",
                (sub_id, qty)
            ) as cur:
                items = await cur.fetchall()
            if len(items) < qty:
                await db.execute("ROLLBACK")
                return {"error": "stock"}
            order_id = _generate_order_id()
            now = datetime.now().isoformat()
            await db.execute(
                "UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE id = ?",
                (total_price, total_price, user_id),
            )
            contents = []
            for item in items:
                await db.execute(
                    "UPDATE subcategory_items SET is_sold = 1, sold_at = ?, order_id = ? WHERE id = ? AND is_sold = 0",
                    (now, order_id, item["id"]),
                )
                contents.append(item["content"])
            await db.execute(
                "INSERT INTO orders (id, user_id, subcategory_id, subcategory_name, category_name, price, total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, user_id, sub_id, sub_name, cat_name, price_each, total_price),
            )
            await db.execute("COMMIT")
            return {"error": None, "order_id": order_id, "contents": contents,
                    "qty": qty, "price_each": price_each, "total": total_price,
                    "subcategory_name": sub_name, "category_name": cat_name}
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            return {"error": "internal"}


async def get_user_orders(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 20", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def create_invoice(invoice_id, user_id, amount, crypto_invoice_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO invoices (id, user_id, amount, crypto_invoice_id) VALUES (?, ?, ?, ?)",
            (invoice_id, user_id, amount, crypto_invoice_id),
        )
        await db.commit()


async def get_invoice(invoice_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_invoice_for_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM invoices WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def mark_invoice_paid_and_credit(invoice_id, user_id, amount):
    async with aiosqlite.connect(DB_PATH, isolation_level=None) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)) as cur:
                row = await cur.fetchone()
            if not row or row[0] != "pending":
                await db.execute("ROLLBACK")
                return False
            now = datetime.now().isoformat()
            await db.execute(
                "UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'pending'",
                (now, invoice_id),
            )
            await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
            await db.execute("COMMIT")
            return True
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            return False


async def mark_invoice_expired(invoice_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE invoices SET status = 'expired' WHERE id = ? AND status = 'pending'", (invoice_id,)
        )
        await db.commit()


async def cancel_pending_invoices(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE invoices SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,)
        )
        await db.commit()


async def get_analytics():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = datetime.now().strftime("%Y-%m-%d")
        async with db.execute("SELECT COUNT(*) as cnt FROM users") as cur:
            total_users = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COUNT(*) as cnt FROM users WHERE registered_at LIKE ?", (f"{today}%",)) as cur:
            new_users_today = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COALESCE(SUM(total), 0) as s FROM orders") as cur:
            total_revenue = (await cur.fetchone())["s"]
        async with db.execute("SELECT COALESCE(SUM(total), 0) as s FROM orders WHERE created_at LIKE ?", (f"{today}%",)) as cur:
            revenue_today = (await cur.fetchone())["s"]
        async with db.execute("SELECT COUNT(*) as cnt FROM orders") as cur:
            total_orders = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COUNT(*) as cnt FROM orders WHERE created_at LIKE ?", (f"{today}%",)) as cur:
            orders_today = (await cur.fetchone())["cnt"]
        async with db.execute("SELECT COUNT(*) as cnt FROM subcategory_items WHERE is_sold = 0") as cur:
            items_in_stock = (await cur.fetchone())["cnt"]
        return {
            "total_users": total_users, "new_users_today": new_users_today,
            "total_revenue": total_revenue, "revenue_today": revenue_today,
            "total_orders": total_orders, "orders_today": orders_today,
            "items_in_stock": items_in_stock,
        }


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM users") as cur:
            rows = await cur.fetchall()
        return [r["id"] for r in rows]


async def get_all_users_with_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.id, u.username, u.full_name, u.balance, u.registered_at, COUNT(o.id) AS purchases
            FROM users u LEFT JOIN orders o ON o.user_id = u.id
            GROUP BY u.id ORDER BY u.registered_at DESC
        """) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

# ─── CRYPTOBOT ───────────────────────────────────────────────────────────────

async def cryptobot_create_invoice(amount, payload):
    url = f"{CRYPTOBOT_API_URL}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {
        "asset": "USDT", "amount": str(amount), "payload": payload,
        "description": f"Пополнение баланса на {amount}$",
        "allow_comments": "false", "allow_anonymous": "false", "expires_in": 3600,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
    except Exception:
        pass
    return None


async def cryptobot_check_invoice(crypto_invoice_id):
    url = f"{CRYPTOBOT_API_URL}/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": str(crypto_invoice_id)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    items = data["result"].get("items", [])
                    if items:
                        return items[0].get("status", "active")
    except Exception:
        pass
    return "active"

# ─── CAPTCHA ─────────────────────────────────────────────────────────────────

def generate_captcha_code(length=None):
    if length is None:
        length = random.randint(5, 7)
    return "".join([str(random.randint(0, 9)) for _ in range(length)])


def generate_captcha_image(code):
    width, height = 200, 80
    bg_color = (random.randint(220, 245), random.randint(220, 245), random.randint(220, 245))
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        color = (random.randint(150, 200), random.randint(150, 200), random.randint(150, 200))
        draw.line([(x1, y1), (x2, y2)], fill=color, width=1)
    for _ in range(300):
        x, y = random.randint(0, width), random.randint(0, height)
        color = (random.randint(100, 200), random.randint(100, 200), random.randint(100, 200))
        draw.point((x, y), fill=color)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
    except Exception:
        try:
            font = ImageFont.truetype("/run/current-system/sw/share/X11/fonts/DejaVuSans-Bold.ttf", 44)
        except Exception:
            font = ImageFont.load_default()
    char_spacing = (width - 20) // len(code)
    for i, ch in enumerate(code):
        angle = random.randint(-18, 18)
        color = (random.randint(30, 100), random.randint(30, 100), random.randint(30, 100))
        char_img = Image.new("RGBA", (50, 60), (0, 0, 0, 0))
        char_draw = ImageDraw.Draw(char_img)
        char_draw.text((5, 5), ch, font=font, fill=color)
        char_img = char_img.rotate(angle, expand=True)
        x = 10 + i * char_spacing + random.randint(-3, 3)
        y = random.randint(8, 18)
        img.paste(char_img, (x, y), char_img)
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────

def subscription_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")
    builder.button(text="✅ Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()


def main_menu_kb():
    builder = ReplyKeyboardBuilder()
    builder.button(text="💎 Ассортимент")
    builder.button(text="👤 Профиль")
    builder.button(text="💳 Пополнить баланс")
    builder.button(text="📦 Мои заказы")
    builder.button(text="💬 Поддержка")
    builder.adjust(2, 1, 2)
    return builder.as_markup(resize_keyboard=True)


def topup_amounts_kb():
    builder = InlineKeyboardBuilder()
    for a in PRESET_AMOUNTS:
        builder.button(text=f"💰 {a}$", callback_data=f"topup_amount:{a}")
    builder.button(text="✏️ Ввести свою сумму", callback_data="topup_custom")
    builder.adjust(3, 2, 1)
    return builder.as_markup()


def pay_invoice_kb(pay_url, invoice_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Оплатить через CryptoBot", url=pay_url)
    builder.button(text="✅ Проверить оплату", callback_data=f"check_pay:{invoice_id}")
    builder.button(text="❌ Отмена", callback_data="cancel_pay")
    builder.adjust(1)
    return builder.as_markup()


def categories_kb(categories):
    builder = InlineKeyboardBuilder()
    for cat in categories:
        stock = cat.get("total_stock", 0)
        stock_label = f" ({stock} шт.)" if stock > 0 else " (нет)"
        builder.button(text=f"{cat['name']}{stock_label}", callback_data=f"cat:{cat['id']}")
    builder.adjust(1)
    return builder.as_markup()


def subcategories_kb(subcategories, cat_id):
    builder = InlineKeyboardBuilder()
    for sub in subcategories:
        stock = sub.get("stock", 0)
        stock_label = f" | {stock} шт." if stock > 0 else " | нет"
        builder.button(text=f"{sub['name']} | {sub['price']:.2f}${stock_label}", callback_data=f"sub:{sub['id']}")
    builder.button(text="🔙 К категориям", callback_data="back_cats")
    builder.adjust(1)
    return builder.as_markup()


def subcategory_buy_kb(sub_id, stock):
    builder = InlineKeyboardBuilder()
    if stock > 0:
        for qty in [1, 3, 5]:
            if stock >= qty:
                builder.button(text=f"🛒 {qty} шт.", callback_data=f"buy:{sub_id}:{qty}")
        builder.button(text="✏️ Своё кол-во", callback_data=f"buy_custom:{sub_id}")
    builder.button(text="🔙 Назад", callback_data=f"back_sub:{sub_id}")
    builder.adjust(3, 1, 1)
    return builder.as_markup()


def confirm_buy_kb(sub_id, qty):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить покупку", callback_data=f"confirm_buy:{sub_id}:{qty}")
    builder.button(text="❌ Отмена", callback_data=f"sub:{sub_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_main_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить категорию", callback_data="adm:add_cat")
    builder.button(text="📂 Добавить подкатегорию", callback_data="adm:add_subcat")
    builder.button(text="🗃 Добавить единицы", callback_data="adm:add_items")
    builder.button(text="🗂 Управление", callback_data="adm:manage")
    builder.button(text="📊 Аналитика", callback_data="adm:analytics")
    builder.button(text="💰 Пополнить баланс", callback_data="adm:add_balance")
    builder.button(text="📣 Рассылка", callback_data="adm:broadcast")
    builder.button(text="👥 Пользователи", callback_data="adm:users")
    builder.button(text="📥 Выгрузить базу", callback_data="adm:export_users")
    builder.adjust(1, 1, 1, 1, 2, 2, 1)
    return builder.as_markup()


def admin_cats_kb(categories, action):
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat['name'], callback_data=f"adm:{action}:{cat['id']}")
    builder.button(text="🔙 Назад", callback_data="adm:back")
    builder.adjust(1)
    return builder.as_markup()


def admin_subcats_kb(subcats, cat_id, action):
    builder = InlineKeyboardBuilder()
    for sub in subcats:
        stock = sub.get("stock", 0)
        builder.button(text=f"{sub['name']} | {sub['price']:.2f}$ | {stock} шт.", callback_data=f"adm:{action}:{sub['id']}")
    builder.button(text="🔙 Назад", callback_data="adm:manage")
    builder.adjust(1)
    return builder.as_markup()


def admin_manage_cats_kb(categories):
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat['name'], callback_data=f"adm:manage_cat:{cat['id']}")
    builder.button(text="🔙 Назад", callback_data="adm:back")
    builder.adjust(1)
    return builder.as_markup()


def admin_manage_cat_kb(cat_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Подкатегории", callback_data=f"adm:manage_subs:{cat_id}")
    builder.button(text="🗑 Удалить категорию", callback_data=f"adm:del_cat:{cat_id}")
    builder.button(text="🔙 Назад", callback_data="adm:manage")
    builder.adjust(1)
    return builder.as_markup()


def admin_manage_sub_kb(sub_id, cat_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить подкатегорию", callback_data=f"adm:del_sub:{sub_id}:{cat_id}")
    builder.button(text="🔙 Назад", callback_data=f"adm:manage_subs:{cat_id}")
    builder.adjust(1)
    return builder.as_markup()


def back_admin_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В админ-панель", callback_data="adm:back")
    return builder.as_markup()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def is_admin(user_id):
    return user_id in ADMIN_IDS


async def _check_subscribed_via_api(bot, user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return True


async def send_captcha(target, state: FSMContext):
    code = generate_captcha_code()
    image_bytes = generate_captcha_image(code)
    await state.set_state(CaptchaState.waiting_answer)
    await state.update_data(captcha_code=code)
    photo = BufferedInputFile(image_bytes, filename="captcha.png")
    text = "🔐 Введи цифры с картинки:"
    if isinstance(target, Message):
        await target.answer_photo(photo, caption=text)
    elif isinstance(target, CallbackQuery):
        await target.message.answer_photo(photo, caption=text)
        try:
            await target.message.delete()
        except Exception:
            pass


async def check_subscription_flow(target, user_id):
    text = (
        "📢 <b>Подпишись на канал</b>\n\n"
        "Обязательное условие для доступа к боту.\n"
        "После подписки нажми ✅ <b>Я подписался</b>"
    )
    markup = subscription_kb()
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup, parse_mode="HTML")
    elif isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


async def show_main_menu(target):
    text = "🏪 <b>Главное меню</b>"
    markup = main_menu_kb()
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup, parse_mode="HTML")
    elif isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup, parse_mode="HTML")
        try:
            await target.message.delete()
        except Exception:
            pass


async def show_admin(target, state=None):
    if state:
        await state.clear()
    text = "🛠 <b>Админ-панель</b>\n\nВыбери действие 👇"
    markup = admin_main_kb()
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup, parse_mode="HTML")
    elif isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await target.answer()

# ─── ROUTER ───────────────────────────────────────────────────────────────────

router = Router()
_purchasing: set = set()
_invoice_creating: set = set()
_payment_checking: set = set()

# ── START / CAPTCHA / SUB ─────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    if not user["captcha_passed"]:
        await send_captcha(message, state)
        return
    if not user["subscribed"]:
        if await _check_subscribed_via_api(message.bot, message.from_user.id):
            await set_subscribed(message.from_user.id)
            user["subscribed"] = 1
    if not user["subscribed"]:
        await check_subscription_flow(message, user["id"])
        return
    await show_main_menu(message)


@router.message(CaptchaState.waiting_answer)
async def captcha_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("captcha_code", "")
    if (message.text or "").strip() == correct:
        await state.clear()
        await set_captcha_passed(message.from_user.id)
        user = await get_user(message.from_user.id)
        if not user["subscribed"]:
            if await _check_subscribed_via_api(message.bot, message.from_user.id):
                await set_subscribed(message.from_user.id)
                user["subscribed"] = 1
        if not user["subscribed"]:
            await check_subscription_flow(message, message.from_user.id)
        else:
            await show_main_menu(message)
    else:
        await message.answer("❌ Неверно, попробуй ещё раз:")
        await send_captcha(message, state)


@router.callback_query(F.data == "check_sub")
async def check_subscription(call: CallbackQuery):
    try:
        member = await call.bot.get_chat_member(CHANNEL_ID, call.from_user.id)
        is_member = member.status not in ("left", "kicked", "banned")
    except Exception:
        is_member = True
    if is_member:
        await set_subscribed(call.from_user.id)
        await call.answer("✅ Подписка подтверждена!")
        await show_main_menu(call)
    else:
        await call.answer("❌ Ты не подписан на канал!", show_alert=True)


@router.callback_query(F.data == "back_main")
async def back_to_main(call: CallbackQuery):
    await show_main_menu(call)
    await call.answer()

# ── CATALOG ───────────────────────────────────────────────────────────────────

@router.message(F.text == "💎 Ассортимент")
async def catalog_main(message: Message):
    categories = await get_categories()
    if not categories:
        await message.answer("❌ Товаров пока нет.")
        return
    await message.answer("🛍 <b>Ассортимент</b>", reply_markup=categories_kb(categories), parse_mode="HTML")


@router.callback_query(F.data == "back_cats")
async def back_to_categories(call: CallbackQuery):
    categories = await get_categories()
    if not categories:
        await call.answer("Категорий нет", show_alert=True)
        return
    await call.message.edit_text("🛍 <b>Ассортимент</b>", reply_markup=categories_kb(categories), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("cat:"))
async def show_category(call: CallbackQuery):
    cat_id = int(call.data.split(":")[1])
    cat = await get_category(cat_id)
    if not cat:
        await call.answer("Категория не найдена", show_alert=True)
        return
    subcats = await get_subcategories(cat_id)
    if not subcats:
        await call.answer("В этой категории пока нет товаров.", show_alert=True)
        return
    desc = cat.get("description", "").strip()
    text = f"<b>{cat['name']}</b>"
    if desc:
        text += f"\n<i>{desc}</i>"
    await call.message.edit_text(text, reply_markup=subcategories_kb(subcats, cat_id), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("sub:"))
async def show_subcategory(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sub_id = int(call.data.split(":")[1])
    sub = await get_subcategory(sub_id)
    if not sub:
        await call.answer("Подкатегория не найдена", show_alert=True)
        return
    stock = sub.get("stock", 0)
    price = sub.get("price", 0)
    desc = sub.get("description", "").strip()
    text = f"<b>{sub['name']}</b>\n"
    if desc:
        text += f"<i>{desc}</i>\n"
    text += f"\n💵 {price:.2f} USDT / шт."
    text += f"\n✅ В наличии: {stock} шт." if stock > 0 else "\n❌ Нет в наличии"
    await call.message.edit_text(text, reply_markup=subcategory_buy_kb(sub_id, stock), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("back_sub:"))
async def back_from_subcat(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sub_id = int(call.data.split(":")[1])
    sub = await get_subcategory(sub_id)
    if not sub:
        await call.answer("Ошибка", show_alert=True)
        return
    cat_id = sub["category_id"]
    cat = await get_category(cat_id)
    subcats = await get_subcategories(cat_id)
    desc = cat.get("description", "").strip() if cat else ""
    text = f"<b>{cat['name'] if cat else ''}</b>"
    if desc:
        text += f"\n<i>{desc}</i>"
    await call.message.edit_text(text, reply_markup=subcategories_kb(subcats, cat_id), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_confirm(call: CallbackQuery):
    parts = call.data.split(":")
    sub_id, qty = int(parts[1]), int(parts[2])
    sub = await get_subcategory(sub_id)
    if not sub:
        await call.answer("Подкатегория не найдена", show_alert=True)
        return
    stock = sub.get("stock", 0)
    if stock < qty:
        await call.answer(f"❌ В наличии: {stock} шт." if stock > 0 else "❌ Товар закончился!", show_alert=True)
        return
    user = await get_or_create_user(call.from_user.id, call.from_user.username or "", call.from_user.full_name or "")
    price_each = sub["price"]
    total = price_each * qty
    balance_ok = user["balance"] >= total
    text = (
        f"🛒 <b>{sub['name']}</b>\n"
        f"{qty} шт. × {price_each:.2f}$ = <b>{total:.2f} USDT</b>\n"
        f"Баланс: {user['balance']:.2f}$\n\n"
        f"{'✅ Достаточно средств' if balance_ok else '❌ Недостаточно средств'}"
    )
    await call.message.edit_text(text, reply_markup=confirm_buy_kb(sub_id, qty), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("buy_custom:"))
async def buy_custom_qty(call: CallbackQuery, state: FSMContext):
    sub_id = int(call.data.split(":")[1])
    sub = await get_subcategory(sub_id)
    if not sub:
        await call.answer("Подкатегория не найдена", show_alert=True)
        return
    stock = sub.get("stock", 0)
    max_allowed = min(stock, MAX_QTY)
    await state.set_state(CatalogStates.waiting_custom_qty)
    await state.update_data(sub_id=sub_id)
    await call.message.edit_text(
        f"✏️ <b>Своё количество</b>\n\n"
        f"В наличии: <b>{stock} шт.</b> | Максимум: <b>{MAX_QTY} шт.</b>\n\n"
        f"Отправь число от <b>1</b> до <b>{max_allowed}</b>:",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(CatalogStates.waiting_custom_qty)
async def custom_qty_input(message: Message, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("sub_id")
    try:
        qty = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи целое число, например: <code>7</code>", parse_mode="HTML")
        return
    if qty < 1:
        await message.answer("❌ Минимум — <b>1 шт.</b>", parse_mode="HTML")
        return
    if qty > MAX_QTY:
        await message.answer(f"❌ Максимум — <b>{MAX_QTY} шт.</b>", parse_mode="HTML")
        return
    sub = await get_subcategory(sub_id)
    if not sub:
        await state.clear()
        await message.answer("❌ Товар не найден.")
        return
    stock = sub.get("stock", 0)
    if qty > stock:
        await message.answer(f"❌ В наличии: <b>{stock} шт.</b> Введи до <b>{min(stock, MAX_QTY)}</b>:", parse_mode="HTML")
        return
    await state.clear()
    user = await get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    price_each = sub["price"]
    total = price_each * qty
    balance_ok = user["balance"] >= total
    text = (
        f"🛒 <b>{sub['name']}</b>\n"
        f"{qty} шт. × {price_each:.2f}$ = <b>{total:.2f} USDT</b>\n"
        f"Баланс: {user['balance']:.2f}$\n\n"
        f"{'✅ Достаточно средств' if balance_ok else '❌ Недостаточно средств'}"
    )
    await message.answer(text, reply_markup=confirm_buy_kb(sub_id, qty), parse_mode="HTML")


@router.callback_query(F.data.startswith("confirm_buy:"))
async def confirm_purchase(call: CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split(":")
    sub_id, qty = int(parts[1]), int(parts[2])
    if user_id in _purchasing:
        await call.answer("⏳ Покупка уже обрабатывается...", show_alert=False)
        return
    _purchasing.add(user_id)
    try:
        result = await atomic_purchase(user_id, sub_id, qty)
        if result["error"] == "balance":
            await call.answer("❌ Недостаточно средств! Пополни баланс.", show_alert=True)
            return
        if result["error"] == "stock":
            sub = await get_subcategory(sub_id)
            stock = sub.get("stock", 0) if sub else 0
            await call.answer(f"❌ В наличии: {stock} шт." if stock > 0 else "❌ Товар закончился!", show_alert=True)
            return
        if result["error"] in ("no_product", "internal"):
            await call.answer("❌ Ошибка. Обратись в поддержку.", show_alert=True)
            return
        order_id = result["order_id"]
        contents = result["contents"]
        qty_bought = result["qty"]
        total = result["total"]
        sub_name = result["subcategory_name"]
        cat_name = result["category_name"]
        file_lines = [
            f"Заказ: {order_id}",
            f"Товар: {cat_name} — {sub_name}",
            f"Количество: {qty_bought} шт.",
            f"Итого: {total:.2f} USDT",
            "", "=" * 30, "",
        ]
        for i, content in enumerate(contents, 1):
            file_lines.append(f"#{i}: {content}")
        file = BufferedInputFile("\n".join(file_lines).encode("utf-8"), filename=f"order_{order_id}.txt")
        caption = (
            f"✅ <b>Оплачено</b>\n"
            f"{cat_name} — {sub_name}\n"
            f"{qty_bought} шт. · {total:.2f} USDT\n"
            f"Заказ: <code>{order_id}</code>\n\n"
            f"📄 Товар в файле · проверка 1 час\n"
            f"💬 Вопросы: @aiviproj"
        )
        await call.message.answer_document(file, caption=caption, parse_mode="HTML")
        await call.message.delete()
        await call.answer()
    finally:
        _purchasing.discard(user_id)

# ── PROFILE ───────────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Профиль")
async def profile_handler(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    orders = await get_user_orders(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"📛 Имя: <b>{message.from_user.full_name}</b>\n"
        f"🔗 Username: {username}\n\n"
        f"💰 Баланс: <b>{user['balance']:.2f}$</b>\n"
        f"💸 Потрачено всего: <b>{user['total_spent']:.2f}$</b>\n"
        f"📦 Заказов: <b>{len(orders)}</b>\n\n"
        f"📅 В боте с: <b>{user['registered_at'][:10]}</b>"
    )
    await message.answer(text, parse_mode="HTML")

# ── ORDERS ────────────────────────────────────────────────────────────────────

@router.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message):
    orders = await get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("📦 Заказов пока нет.")
        return
    text = "📦 <b>Мои заказы</b>\n\n"
    for order in orders[:10]:
        text += (
            f"🆔 <code>{order['id']}</code>\n"
            f"📁 {order.get('category_name', '')} → {order['subcategory_name']}\n"
            f"💵 {order['total']:.2f}$\n"
            f"📅 {order['created_at'][:16]}\n"
            f"{'─' * 20}\n"
        )
    if len(orders) > 10:
        text += f"\n<i>Последние 10 из {len(orders)} заказов</i>"
    await message.answer(text, parse_mode="HTML")

# ── SUPPORT ───────────────────────────────────────────────────────────────────

@router.message(F.text == "💬 Поддержка")
async def support_handler(message: Message):
    await message.answer("💬 <b>Поддержка:</b> @aiviproj", parse_mode="HTML")

# ── TOPUP ─────────────────────────────────────────────────────────────────────

@router.message(F.text == "💳 Пополнить баланс")
async def topup_main(message: Message):
    await message.answer(
        "💳 <b>Пополнение</b> — оплата через CryptoBot (USDT)",
        reply_markup=topup_amounts_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("topup_amount:"))
async def topup_amount_selected(call: CallbackQuery, state: FSMContext):
    amount = float(call.data.split(":")[1])
    await _process_topup(call, state, amount)


@router.callback_query(F.data == "topup_custom")
async def topup_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(TopupStates.waiting_custom_amount)
    await call.message.edit_text(f"✏️ Введи сумму от <b>{MIN_TOPUP}$</b>:", parse_mode="HTML")
    await call.answer()


@router.message(TopupStates.waiting_custom_amount)
async def topup_custom_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например: <code>15</code>", parse_mode="HTML")
        return
    if amount < MIN_TOPUP:
        await message.answer(f"❌ Минимальная сумма: <b>{MIN_TOPUP}$</b>", parse_mode="HTML")
        return
    await state.clear()
    msg = await message.answer("⏳ Создаю счёт...", parse_mode="HTML")
    await _create_and_send_invoice(message, msg, amount)


async def _process_topup(call, state, amount):
    await call.answer()
    await state.clear()
    msg = await call.message.edit_text("⏳ Создаю счёт...", parse_mode="HTML")
    await _create_and_send_invoice(call, msg, amount)


async def _create_and_send_invoice(target, msg, amount):
    user_id = target.from_user.id
    if user_id in _invoice_creating:
        await msg.edit_text("⏳ Счёт уже создаётся, подождите.")
        return
    _invoice_creating.add(user_id)
    try:
        existing = await get_pending_invoice_for_user(user_id)
        if existing:
            pay_url = f"https://t.me/CryptoBot?start=IV{existing['crypto_invoice_id']}"
            await msg.edit_text(
                f"⚠️ <b>Активный счёт</b>: {existing['amount']} USDT\n<code>{existing['id']}</code>",
                reply_markup=pay_invoice_kb(pay_url, existing["id"]), parse_mode="HTML",
            )
            return
        invoice_id = str(uuid.uuid4())[:8].upper()
        result = await cryptobot_create_invoice(amount, f"topup:{user_id}:{invoice_id}")
        if not result:
            await msg.edit_text("❌ Не удалось создать счёт. Попробуй позже.")
            return
        await create_invoice(invoice_id, user_id, amount, result["invoice_id"])
        await msg.edit_text(
            f"💳 <b>{amount} USDT</b> · <code>{invoice_id}</code>\n\n"
            f"Нажми <b>Оплатить</b>, затем <b>Проверить оплату</b>",
            reply_markup=pay_invoice_kb(result["pay_url"], invoice_id), parse_mode="HTML",
        )
    finally:
        _invoice_creating.discard(user_id)


@router.callback_query(F.data.startswith("check_pay:"))
async def check_payment(call: CallbackQuery):
    invoice_id = call.data.split(":")[1]
    if invoice_id in _payment_checking:
        await call.answer("⏳ Проверка уже идёт...", show_alert=False)
        return
    _payment_checking.add(invoice_id)
    try:
        invoice = await get_invoice(invoice_id)
        if not invoice:
            await call.answer("❌ Счёт не найден", show_alert=True)
            return
        if invoice["status"] == "paid":
            await call.answer("✅ Этот счёт уже был оплачен!", show_alert=True)
            return
        if invoice["user_id"] != call.from_user.id:
            await call.answer("❌ Это не ваш счёт.", show_alert=True)
            return
        status = await cryptobot_check_invoice(invoice["crypto_invoice_id"])
        if status == "paid":
            success = await mark_invoice_paid_and_credit(invoice_id, invoice["user_id"], invoice["amount"])
            if not success:
                await call.answer("✅ Оплата уже была зачислена!", show_alert=True)
                return
            user = await get_user(call.from_user.id)
            await call.message.edit_text(
                f"✅ <b>+{invoice['amount']}$</b> — баланс: <b>{user['balance']:.2f}$</b>",
                parse_mode="HTML",
            )
            await call.answer("✅ Баланс пополнен!", show_alert=True)
        elif status == "expired":
            await mark_invoice_expired(invoice_id)
            await call.answer("❌ Счёт истёк. Создай новый.", show_alert=True)
        else:
            await call.answer("⏳ Оплата ещё не поступила. Попробуй позже.", show_alert=True)
    finally:
        _payment_checking.discard(invoice_id)


@router.callback_query(F.data == "cancel_pay")
async def cancel_payment(call: CallbackQuery):
    await cancel_pending_invoices(call.from_user.id)
    await call.message.edit_text("❌ Пополнение отменено.", parse_mode="HTML")
    await call.answer()

# ── ADMIN ─────────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return
    await show_admin(message, state)


@router.callback_query(F.data == "adm:back")
async def adm_back(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await show_admin(call, state)


@router.callback_query(F.data == "adm:add_cat")
async def adm_add_cat(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_cat_name)
    await call.message.edit_text("📁 <b>Новая категория</b>\n\nОтправь <b>название</b>:", reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.message(AdminStates.waiting_cat_name)
async def adm_cat_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым!")
        return
    await state.update_data(cat_name=name)
    await state.set_state(AdminStates.waiting_cat_desc)
    await message.answer(f"📝 Название: <b>{name}</b>\n\nОтправь <b>описание</b> (или <code>-</code> чтобы пропустить):", parse_mode="HTML")


@router.message(AdminStates.waiting_cat_desc)
async def adm_cat_desc(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    desc = "" if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    await add_category(data["cat_name"], desc)
    await state.clear()
    await message.answer(
        f"✅ <b>Категория создана!</b>\n\n📁 <b>{data['cat_name']}</b>\n📝 {desc or '<i>без описания</i>'}",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:add_subcat")
async def adm_add_subcat(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    categories = await get_categories()
    if not categories:
        await call.answer("❌ Сначала создай категорию!", show_alert=True)
        return
    await call.message.edit_text("📂 <b>Новая подкатегория</b>\n\nВыбери категорию:", reply_markup=admin_cats_kb(categories, "sel_cat_subcat"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:sel_cat_subcat:"))
async def adm_sel_cat_subcat(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    cat_id = int(call.data.split(":")[2])
    cat = await get_category(cat_id)
    await state.update_data(cat_id=cat_id)
    await state.set_state(AdminStates.waiting_subcat_name)
    await call.message.edit_text(f"📂 <b>«{cat['name'] if cat else cat_id}»</b>\n\nШаг 1/3 — <b>название</b>:", parse_mode="HTML")
    await call.answer()


@router.message(AdminStates.waiting_subcat_name)
async def adm_subcat_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым!")
        return
    await state.update_data(subcat_name=name)
    await state.set_state(AdminStates.waiting_subcat_desc)
    await message.answer(f"📝 Название: <b>{name}</b>\n\nШаг 2/3 — <b>описание</b> (или <code>-</code>):", parse_mode="HTML")


@router.message(AdminStates.waiting_subcat_desc)
async def adm_subcat_desc(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    desc = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(subcat_desc=desc)
    await state.set_state(AdminStates.waiting_subcat_price)
    await message.answer("💵 Шаг 3/3 — <b>цена</b> за 1 единицу (например: <code>9.99</code>):", parse_mode="HTML")


@router.message(AdminStates.waiting_subcat_price)
async def adm_subcat_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи корректную цену, например: <code>9.99</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    await add_subcategory(data["cat_id"], data["subcat_name"], data.get("subcat_desc", ""), price)
    await state.clear()
    await message.answer(
        f"✅ <b>Подкатегория создана!</b>\n\n<b>{data['subcat_name']}</b>\n💵 Цена: <b>{price}$</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:add_items")
async def adm_add_items(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    categories = await get_categories()
    if not categories:
        await call.answer("❌ Сначала создай категорию!", show_alert=True)
        return
    await call.message.edit_text("🗃 <b>Добавить единицы</b>\n\nВыбери категорию:", reply_markup=admin_cats_kb(categories, "sel_cat_items"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:sel_cat_items:"))
async def adm_sel_cat_items(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    cat_id = int(call.data.split(":")[2])
    subcats = await get_subcategories(cat_id)
    if not subcats:
        await call.answer("❌ В этой категории нет подкатегорий!", show_alert=True)
        return
    cat = await get_category(cat_id)
    await call.message.edit_text(f"🗃 <b>«{cat['name'] if cat else cat_id}»</b>\n\nВыбери подкатегорию:", reply_markup=admin_subcats_kb(subcats, cat_id, "sel_sub_items"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:sel_sub_items:"))
async def adm_sel_sub_items(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    sub_id = int(call.data.split(":")[2])
    sub = await get_subcategory(sub_id)
    await state.update_data(sub_id=sub_id)
    await state.set_state(AdminStates.waiting_items_content)
    await call.message.edit_text(
        f"🗃 <b>«{sub['name'] if sub else sub_id}»</b>\n\nКаждая единица — отдельная строка:\n\n"
        f"<code>логин1:пароль1\nлогин2:пароль2</code>",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.waiting_items_content)
async def adm_items_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    sub_id = data.get("sub_id")
    lines = [line for line in message.text.split("\n") if line.strip()]
    if not lines:
        await message.answer("❌ Нет данных для добавления!")
        return
    await add_items_to_subcategory(sub_id, lines)
    await state.clear()
    sub = await get_subcategory(sub_id)
    await message.answer(
        f"✅ <b>Добавлено {len(lines)} единиц!</b>\n\n"
        f"<b>{sub['name'] if sub else sub_id}</b> — всего: <b>{sub['stock'] if sub else '?'} шт.</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:manage")
async def adm_manage(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    categories = await get_categories()
    if not categories:
        await call.message.edit_text("📂 <b>Категорий нет</b>", reply_markup=back_admin_kb(), parse_mode="HTML")
        await call.answer()
        return
    await call.message.edit_text("🗂 <b>Управление</b>\n\nВыбери категорию:", reply_markup=admin_manage_cats_kb(categories), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:manage_cat:"))
async def adm_manage_cat(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    cat_id = int(call.data.split(":")[2])
    cat = await get_category(cat_id)
    if not cat:
        await call.answer("Категория не найдена", show_alert=True)
        return
    subcats = await get_subcategories(cat_id)
    total_stock = sum(s.get("stock", 0) for s in subcats)
    text = (
        f"📁 <b>{cat['name']}</b>\n"
        f"📝 {cat.get('description') or '<i>без описания</i>'}\n"
        f"📂 Подкатегорий: <b>{len(subcats)}</b>\n"
        f"🗃 Всего единиц: <b>{total_stock}</b>"
    )
    await call.message.edit_text(text, reply_markup=admin_manage_cat_kb(cat_id), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:manage_subs:"))
async def adm_manage_subs(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    cat_id = int(call.data.split(":")[2])
    cat = await get_category(cat_id)
    subcats = await get_subcategories(cat_id)
    if not subcats:
        await call.message.edit_text(f"📂 В «{cat['name'] if cat else cat_id}» нет подкатегорий.", reply_markup=back_admin_kb(), parse_mode="HTML")
        await call.answer()
        return
    await call.message.edit_text(f"📂 <b>«{cat['name'] if cat else cat_id}»</b>\n\nВыбери подкатегорию:", reply_markup=admin_subcats_kb(subcats, cat_id, "manage_sub_detail"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:manage_sub_detail:"))
async def adm_manage_sub_detail(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    sub_id = int(call.data.split(":")[2])
    sub = await get_subcategory(sub_id)
    if not sub:
        await call.answer("Подкатегория не найдена", show_alert=True)
        return
    cat_id = sub["category_id"]
    text = (
        f"<b>{sub['name']}</b>\n"
        f"📝 {sub.get('description') or '<i>без описания</i>'}\n"
        f"💵 Цена: <b>{sub['price']:.2f}$</b>\n"
        f"🗃 В наличии: <b>{sub['stock']} шт.</b>"
    )
    await call.message.edit_text(text, reply_markup=admin_manage_sub_kb(sub_id, cat_id), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:del_cat:"))
async def adm_del_cat(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    cat_id = int(call.data.split(":")[2])
    cat = await get_category(cat_id)
    name = cat["name"] if cat else "?"
    await delete_category(cat_id)
    await call.message.edit_text(f"🗑 <b>Категория «{name}» удалена.</b>", reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("adm:del_sub:"))
async def adm_del_sub(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    parts = call.data.split(":")
    sub_id, cat_id = int(parts[2]), int(parts[3])
    sub = await get_subcategory(sub_id)
    name = sub["name"] if sub else "?"
    await delete_subcategory(sub_id)
    await call.message.edit_text(f"🗑 <b>Подкатегория «{name}» удалена.</b>", reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:analytics")
async def adm_analytics(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    stats = await get_analytics()
    text = (
        "📊 <b>Аналитика</b>\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b> (сегодня +{stats['new_users_today']})\n"
        f"📦 Заказов: <b>{stats['total_orders']}</b> (сегодня {stats['orders_today']})\n"
        f"💰 Выручка: <b>{stats['total_revenue']:.2f}$</b> (сегодня {stats['revenue_today']:.2f}$)\n"
        f"🗃 Единиц в наличии: <b>{stats['items_in_stock']}</b>"
    )
    await call.message.edit_text(text, reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "adm:add_balance")
async def adm_add_balance(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_balance_user_id)
    await call.message.edit_text("💰 <b>Пополнить баланс</b>\n\nОтправь <b>Telegram ID</b> пользователя:", parse_mode="HTML")
    await call.answer()


@router.message(AdminStates.waiting_balance_user_id)
async def adm_balance_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи числовой Telegram ID!")
        return
    user = await get_user(uid)
    if not user:
        await message.answer("❌ Пользователь не найден!")
        return
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminStates.waiting_balance_amount)
    await message.answer(f"👤 <b>{user['full_name']}</b>\n💰 Баланс: <b>{user['balance']:.2f}$</b>\n\nВведи сумму:", parse_mode="HTML")


@router.message(AdminStates.waiting_balance_amount)
async def adm_balance_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи корректную сумму, например: <code>10</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    uid = data["target_user_id"]
    await update_balance(uid, amount)
    user = await get_user(uid)
    await state.clear()
    await message.answer(
        f"✅ <b>Баланс пополнен!</b>\n\n👤 <b>{user['full_name']}</b> (<code>{uid}</code>)\n"
        f"💵 +{amount}$ → <b>{user['balance']:.2f}$</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )
    try:
        await message.bot.send_message(
            uid,
            f"💰 <b>+{amount}$</b> — баланс: <b>{user['balance']:.2f}$</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    await call.message.edit_text("📣 <b>Рассылка</b>\n\nОтправь сообщение для всех пользователей:", parse_mode="HTML")
    await call.answer()


@router.message(AdminStates.waiting_broadcast_text)
async def adm_broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    user_ids = await get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"📣 Рассылка на <b>{len(user_ids)}</b> пользователей...", parse_mode="HTML")
    for uid in user_ids:
        try:
            await message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n📤 Отправлено: <b>{sent}</b>\n❌ Не доставлено: <b>{failed}</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    stats = await get_analytics()
    await call.message.edit_text(
        f"👥 <b>Пользователи</b>\n\nВсего: <b>{stats['total_users']}</b>\nНовых сегодня: <b>{stats['new_users_today']}</b>",
        reply_markup=back_admin_kb(), parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "adm:export_users")
async def adm_export_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer("⏳ Формирую файл...")
    users = await get_all_users_with_stats()
    if not users:
        await call.message.answer("❌ Нет пользователей в базе.")
        return
    lines = ["=" * 54, f"  БАЗА ПОЛЬЗОВАТЕЛЕЙ — {len(users)} чел.", "=" * 54]
    for i, u in enumerate(users, 1):
        username = f"@{u['username']}" if u["username"] else "—"
        reg = u["registered_at"][:10] if u["registered_at"] else "—"
        lines.append(f"\n#{i}")
        lines.append(f"  Имя:       {u['full_name'] or '—'}")
        lines.append(f"  Username:  {username}")
        lines.append(f"  ID:        {u['id']}")
        lines.append(f"  Баланс:    {u['balance']:.2f} $")
        lines.append(f"  Покупок:   {u['purchases']}")
        lines.append(f"  Дата рег.: {reg}")
        lines.append("  " + "-" * 38)
    buf = BytesIO("\n".join(lines).encode("utf-8"))
    filename = f"users_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    await call.message.answer_document(
        BufferedInputFile(buf.getvalue(), filename=filename),
        caption=f"📥 <b>База пользователей</b> — <b>{len(users)}</b> чел.",
        parse_mode="HTML",
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    logger.info("Database initialized")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Starting bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
