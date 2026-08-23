"""
Zaxira Bot - Mebel ustaxonasi uchun mahsulot kirim/chiqim va qoldiq kuzatuv boti.

Mahsulot nomi har doim shu tartibda yoziladi: <model> <detal>
Masalan: "laura tumba", "vena shkaf". Bot birinchi so'zni model deb,
qolganini detal deb tushunadi va shunga qarab guruhlaydi.

Buyruqlar:
  /kirim <model> <detal> <miqdor>   - mahsulot keldi (zaxiraga qo'shiladi)
  /chiqim <model> <detal> <miqdor>  - mahsulot ketdi/sotildi (zaxiradan ayiriladi)
  /qoldiq                            - barcha mahsulotlar qoldig'ini ko'rsatadi
  /qoldiq <model> <detal>            - bitta mahsulot qoldig'ini ko'rsatadi
  /modellar                          - modellar ro'yxatini tugmalar bilan ko'rsatadi,
                                        bosilganda o'sha model bo'yicha barcha
                                        detallar va qoldig'ini chiqaradi
  /tarix <model> <detal> [soni]      - mahsulot bo'yicha oxirgi harakatlar tarixi
  /ochir <model> <detal>             - mahsulotni ro'yxatdan o'chiradi (ehtiyot bo'ling)
  /royxatga                          - bir nechta modelni bir martada ro'yxatga
                                        qo'shadi (miqdori 0 dan boshlanadi), format:
                                        /royxatga
                                        laura: shkaf, tumba, krovat, kamod, parta
                                        vena: shkaf, tumba, krovat, kamod, parta
  /yordam yoki /start                - yordam matni

Faqat egasi (OWNER_ID muhit o'zgaruvchisida ko'rsatilgan foydalanuvchi)
/kirim, /chiqim va /ochir buyruqlaridan foydalana oladi. Boshqa hamma
(butun jamoa) faqat /qoldiq, /modellar va /tarix orqali ko'rib turadi,
o'zgartira olmaydi. Agar OWNER_ID sozlanmagan bo'lsa, cheklov ishlamaydi
(hamma hamma narsani qila oladi).
"""

import logging
import json
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Railway'da /data volume ulangan bo'lsa, baza o'sha doimiy papkada saqlanadi
# (shunda deploy/qayta ishga tushganda ma'lumot o'chib qolmaydi). Aks holda
# botning o'z papkasida saqlanadi (mahalliy sinov uchun yetarli).
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "zaxira.db")

_owner_id_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(_owner_id_raw) if _owner_id_raw.isdigit() else None

_group_chat_id_raw = os.environ.get("GROUP_CHAT_ID", "").strip()
GROUP_CHAT_ID = int(_group_chat_id_raw) if _group_chat_id_raw.lstrip("-").isdigit() else None

_worker_chat_id_raw = os.environ.get("WORKER_CHAT_ID", "").strip()
WORKER_CHAT_ID = int(_worker_chat_id_raw) if _worker_chat_id_raw.lstrip("-").isdigit() else None

LOW_STOCK_THRESHOLD = 3
TASHKENT_TZ = timezone(timedelta(hours=5))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            model TEXT NOT NULL COLLATE NOCASE DEFAULT '',
            item TEXT NOT NULL COLLATE NOCASE DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL,
            change_type TEXT NOT NULL,  -- 'kirim' or 'chiqim'
            amount INTEGER NOT NULL,
            user_name TEXT,
            user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guruh_id INTEGER,                   -- bitta seansda birga yaratilgan buyurtmalar guruhi
            model TEXT NOT NULL COLLATE NOCASE,
            item TEXT COLLATE NOCASE,          -- NULL bo'lsa - butun komplekt
            amount INTEGER NOT NULL,
            deadline TEXT NOT NULL,             -- ISO sana: YYYY-MM-DD
            deadline_display TEXT NOT NULL,     -- masalan '5 avgust'
            customer TEXT,
            status TEXT NOT NULL DEFAULT 'kutilmoqda',  -- yoki 'bajarildi'
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute("PRAGMA table_info(orders)")
    orders_columns = {row[1] for row in cur.fetchall()}
    if "guruh_id" not in orders_columns:
        cur.execute("ALTER TABLE orders ADD COLUMN guruh_id INTEGER")
        # Eski buyurtmalar - har biri o'zining alohida guruhi (o'z ID'si bilan).
        cur.execute("UPDATE orders SET guruh_id = id WHERE guruh_id IS NULL")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS komplekt_tarkibi (
            model TEXT NOT NULL COLLATE NOCASE,
            item TEXT NOT NULL COLLATE NOCASE,
            soni INTEGER NOT NULL,
            PRIMARY KEY (model, item)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS narxlar (
            turi TEXT NOT NULL COLLATE NOCASE,     -- 'upakovka' yoki 'yigish'
            model TEXT NOT NULL COLLATE NOCASE DEFAULT '',  -- '' = barcha modellar uchun umumiy
            item TEXT NOT NULL COLLATE NOCASE,     -- detal nomi, yoki 'komplekt'
            rate INTEGER NOT NULL,
            PRIMARY KEY (turi, model, item)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_accounts (
            telegram_id INTEGER PRIMARY KEY,
            worker_name TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS work_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker TEXT NOT NULL COLLATE NOCASE,
            turi TEXT NOT NULL COLLATE NOCASE,  -- 'upakovka' yoki 'yigish'
            order_id INTEGER,
            model TEXT,
            item TEXT,
            amount INTEGER NOT NULL,
            rate INTEGER NOT NULL,
            total INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    # Eski bazalarda 'turi' ustuni bo'lmasligi mumkin - qo'shib olamiz.
    cur.execute("PRAGMA table_info(work_log)")
    work_log_columns = {row[1] for row in cur.fetchall()}
    if "turi" not in work_log_columns:
        cur.execute("ALTER TABLE work_log ADD COLUMN turi TEXT NOT NULL COLLATE NOCASE DEFAULT 'yigish'")

    cur.execute("PRAGMA table_info(narxlar)")
    narxlar_columns = {row[1] for row in cur.fetchall()}
    if "turi" not in narxlar_columns:
        # Eski (turi'siz) narxlar jadvali bo'lsa, uni 'yigish' turi bilan qayta quramiz.
        cur.execute("ALTER TABLE narxlar RENAME TO narxlar_old")
        cur.execute(
            """
            CREATE TABLE narxlar (
                turi TEXT NOT NULL COLLATE NOCASE,
                model TEXT NOT NULL COLLATE NOCASE DEFAULT '',
                item TEXT NOT NULL COLLATE NOCASE,
                rate INTEGER NOT NULL,
                PRIMARY KEY (turi, model, item)
            )
            """
        )
        cur.execute(
            "INSERT INTO narxlar (turi, model, item, rate) SELECT 'yigish', '', item, rate FROM narxlar_old"
        )
        cur.execute("DROP TABLE narxlar_old")
    elif "model" not in narxlar_columns:
        # 'turi' bor, lekin 'model' yo'q - shuni qo'shib, mavjud narxlarni umumiy ('') deb belgilaymiz.
        cur.execute("ALTER TABLE narxlar RENAME TO narxlar_old")
        cur.execute(
            """
            CREATE TABLE narxlar (
                turi TEXT NOT NULL COLLATE NOCASE,
                model TEXT NOT NULL COLLATE NOCASE DEFAULT '',
                item TEXT NOT NULL COLLATE NOCASE,
                rate INTEGER NOT NULL,
                PRIMARY KEY (turi, model, item)
            )
            """
        )
        cur.execute(
            "INSERT INTO narxlar (turi, model, item, rate) SELECT turi, '', item, rate FROM narxlar_old"
        )
        cur.execute("DROP TABLE narxlar_old")

    # Boshlang'ich ishchilar (agar hali qo'shilmagan bo'lsa).
    for default_worker in ("Hojiakbar", "Abdulloh"):
        cur.execute(
            "INSERT OR IGNORE INTO workers (name, created_at) VALUES (?, ?)",
            (default_worker, datetime.now().isoformat(timespec="seconds")),
        )

    # Eski bazalarda 'model' va 'item' ustunlari bo'lmasligi mumkin - qo'shib olamiz.
    cur.execute("PRAGMA table_info(products)")
    existing_columns = {row[1] for row in cur.fetchall()}
    if "model" not in existing_columns:
        cur.execute("ALTER TABLE products ADD COLUMN model TEXT NOT NULL COLLATE NOCASE DEFAULT ''")
    if "item" not in existing_columns:
        cur.execute("ALTER TABLE products ADD COLUMN item TEXT NOT NULL COLLATE NOCASE DEFAULT ''")

    # Model/item bo'sh qolgan (eski) yozuvlarni 'name' asosida to'ldiramiz.
    cur.execute("SELECT name FROM products WHERE model = '' OR item = ''")
    for (name,) in cur.fetchall():
        model, item = split_model_item(name)
        cur.execute(
            "UPDATE products SET model = ?, item = ? WHERE name = ?",
            (model, item or name, name),
        )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_group_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT,
            item TEXT,
            amount INTEGER,
            deadline TEXT,
            deadline_display TEXT,
            customer TEXT,
            raw_text TEXT,
            source_chat_id INTEGER,
            status TEXT NOT NULL DEFAULT 'kutilmoqda',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute("PRAGMA table_info(pending_group_orders)")
    pending_columns = {row[1] for row in cur.fetchall()}
    if "entries_json" not in pending_columns:
        cur.execute("ALTER TABLE pending_group_orders ADD COLUMN entries_json TEXT")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mijoz_tolovlar (
            guruh_id INTEGER PRIMARY KEY,
            customer TEXT NOT NULL,
            expected_value INTEGER NOT NULL,
            received_amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def get_setting(key: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key: str, value: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def normalize_product_name(name: str) -> str:
    return name.strip().lower()


def split_model_item(product_display: str):
    """'laura tumba' -> ('laura', 'tumba'). Bitta so'z bo'lsa item bo'sh qoladi."""
    parts = product_display.strip().split(maxsplit=1)
    model = parts[0] if parts else ""
    item = parts[1] if len(parts) > 1 else ""
    return model.lower(), item.lower()


MONTH_NAMES = {
    "yanvar": 1,
    "fevral": 2,
    "mart": 3,
    "aprel": 4,
    "may": 5,
    "iyun": 6,
    "iyul": 7,
    "avgust": 8,
    "sentyabr": 9,
    "oktyabr": 10,
    "noyabr": 11,
    "dekabr": 12,
}


def parse_amount(raw: str) -> int:
    amount = int(raw)
    if amount <= 0:
        raise ValueError("Miqdor musbat son bo'lishi kerak")
    return amount


def is_owner(update: Update) -> bool:
    if OWNER_ID is None:
        return True
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


def get_linked_worker(update: Update):
    """Agar shu Telegram foydalanuvchisi biror ishchiga bog'langan bo'lsa, ismini qaytaradi."""
    user = update.effective_user
    if not user:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT worker_name FROM worker_accounts WHERE telegram_id = ?", (user.id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def can_kirim(update: Update) -> bool:
    """Kirim (upakovka) qilish huquqi: egasi yoki bog'langan ishchi."""
    return is_owner(update) or get_linked_worker(update) is not None


async def deny_access(update: Update):
    await update.message.reply_text(
        "Kechirasiz, faqat egasi mahsulot kirim/chiqim/o'chirish qila oladi. "
        "Siz /qoldiq, /modellar va /tarix orqali ko'rib turishingiz mumkin."
    )


MENU_BUTTONS = {
    "qoldiq": "📦 Qoldiq",
    "modellar": "📋 Modellar",
    "buyurtmalar": "📝 Buyurtmalar",
    "yordam": "❓ Yordam",
    "kirim": "📥 Kirim",
    "chiqim": "📤 Chiqim",
    "yangi_buyurtma": "🆕 Buyurtma",
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [MENU_BUTTONS["kirim"], MENU_BUTTONS["chiqim"]],
        [MENU_BUTTONS["qoldiq"], MENU_BUTTONS["modellar"]],
        [MENU_BUTTONS["yangi_buyurtma"], MENU_BUTTONS["buyurtmalar"]],
        [MENU_BUTTONS["yordam"]],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

FINISH_BUTTON = "✅ Tayyor"
FINISH_MENU = ReplyKeyboardMarkup([[FINISH_BUTTON]], resize_keyboard=True, is_persistent=True)


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: {chat.id}\nTuri: {chat.type}"
    )


async def buyurtmaguruhi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Bu buyruqni buyurtma qabul qilinadigan GURUH ichida yuboring "
            "(masalan 'ZAKAZ MIRANDA MAYA' guruhida)."
        )
        return
    set_setting("order_intake_group_id", str(chat.id))
    await update.message.reply_text(
        f"✅ Bu guruh ({chat.title}) endi buyurtma qabul qiluvchi guruh sifatida belgilandi.\n\n"
        "Endi bu guruhga yozilgan xabarlarni bot o'qib, sizga (shaxsiy xabarda) "
        "\"shunday tushundim\" deb tasdiqlash uchun yuboradi.\n\n"
        "⚠️ MUHIM: bot barcha xabarlarni ko'rishi uchun BotFather'da shu botga "
        "/setprivacy → Disable qilib qo'yish kerak (agar hali qilinmagan bo'lsa)."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Assalomu alaykum! Men zaxira botiman.\n\n"
        "Pastdagi tugmalardan foydalanishingiz mumkin, yoki buyruqlarni "
        "to'g'ridan-to'g'ri yozing.\n\n"
        "Mahsulot nomini har doim shunday yozing: <model> <detal>\n"
        "Misol: laura tumba, vena shkaf\n\n"
        "Buyruqlar:\n"
        "/kirim <model> <detal> <miqdor> - mahsulot keldi\n"
        "/chiqim <model> <detal> <miqdor> - mahsulot sotildi/ketdi\n"
        "/qoldiq - barcha mahsulotlar qoldig'i\n"
        "/qoldiq <model> <detal> - bitta mahsulot qoldig'i\n"
        "/modellar - modellar bo'yicha tugmali ko'rinish\n"
        "/tarix <model> <detal> [soni] - oxirgi harakatlar\n"
        "/ochir <model> <detal> - mahsulotni ro'yxatdan o'chirish\n"
        "/tozalash hammasi - barcha sonlarni 0 ga qaytarish\n"
        "/modelnomi <eski> <yangi> - model nomini o'zgartirish\n"
        "/detalnomi <model> <eski detal> <yangi detal> - detal nomini o'zgartirish\n"
        "/komplekttarkibi <model> - komplekt tarkibini ko'rish\n"
        "/komplekttarkibi <model> <detal> <soni> - komplektda detaldan nechta ketishini sozlash\n"
        "/royxatga - bir nechta modelni birdaniga qo'shish\n"
        "/buyurtma <model> komplekt <kun> <oy> [mijoz] - buyurtma qabul qilish\n"
        "/buyurtma <model> <detal> <miqdor> <kun> <oy> [mijoz] - buyurtma qabul qilish\n"
        "/buyurtmalar - bajarilmagan buyurtmalar ro'yxati\n"
        "/bajarildi <raqam> - buyurtmani bajarilgan deb belgilaydi va zaxiradan chiqaradi\n"
        "/bekor <raqam> - buyurtmani bekor qiladi (zaxiraga tegmaydi)\n"
        "/narx <upakovka|yigish> <detal> <summa> - narx belgilash (barcha modellar)\n"
        "/modelnarx <upakovka|yigish> <model> <detal> <summa> - faqat bitta modelga maxsus narx\n"
        "/ishchiulash <ism> <telegram ID> - ishchini botga ulash (kirim huquqi)\n"
        "/narxlar - barcha narxlarni ko'rish\n"
        "/ishchilar - ishchilar ro'yxati\n"
        "/maosh [ism] - to'lanmagan maoshlarni ko'rish\n"
        "/tolandi <ism> - ishchi maoshini to'landi deb belgilash\n\n"
        "Avtomatik xabarlar:\n"
        f"- Har kuni ertalab: {LOW_STOCK_THRESHOLD} tadan kam qolgan mahsulotlar haqida ogohlantirish\n"
        "- Har kuni ertalab: guruhga to'liq qoldiq hisoboti (agar sozlangan bo'lsa)\n"
        "- Har kuni 9:00 va 14:00 da: ishchiga bajarilmagan buyurtmalar eslatmasi (agar sozlangan bo'lsa)\n"
        "- Har yakshanba kechqurun: haftalik sotuv hisoboti\n\n"
        "Misol:\n"
        "/kirim laura tumba 5\n"
        "/chiqim vena shkaf 2"
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def kirim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_kirim(update):
        await deny_access(update)
        return
    await change_stock(update, context, "kirim")


async def chiqim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    await change_stock(update, context, "chiqim")


async def change_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, change_type: str):
    await change_stock_core(update, context, change_type, context.args)


async def change_stock_core(update: Update, context: ContextTypes.DEFAULT_TYPE, change_type: str, args):
    if len(args) < 3:
        cmd = "/kirim" if change_type == "kirim" else "/chiqim"
        await update.effective_message.reply_text(
            f"Foydalanish: {cmd} <model> <detal> <miqdor>\nMisol: {cmd} laura tumba 5"
        )
        return

    *name_parts, amount_raw = args
    product_display = " ".join(name_parts).strip()
    if not product_display:
        await update.effective_message.reply_text("Mahsulot nomini kiriting.")
        return

    try:
        amount = parse_amount(amount_raw)
    except ValueError:
        await update.effective_message.reply_text("Miqdor musbat butun son bo'lishi kerak. Misol: 5")
        return

    product_key = normalize_product_name(product_display)
    model, item = split_model_item(product_display)
    if not item:
        await update.effective_message.reply_text(
            "Mahsulot nomini <model> <detal> ko'rinishida yozing.\nMisol: laura tumba"
        )
        return

    user = update.effective_user
    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
    row = cur.fetchone()

    if row is None:
        if change_type == "chiqim":
            conn.close()
            await update.effective_message.reply_text(
                f"'{product_display}' ro'yxatda yo'q, undan chiqim qilib bo'lmaydi."
            )
            return
        current_qty = 0
        cur.execute(
            "INSERT INTO products (name, model, item, quantity) VALUES (?, ?, ?, ?)",
            (product_key, model, item, 0),
        )
    else:
        current_qty = row[0]

    if change_type == "kirim":
        new_qty = current_qty + amount
    else:
        if current_qty < amount:
            conn.close()
            await update.effective_message.reply_text(
                f"Xatolik: '{product_display}' dan faqat {current_qty} ta qolgan, "
                f"{amount} tani chiqarib bo'lmaydi."
            )
            return
        new_qty = current_qty - amount

    cur.execute(
        "UPDATE products SET quantity = ? WHERE name = ?",
        (new_qty, product_key),
    )
    cur.execute(
        """
        INSERT INTO transactions (product, change_type, amount, user_name, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (product_key, change_type, amount, user_name, user_id, datetime.now().isoformat(timespec="seconds")),
    )

    payment_line = ""
    if change_type == "kirim":
        # Agar shu foydalanuvchi bog'langan ishchi bo'lsa, kirim = upakovka ishi
        # deb hisoblab, avtomatik to'lovni yozib qo'yamiz.
        cur.execute("SELECT worker_name FROM worker_accounts WHERE telegram_id = ?", (user_id,))
        wrow = cur.fetchone()
        if wrow:
            worker = wrow[0]
            rate = get_rate(cur, "upakovka", model, item)
            total = amount * rate
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                """
                INSERT INTO work_log (worker, turi, order_id, model, item, amount, rate, total, paid, created_at)
                VALUES (?, 'upakovka', NULL, ?, ?, ?, ?, ?, 0, ?)
                """,
                (worker, model, item, amount, rate, total, now),
            )
            if rate == 0:
                payment_line = f"\n📦 {worker} — upakovka narxi belgilanmagan ('{item}' uchun /narx upakovka {item} <summa> bilan belgilang)."
            else:
                payment_line = f"\n📦 {worker} — upakovka: {total:,} so'm hisoblandi ({amount} x {rate:,}).".replace(",", " ")

    conn.commit()
    conn.close()

    verb = "qo'shildi" if change_type == "kirim" else "ayirildi"
    emoji = "📥" if change_type == "kirim" else "📤"
    text = (
        f"{emoji} '{product_display}': {amount} ta {verb}.\n"
        f"Yangi qoldiq: {new_qty} ta.\n"
        f"Kiritdi: {user_name}"
    )
    if payment_line:
        text += payment_line
    await update.effective_message.reply_text(text)


async def sb_start(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    allowed = can_kirim(update) if mode == "kirim" else is_owner(update)
    if not allowed:
        await deny_access(update)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products ORDER BY model")
    models = [row[0] for row in cur.fetchall()]
    conn.close()

    if not models:
        await update.message.reply_text("Hozircha hech qanday model ro'yxatga olinmagan.")
        return

    context.user_data["sb"] = {"mode": mode}
    title = "📥 Kirim" if mode == "kirim" else "📤 Chiqim"
    buttons = [[InlineKeyboardButton(m.capitalize(), callback_data=f"sb:model:{m}")] for m in models]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="sb:cancel")])
    await update.message.reply_text(
        f"{title} — modelni tanlang:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def kirim_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sb_start(update, context, "kirim")


async def chiqim_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sb_start(update, context, "chiqim")


def sb_item_keyboard(sb):
    buttons = [
        [InlineKeyboardButton(it, callback_data=f"sb:item:{i}")]
        for i, it in enumerate(sb["item_list"])
    ]
    buttons.append([InlineKeyboardButton("🔁 Boshqa model", callback_data="sb:restart")])
    buttons.append([InlineKeyboardButton("✅ Tugatish", callback_data="sb:done")])
    return InlineKeyboardMarkup(buttons)


def sb_qty_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data="sb:qty:-1"),
                InlineKeyboardButton("➕", callback_data="sb:qty:+1"),
            ],
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data="sb:qty:confirm")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="sb:qty:cancel")],
        ]
    )


def sb_qty_text(sb):
    title = "📥 Kirim" if sb["mode"] == "kirim" else "📤 Chiqim"
    return f"{title} — {sb['model'].capitalize()} {sb['current_item']}\n\nHozirgi son: {sb['qty_value']} ta"


async def sb_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    sb = context.user_data.get("sb")
    mode = sb.get("mode") if sb else None
    allowed = is_owner(update) or (mode == "kirim" and get_linked_worker(update) is not None)
    if not allowed:
        await query.answer("Faqat egasi (yoki ruxsat berilgan ishchi kirim uchun) qila oladi.", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data == "sb:cancel":
        context.user_data["sb"] = None
        await query.edit_message_text("Bekor qilindi.")
        return

    if data == "sb:restart":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT model FROM products ORDER BY model")
        models = [row[0] for row in cur.fetchall()]
        conn.close()
        title = "📥 Kirim" if sb["mode"] == "kirim" else "📤 Chiqim"
        buttons = [[InlineKeyboardButton(m.capitalize(), callback_data=f"sb:model:{m}")] for m in models]
        buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="sb:cancel")])
        await query.edit_message_text(f"{title} — modelni tanlang:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "sb:done":
        context.user_data["sb"] = None
        await query.edit_message_text("Tayyor.")
        return

    if sb is None:
        await query.edit_message_text("Sessiya tugagan. Qaytadan tugmani bosing.")
        return

    if data.startswith("sb:model:"):
        model = data.split(":", 2)[2]
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT item FROM products WHERE model = ? ORDER BY item", (model,))
        item_list = [row[0] for row in cur.fetchall()]
        conn.close()
        sb["model"] = model
        sb["item_list"] = item_list
        title = "📥 Kirim" if sb["mode"] == "kirim" else "📤 Chiqim"
        await query.edit_message_text(
            f"{title} — {model.capitalize()} — detalni tanlang:", reply_markup=sb_item_keyboard(sb)
        )
        return

    if data.startswith("sb:item:"):
        idx = int(data.split(":", 2)[2])
        sb["current_item"] = sb["item_list"][idx]
        sb["qty_value"] = 1
        await query.edit_message_text(sb_qty_text(sb), reply_markup=sb_qty_keyboard())
        return

    if data in ("sb:qty:+1", "sb:qty:-1"):
        delta = 1 if data.endswith("+1") else -1
        sb["qty_value"] = max(1, sb["qty_value"] + delta)
        await query.edit_message_text(sb_qty_text(sb), reply_markup=sb_qty_keyboard())
        return

    if data == "sb:qty:cancel":
        title = "📥 Kirim" if sb["mode"] == "kirim" else "📤 Chiqim"
        await query.edit_message_text(
            f"{title} — {sb['model'].capitalize()} — detalni tanlang:", reply_markup=sb_item_keyboard(sb)
        )
        return

    if data == "sb:qty:confirm":
        args = [sb["model"], sb["current_item"], str(sb["qty_value"])]
        await change_stock_core(update, context, sb["mode"], args)
        title = "📥 Kirim" if sb["mode"] == "kirim" else "📤 Chiqim"
        await query.edit_message_text(
            f"{title} — {sb['model'].capitalize()} — yana detal tanlang:", reply_markup=sb_item_keyboard(sb)
        )
        return


def ob_item_menu_text(ob):
    lines = [f"🆕 Buyurtma — {ob['model'].capitalize()}\n", "Detallarni tanlang (bir nechtasini tanlashingiz mumkin):"]
    if ob["items"]:
        lines.append("\nTanlanganlar:")
        for it, qty in ob["items"].items():
            lines.append(f"✅ {it}: {qty} ta")
    return "\n".join(lines)


def ob_item_keyboard(ob):
    buttons = []
    for it in ob["item_list"]:
        label = f"✅ {it} ({ob['items'][it]})" if it in ob["items"] else it
        buttons.append([InlineKeyboardButton(label, callback_data=f"ob:item:{ob['item_list'].index(it)}")])
    buttons.append([InlineKeyboardButton("📦 Komplekt (barchasi)", callback_data="ob:komplekt")])
    if ob["items"]:
        buttons.append([InlineKeyboardButton("➡️ Davom etish", callback_data="ob:items:done")])
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="ob:cancel")])
    return InlineKeyboardMarkup(buttons)


def ob_qty_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data="ob:qty:-1"),
                InlineKeyboardButton("➕", callback_data="ob:qty:+1"),
            ],
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data="ob:qty:confirm")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="ob:qty:cancel")],
        ]
    )


def ob_qty_text(ob):
    if ob["qty_mode"] == "komplekt":
        return f"📦 Komplekt: nechta?\n\nHozirgi son: {ob['qty_value']} ta"
    return f"📐 {ob['qty_item']}: nechta?\n\nHozirgi son: {ob['qty_value']} ta"


async def buyurtma_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products ORDER BY model")
    models = [row[0] for row in cur.fetchall()]
    conn.close()

    if not models:
        await update.message.reply_text("Hozircha hech qanday model ro'yxatga olinmagan.")
        return

    context.user_data["ob"] = None
    buttons = [[InlineKeyboardButton(m.capitalize(), callback_data=f"ob:model:{m}")] for m in models]
    await update.message.reply_text(
        "🆕 Yangi buyurtma — modelni tanlang:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ob_show_item_menu(query, ob):
    await query.edit_message_text(ob_item_menu_text(ob), reply_markup=ob_item_keyboard(ob))


async def ob_show_customer_menu(message_or_query, context, edit=False):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT customer FROM orders WHERE customer IS NOT NULL ORDER BY id DESC LIMIT 6"
    )
    customers = [row[0] for row in cur.fetchall()]
    conn.close()

    ob = context.user_data["ob"]
    ob["customer_options"] = customers

    buttons = [
        [InlineKeyboardButton(c, callback_data=f"ob:customer:pick:{i}")]
        for i, c in enumerate(customers)
    ]
    buttons.append([InlineKeyboardButton("✍️ Yangi mijoz yozish", callback_data="ob:customer:new")])
    buttons.append([InlineKeyboardButton("🚫 Mijozsiz davom etish", callback_data="ob:customer:skip")])

    text = "👤 Mijozni tanlang:"
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await message_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await message_or_query.reply_text(text, reply_markup=markup)


async def ob_show_month_menu(message_or_query, edit=False):
    month_names_ordered = [
        "yanvar", "fevral", "mart", "aprel", "may", "iyun",
        "iyul", "avgust", "sentyabr", "oktyabr", "noyabr", "dekabr",
    ]
    buttons = []
    row = []
    for i, m in enumerate(month_names_ordered, start=1):
        row.append(InlineKeyboardButton(m.capitalize(), callback_data=f"ob:month:{m}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    text = "📅 Muddat oyini tanlang:"
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await message_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await message_or_query.reply_text(text, reply_markup=markup)


async def ob_finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ob = context.user_data.get("ob")
    if not ob:
        return

    day = ob["day"]
    month = MONTH_NAMES[ob["month"]]
    deadline = compute_deadline(day, month)
    if deadline is None:
        await update.message.reply_text("Sana noto'g'ri. Qaytadan /buyurtma tugmasini bosing.")
        context.user_data["ob"] = None
        context.user_data["awaiting"] = None
        return

    deadline_display = f"{day} {ob['month']}"
    model = ob["model"]
    customer = ob["customer"]

    entries = []
    if ob["komplekt"]:
        entries.append((None, ob["komplekt_qty"]))
    else:
        for item, qty in ob["items"].items():
            entries.append((item, qty))

    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    created = []
    for item, amount in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (model, item, amount, deadline.isoformat(), deadline_display, customer, now),
        )
        created.append((cur.lastrowid, item, amount))

    guruh_id = created[0][0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created)})",
        [guruh_id] + [oid for oid, _, _ in created],
    )
    conn.commit()
    conn.close()

    what_all = ", ".join(
        (f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)")
        for _, item, amount in created
    )
    lines = [f"📝 Yangi buyurtma qabul qilindi (№{guruh_id}):", what_all]
    lines.append(f"Muddat: {deadline_display}")
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")

    context.user_data["ob"] = None
    context.user_data["awaiting"] = None
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)


async def ob_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ob:noop":
        return

    if data == "ob:cancel":
        context.user_data["ob"] = None
        context.user_data["awaiting"] = None
        await query.edit_message_text("Bekor qilindi.")
        return

    if data.startswith("ob:model:"):
        model = data.split(":", 2)[2]
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT item FROM products WHERE model = ? ORDER BY item", (model,))
        item_list = [row[0] for row in cur.fetchall()]
        conn.close()

        context.user_data["ob"] = {
            "model": model,
            "item_list": item_list,
            "items": {},
            "komplekt": False,
            "komplekt_qty": 1,
            "customer": None,
            "day": None,
            "month": None,
        }
        ob = context.user_data["ob"]
        await ob_show_item_menu(query, ob)
        return

    ob = context.user_data.get("ob")
    if ob is None:
        await query.edit_message_text("Sessiya tugagan. Qaytadan '🆕 Buyurtma' tugmasini bosing.")
        return

    if data.startswith("ob:item:"):
        idx = int(data.split(":", 2)[2])
        item = ob["item_list"][idx]
        ob["qty_mode"] = "item"
        ob["qty_item"] = item
        ob["qty_value"] = ob["items"].get(item, 1)
        await query.edit_message_text(ob_qty_text(ob), reply_markup=ob_qty_keyboard())
        return

    if data == "ob:komplekt":
        ob["qty_mode"] = "komplekt"
        ob["qty_value"] = ob.get("komplekt_qty", 1)
        await query.edit_message_text(ob_qty_text(ob), reply_markup=ob_qty_keyboard())
        return

    if data in ("ob:qty:+1", "ob:qty:-1"):
        delta = 1 if data.endswith("+1") else -1
        ob["qty_value"] = max(1, ob["qty_value"] + delta)
        await query.edit_message_text(ob_qty_text(ob), reply_markup=ob_qty_keyboard())
        return

    if data == "ob:qty:cancel":
        await ob_show_item_menu(query, ob)
        return

    if data == "ob:qty:confirm":
        if ob["qty_mode"] == "komplekt":
            ob["komplekt"] = True
            ob["komplekt_qty"] = ob["qty_value"]
            ob["items"] = {}
            await ob_show_customer_menu(query, context, edit=True)
        else:
            ob["items"][ob["qty_item"]] = ob["qty_value"]
            ob["komplekt"] = False
            await ob_show_item_menu(query, ob)
        return

    if data == "ob:items:done":
        if not ob["items"] and not ob["komplekt"]:
            await query.answer("Kamida bitta detal yoki komplekt tanlang.", show_alert=True)
            return
        await ob_show_customer_menu(query, context, edit=True)
        return

    if data.startswith("ob:customer:pick:"):
        idx = int(data.split(":", 3)[3])
        ob["customer"] = ob["customer_options"][idx]
        await ob_show_month_menu(query, edit=True)
        return

    if data == "ob:customer:skip":
        ob["customer"] = None
        await ob_show_month_menu(query, edit=True)
        return

    if data == "ob:customer:new":
        context.user_data["awaiting"] = "buyurtma_customer"
        await query.edit_message_text("✍️ Mijoz nomini yozing:")
        return

    if data.startswith("ob:month:"):
        month = data.split(":", 2)[2]
        ob["month"] = month
        context.user_data["awaiting"] = "buyurtma_day"
        await query.edit_message_text(f"📅 Oy: {month.capitalize()}\n\nEndi kunni raqam bilan yozing (masalan: 14):")
        return


async def tayyor_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = None
    context.user_data["ob"] = None
    await update.message.reply_text("Tayyor. Bosh menyuga qaytdik.", reply_markup=MAIN_MENU)


async def handle_awaiting_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return  # oddiy xabar, buyruq emas - e'tiborsiz qoldiramiz

    text = (update.message.text or "").strip()

    if awaiting == "buyurtma_customer":
        ob = context.user_data.get("ob")
        if ob is None:
            context.user_data["awaiting"] = None
            return
        ob["customer"] = text
        context.user_data["awaiting"] = None
        await ob_show_month_menu(update.message)
        return

    if awaiting == "buyurtma_day":
        ob = context.user_data.get("ob")
        if ob is None:
            context.user_data["awaiting"] = None
            return
        if not text.isdigit():
            await update.message.reply_text("Kunni faqat raqam bilan yozing. Misol: 14")
            return
        ob["day"] = int(text)
        await ob_finalize_order(update, context)
        return

    if awaiting == "worker_name_for_order":
        guruh_id = context.user_data.get("pending_order_id")
        context.user_data["awaiting"] = None
        context.user_data["pending_order_id"] = None
        if guruh_id is None:
            return
        prompt = start_payment_prompt(context, guruh_id, text)
        await update.message.reply_text(prompt)
        return

    if awaiting == "amount_received_for_order":
        pending = context.user_data.get("pending_payment")
        if pending is None:
            context.user_data["awaiting"] = None
            return
        cleaned = text.strip().replace(" ", "")
        if not cleaned.isdigit():
            await update.message.reply_text(
                "Iltimos, faqat son kiriting (masalan 0 yoki 1500000)."
            )
            return  # awaiting holati saqlanadi, qayta urinib ko'radi
        received = int(cleaned)
        context.user_data["awaiting"] = None
        context.user_data["pending_payment"] = None
        result_text = await finalize_payment(
            update.effective_user,
            pending["guruh_id"],
            pending["worker"],
            pending["customer"],
            pending["expected_value"],
            received,
        )
        await update.message.reply_text(result_text)
        return

    args = text.split()
    await change_stock_core(update, context, awaiting, args)
    # awaiting rejimi saqlanib qoladi - foydalanuvchi '✅ Tayyor' bosguncha
    # ketma-ket yana mahsulot yozishi mumkin.


def get_rate(cur, turi: str, model: str, item: str) -> int:
    """Narxni topadi: avval model uchun maxsus belgilangan narxni, topilmasa
    umumiy (barcha modellar uchun) narxni qaytaradi. Hech narsa topilmasa 0."""
    cur.execute(
        "SELECT rate FROM narxlar WHERE turi = ? AND model = ? AND item = ?",
        (turi, model, item),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "SELECT rate FROM narxlar WHERE turi = ? AND model = '' AND item = ?",
        (turi, item),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def stock_indicator(quantity: int) -> str:
    if quantity <= 0:
        return "🔴"
    if quantity <= LOW_STOCK_THRESHOLD:
        return "🟡"
    return "🟢"


async def qoldiq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    conn = get_conn()
    cur = conn.cursor()

    if args:
        product_display = " ".join(args).strip()
        product_key = normalize_product_name(product_display)
        cur.execute("SELECT name, quantity FROM products WHERE name = ?", (product_key,))
        row = cur.fetchone()
        conn.close()
        if row is None:
            await update.message.reply_text(f"'{product_display}' ro'yxatda topilmadi.")
        else:
            await update.message.reply_text(
                f"{stock_indicator(row[1])} {row[0]}: {row[1]} ta qoldi."
            )
        return

    conn.close()
    # Argumentsiz chaqirilsa - modellar tugmalarini ko'rsatamiz (xuddi /modellar kabi).
    await modellar(update, context)


async def modellar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products ORDER BY model")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Hozircha hech qanday model ro'yxatga olinmagan.")
        return

    buttons = [
        [InlineKeyboardButton(model.capitalize(), callback_data=f"model:{model}")]
        for (model,) in rows
    ]
    await update.message.reply_text(
        "📋 Modelni tanlang:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("model:"):
        return
    model = query.data.split(":", 1)[1]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT item, quantity FROM products WHERE model = ? ORDER BY item",
        (model,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text(f"'{model}' bo'yicha mahsulot topilmadi.")
        return

    total = sum(qty for _, qty in rows)
    lines = [f"📦 {model.capitalize()} (jami {total} ta):\n"]
    for item, quantity in rows:
        lines.append(f"{stock_indicator(quantity)} {item}: {quantity} ta")
    await query.edit_message_text("\n".join(lines))


async def tarix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Foydalanish: /tarix <model> <detal> [soni]\nMisol: /tarix laura tumba 10")
        return

    limit = 10
    name_parts = args
    if args[-1].isdigit():
        limit = int(args[-1])
        name_parts = args[:-1]

    product_display = " ".join(name_parts).strip()
    if not product_display:
        await update.message.reply_text("Mahsulot nomini kiriting.")
        return

    product_key = normalize_product_name(product_display)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT change_type, amount, user_name, created_at FROM transactions
        WHERE product = ? ORDER BY id DESC LIMIT ?
        """,
        (product_key, limit),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"'{product_display}' bo'yicha hech qanday harakat topilmadi.")
        return

    lines = [f"🕓 '{product_display}' tarixi (oxirgi {len(rows)} ta):\n"]
    for change_type, amount, user_name, created_at in rows:
        emoji = "📥" if change_type == "kirim" else "📤"
        lines.append(f"{emoji} {created_at} — {amount} ta ({change_type}) — {user_name}")
    await update.message.reply_text("\n".join(lines))


async def ochir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    args = context.args
    if not args:
        await update.message.reply_text("Foydalanish: /ochir <model> <detal>")
        return
    product_display = " ".join(args).strip()
    product_key = normalize_product_name(product_display)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE name = ?", (product_key,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await update.message.reply_text(f"'{product_display}' ro'yxatdan o'chirildi.")
    else:
        await update.message.reply_text(f"'{product_display}' ro'yxatda topilmadi.")


async def tozalash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or args[0].lower() != "hammasi":
        await update.message.reply_text(
            "Bu buyruq BARCHA mahsulotlar sonini 0 ga qaytaradi "
            "(ro'yxat - model va detal nomlari - saqlanib qoladi).\n\n"
            "Tasdiqlash uchun aynan shu yozing:\n"
            "/tozalash hammasi"
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM products")
    total = cur.fetchone()[0]
    cur.execute("UPDATE products SET quantity = 0")
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🧹 Tozalandi. {total} ta mahsulotning barchasi 0 taga qaytarildi.\n"
        "Endi /kirim orqali haqiqiy sonlarni qaytadan kiritishingiz mumkin."
    )


async def narx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Foydalanish: /narx <upakovka|yigish|sotish> <detal> <summa>\n"
        "Misol: /narx upakovka shkaf 5000\n"
        "Misol: /narx yigish shkaf 15000\n"
        "Misol: /narx sotish komplekt 1500000  (mijozga sotish narxi)\n"
        "Komplekt uchun: /narx yigish komplekt 100000\n\n"
        "Bitta modelga maxsus narx uchun: /modelnarx <upakovka|yigish|sotish> <model> <detal> <summa>"
    )
    if len(args) < 3 or args[0].lower() not in ("upakovka", "yigish", "sotish") or not args[-1].isdigit():
        await update.message.reply_text(usage)
        return

    turi = args[0].lower()
    rate = int(args[-1])
    item = " ".join(args[1:-1]).lower()
    if not item:
        await update.message.reply_text(usage)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO narxlar (turi, model, item, rate) VALUES (?, '', ?, ?) "
        "ON CONFLICT(turi, model, item) DO UPDATE SET rate = excluded.rate",
        (turi, item, rate),
    )
    conn.commit()
    conn.close()

    turi_label = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish"}[turi]
    await update.message.reply_text(
        f"✅ {turi_label} — '{item}' (barcha modellar) narxi: {rate:,} so'm deb belgilandi.".replace(",", " ")
    )


async def modelnarx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = [a.lower() for a in context.args]
    usage = (
        "Foydalanish: /modelnarx <upakovka|yigish|sotish> <model> <detal> <summa>\n"
        "Misol: /modelnarx yigish bella spalniy shkaf 20000\n"
        "Misol: /modelnarx sotish neo komplekt 1800000\n\n"
        "Bu faqat ko'rsatilgan modelga tegishli, boshqa modellar umumiy narxda qoladi."
    )
    if len(args) < 4 or args[0] not in ("upakovka", "yigish", "sotish") or not args[-1].isdigit():
        await update.message.reply_text(usage)
        return

    turi = args[0]
    rate = int(args[-1])
    middle = args[1:-1]  # <model...> <detal>

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    all_models.sort(key=lambda m: -len(m.split()))

    model = None
    item = None
    for candidate in all_models:
        candidate_tokens = candidate.split()
        if middle[: len(candidate_tokens)] == candidate_tokens:
            model = candidate
            item = " ".join(middle[len(candidate_tokens) :])
            break

    if model is None or not item:
        conn.close()
        await update.message.reply_text(
            f"Model yoki detal topilmadi.\nMavjud modellar: {', '.join(sorted(set(all_models)))}\n\n"
            + usage
        )
        return

    cur.execute(
        "INSERT INTO narxlar (turi, model, item, rate) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(turi, model, item) DO UPDATE SET rate = excluded.rate",
        (turi, model, item, rate),
    )
    conn.commit()
    conn.close()

    turi_label = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish"}[turi]
    await update.message.reply_text(
        f"✅ {turi_label} — '{model} {item}' uchun maxsus narx: {rate:,} so'm.".replace(",", " ")
    )


async def narxlar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT turi, model, item, rate FROM narxlar ORDER BY turi, model, item")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "Hozircha hech qanday narx belgilanmagan.\nQo'shish: /narx <upakovka|yigish> <detal> <summa>"
        )
        return

    lines = ["💰 Narxlar:"]
    current_turi = None
    turi_icons = {"upakovka": "📦", "yigish": "🚚", "sotish": "🏷️"}
    turi_labels = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish (mijozga)"}
    for turi, model, item, rate in rows:
        if turi != current_turi:
            icon = turi_icons.get(turi, "•")
            label = turi_labels.get(turi, turi)
            lines.append(f"\n{icon} {label}:")
            current_turi = turi
        if model:
            lines.append(f"• {model} {item} (maxsus): {rate:,} so'm".replace(",", " "))
        else:
            lines.append(f"• {item}: {rate:,} so'm".replace(",", " "))
    await update.message.reply_text("\n".join(lines))



async def ishchilar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM workers ORDER BY name")
    names = [row[0] for row in cur.fetchall()]
    cur.execute("SELECT worker_name, telegram_id FROM worker_accounts")
    linked = dict(cur.fetchall())
    conn.close()

    if not names:
        await update.message.reply_text("Hozircha hech qanday ishchi qo'shilmagan.")
        return

    lines = ["👷 Ishchilar:\n"]
    for name in names:
        if name in linked:
            lines.append(f"• {name} — bog'langan (kirim qila oladi)")
        else:
            lines.append(f"• {name} — bog'lanmagan")
    lines.append("\nBog'lash: /ishchiulash <ism> <telegram ID>")
    await update.message.reply_text("\n".join(lines))


async def ishchiulash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) < 2 or not args[-1].isdigit():
        await update.message.reply_text(
            "Foydalanish: /ishchiulash <ism> <telegram ID>\n"
            "Misol: /ishchiulash Hojiakbar 6926878775\n\n"
            "Telegram ID ni olish uchun: ishchi botga shaxsiy chatda /chatid deb yozadi."
        )
        return

    telegram_id = int(args[-1])
    worker = " ".join(args[:-1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO workers (name, created_at) VALUES (?, ?)",
        (worker, datetime.now().isoformat(timespec="seconds")),
    )
    cur.execute(
        "INSERT INTO worker_accounts (telegram_id, worker_name, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET worker_name = excluded.worker_name",
        (telegram_id, worker, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ {worker} — Telegram ID {telegram_id} bilan bog'landi.\n"
        f"Endi u botga /kirim orqali mahsulot kirim qila oladi (upakovka ishi sifatida hisoblanadi)."
    )


async def komplekttarkibi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = [a.lower() for a in context.args]
    usage = (
        "Foydalanish:\n"
        "/komplekttarkibi <model> - hozirgi tarkibni ko'rsatadi\n"
        "/komplekttarkibi <model> <detal> <soni> - tarkibni sozlaydi\n\n"
        "Misol:\n"
        "/komplekttarkibi bella spalniy\n"
        "/komplekttarkibi bella spalniy tumba 2"
    )

    if not args:
        await update.message.reply_text(usage)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    # Ko'p so'zli modellarni to'g'ri aniqlash uchun avval uzunlarini tekshiramiz.
    all_models.sort(key=lambda m: -len(m.split()))

    matched_model = None
    remaining = None
    for candidate in all_models:
        candidate_tokens = candidate.split()
        if args[: len(candidate_tokens)] == candidate_tokens:
            matched_model = candidate
            remaining = args[len(candidate_tokens) :]
            break

    if matched_model is None:
        conn.close()
        await update.message.reply_text(
            f"Model topilmadi. Mavjud modellar: {', '.join(sorted(set(all_models)))}"
        )
        return

    if not remaining:
        cur.execute("SELECT item FROM products WHERE model = ? ORDER BY item", (matched_model,))
        items = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT item, soni FROM komplekt_tarkibi WHERE model = ?", (matched_model,))
        overrides = dict(cur.fetchall())
        conn.close()

        lines = [f"📐 '{matched_model}' komplekt tarkibi (1 komplekt uchun):\n"]
        for item in items:
            soni = overrides.get(item, 1)
            standart = "" if item in overrides else " (standart)"
            lines.append(f"• {item}: {soni} ta{standart}")
        lines.append(
            "\nO'zgartirish uchun: /komplekttarkibi <model> <detal> <soni>\n"
            f"Misol: /komplekttarkibi {matched_model} tumba 2"
        )
        await update.message.reply_text("\n".join(lines))
        return

    if len(remaining) == 2 and remaining[1].isdigit():
        item, soni = remaining[0], int(remaining[1])
        if soni <= 0:
            conn.close()
            await update.message.reply_text("Soni musbat butun son bo'lishi kerak.")
            return

        product_key = normalize_product_name(f"{matched_model} {item}")
        cur.execute("SELECT 1 FROM products WHERE name = ?", (product_key,))
        if cur.fetchone() is None:
            conn.close()
            await update.message.reply_text(f"'{product_key}' ro'yxatda topilmadi.")
            return

        cur.execute(
            "INSERT INTO komplekt_tarkibi (model, item, soni) VALUES (?, ?, ?) "
            "ON CONFLICT(model, item) DO UPDATE SET soni = excluded.soni",
            (matched_model, item, soni),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ Endi 1 '{matched_model}' komplektida '{item}' dan {soni} ta bo'ladi.\n"
            "Bu keyingi /bajarildi buyurtmalarida shu bo'yicha hisoblanadi."
        )
        return

    conn.close()
    await update.message.reply_text(usage)


async def modelnomi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Foydalanish: /modelnomi <eski nom> <yangi nom>\n"
            "Misol: /modelnomi laura sofia\n\n"
            "Bu butun modelning (barcha detallari, sonlari va tarixi bilan) "
            "nomini o'zgartiradi."
        )
        return

    old_model = args[0].lower()
    new_model = args[1].lower()

    if old_model == new_model:
        await update.message.reply_text("Eski va yangi nom bir xil.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, item FROM products WHERE model = ?", (old_model,))
    rows = cur.fetchall()

    if not rows:
        conn.close()
        await update.message.reply_text(f"'{old_model}' nomli model topilmadi.")
        return

    renamed = []
    conflicts = []
    for old_name, item in rows:
        new_name = normalize_product_name(f"{new_model} {item}")
        cur.execute("SELECT 1 FROM products WHERE name = ?", (new_name,))
        if cur.fetchone() is not None:
            conflicts.append(item)
            continue
        cur.execute(
            "UPDATE products SET name = ?, model = ? WHERE name = ?",
            (new_name, new_model, old_name),
        )
        cur.execute(
            "UPDATE transactions SET product = ? WHERE product = ?",
            (new_name, old_name),
        )
        renamed.append(item)

    if renamed:
        cur.execute(
            "UPDATE orders SET model = ? WHERE model = ? AND item IN (%s)"
            % ",".join("?" for _ in renamed),
            [new_model, old_model, *renamed],
        )

    conn.commit()
    conn.close()

    lines = []
    if renamed:
        lines.append(
            f"✅ '{old_model}' → '{new_model}' deb o'zgartirildi ({len(renamed)} ta detal):"
        )
        lines.extend(f"• {new_model} {item}" for item in renamed)
    if conflicts:
        lines.append(
            f"\n⚠️ Quyidagilar o'tkazib yuborildi, chunki '{new_model}' modelida "
            "shu nomli detal allaqachon bor edi:"
        )
        lines.extend(f"• {item}" for item in conflicts)

    await update.message.reply_text("\n".join(lines))


async def detalnomi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Foydalanish: /detalnomi <model> <eski detal> <yangi detal>\n"
            "Misol: /detalnomi neo shkaf shkaf oq"
        )
        return

    model = args[0].lower()
    old_item = args[1].lower()
    new_item = " ".join(args[2:]).lower()

    old_name = normalize_product_name(f"{model} {old_item}")
    new_name = normalize_product_name(f"{model} {new_item}")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM products WHERE name = ?", (old_name,))
    if cur.fetchone() is None:
        conn.close()
        await update.message.reply_text(f"'{old_name}' ro'yxatda topilmadi.")
        return

    cur.execute("SELECT 1 FROM products WHERE name = ?", (new_name,))
    if cur.fetchone() is not None:
        conn.close()
        await update.message.reply_text(f"'{new_name}' allaqachon mavjud, boshqa nom tanlang.")
        return

    cur.execute(
        "UPDATE products SET name = ?, item = ? WHERE name = ?",
        (new_name, new_item, old_name),
    )
    cur.execute(
        "UPDATE transactions SET product = ? WHERE product = ?",
        (new_name, old_name),
    )
    cur.execute(
        "UPDATE orders SET item = ? WHERE model = ? AND item = ?",
        (new_item, model, old_item),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ '{old_name}' → '{new_name}' deb o'zgartirildi.")


async def royxatga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    full_text = update.message.text or ""
    # Birinchi qatordan buyruq nomini olib tashlaymiz (masalan "/royxatga").
    lines = full_text.split("\n")
    first_line = lines[0]
    after_command = first_line.split(None, 1)
    remaining_first_line = after_command[1] if len(after_command) > 1 else ""
    body_lines = ([remaining_first_line] if remaining_first_line else []) + lines[1:]

    entries = []  # (model, item)
    errors = []
    for raw_line in body_lines:
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            errors.append(f"'{line}' - qator 'model: detal1, detal2' ko'rinishida bo'lishi kerak")
            continue
        model_part, items_part = line.split(":", 1)
        model = model_part.strip().lower()
        items = [item.strip().lower() for item in items_part.split(",") if item.strip()]
        if not model or not items:
            errors.append(f"'{line}' - model yoki detallar bo'sh")
            continue
        for item in items:
            entries.append((model, item))

    if not entries:
        await update.message.reply_text(
            "Foydalanish:\n"
            "/royxatga\n"
            "laura: shkaf, tumba, krovat, kamod, parta\n"
            "vena: shkaf, tumba, krovat, kamod, parta"
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    added = []
    skipped = []
    for model, item in entries:
        product_display = f"{model} {item}"
        product_key = normalize_product_name(product_display)
        cur.execute("SELECT 1 FROM products WHERE name = ?", (product_key,))
        if cur.fetchone() is not None:
            skipped.append(product_display)
            continue
        cur.execute(
            "INSERT INTO products (name, model, item, quantity) VALUES (?, ?, ?, 0)",
            (product_key, model, item),
        )
        added.append(product_display)
    conn.commit()
    conn.close()

    lines_out = []
    if added:
        lines_out.append(f"✅ Qo'shildi ({len(added)} ta, hammasi 0 ta bilan):")
        lines_out.extend(f"• {name}" for name in added)
    if skipped:
        lines_out.append(f"\n⏭ Allaqachon bor edi, o'tkazib yuborildi ({len(skipped)} ta):")
        lines_out.extend(f"• {name}" for name in skipped)
    if errors:
        lines_out.append("\n⚠️ Xato qatorlar:")
        lines_out.extend(f"• {err}" for err in errors)

    await update.message.reply_text("\n".join(lines_out))


def find_month_index(tokens):
    """Ro'yxatdan oy nomi va undan oldingi kun raqamini topadi.
    Qaytaradi: (oy_indeksi, kun) yoki None agar topilmasa."""
    for i, tok in enumerate(tokens):
        if tok.lower() in MONTH_NAMES and i > 0 and tokens[i - 1].isdigit():
            return i, int(tokens[i - 1])
    return None


def compute_deadline(day: int, month: int):
    """Bugundan keyingi eng yaqin shu kun/oyni topadi (o'tib ketgan bo'lsa - keyingi yil)."""
    today = date.today()
    year = today.year
    try:
        deadline = date(year, month, day)
    except ValueError:
        return None
    if deadline < today:
        try:
            deadline = date(year + 1, month, day)
        except ValueError:
            return None
    return deadline


UZ_MONTH_BY_NUM = {v: k for k, v in MONTH_NAMES.items()}


GENERIC_ITEM_WORDS = ["shkaf", "tumba", "krovat", "kamod", "parta"]


def parse_deadline_from_text(text: str):
    """Matndan 'Muddat: 21-22 avgust', '29-Avgust', '5 avgust' kabi sanalarni topadi.
    Qaytaradi: (day, month_num, deadline_display) yoki None."""
    lowered = text.lower()

    # 1) Oraliq: "21-22 avgust" - oxirgi kunni muddat deb olamiz.
    m = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([a-zʻʼ']+)", lowered)
    if m:
        day = int(m.group(2))
        month_word = m.group(3)
        if month_word in MONTH_NAMES:
            return day, MONTH_NAMES[month_word], f"{day} {month_word}"

    # 2) Chiziqcha bilan qo'shilgan: "29-Avgust"
    m = re.search(r"(\d{1,2})\s*[-–]\s*([a-zʻʼ']+)", lowered)
    if m:
        day = int(m.group(1))
        month_word = m.group(2)
        if month_word in MONTH_NAMES:
            return day, MONTH_NAMES[month_word], f"{day} {month_word}"

    # 3) Oddiy bo'shliq bilan: "5 avgust"
    m = re.search(r"(\d{1,2})\s+([a-zʻʼ']+)", lowered)
    if m:
        day = int(m.group(1))
        month_word = m.group(2)
        if month_word in MONTH_NAMES:
            return day, MONTH_NAMES[month_word], f"{day} {month_word}"

    return None


def parse_model_from_text(text: str, all_models):
    """Matn ichidan (istalgan joyidan) bazadagi modellardan birini qidiradi.
    Ko'p so'zli modellarni ustuvor qiladi (masalan 'bella spalniy' > 'bella')."""
    tokens = re.findall(r"[a-zA-Zʻʼ'\u0400-\u04FF]+", text.lower())
    candidates = sorted(set(all_models), key=lambda m: -len(m.split()))
    for candidate in candidates:
        cand_tokens = candidate.split()
        n = len(cand_tokens)
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == cand_tokens:
                return candidate
    return None


def parse_entries_from_text(text: str, model: str):
    """Modeldan keyin komplekt yoki BIR NECHTA aniq detalni (masalan
    '1 ta shkaf 1 ta parta') aniqlashga harakat qiladi.
    Qaytaradi: (entries, komplekt_aniq) - entries ro'yxati [(item_or_None, amount), ...]."""
    lowered = text.lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT item FROM products WHERE model = ?", (model,))
    model_items = [row[0] for row in cur.fetchall()]
    conn.close()

    candidate_items = set(model_items or GENERIC_ITEM_WORDS)

    # "N ta <item>" yoki "<item> N ta" ko'rinishidagi barcha uchrashuvlarni yig'amiz.
    entries = []
    seen_items = set()
    for m in re.finditer(r"(\d+)\s*ta\s+([a-zʻʼ']+)", lowered):
        item_word = m.group(2)
        for cand in candidate_items:
            if cand.lower() == item_word and cand not in seen_items:
                entries.append((cand, int(m.group(1))))
                seen_items.add(cand)
                break
    for m in re.finditer(r"([a-zʻʼ']+)\s+(\d+)\s*ta\b", lowered):
        item_word = m.group(1)
        for cand in candidate_items:
            if cand.lower() == item_word and cand not in seen_items:
                entries.append((cand, int(m.group(2))))
                seen_items.add(cand)
                break

    if entries:
        return entries, False

    if "komplekt" in lowered:
        return [(None, 1)], True

    # Miqdorsiz, lekin nomi tilga olingan detallarni ham tekshiramiz (masalan faqat "shkaf").
    for item in sorted(candidate_items, key=lambda x: -len(x)):
        if re.search(rf"\b{re.escape(item.lower())}\b", lowered):
            entries.append((item, 1))
    if entries:
        return entries, False

    # Hech narsa aniqlanmadi - komplekt deb taxmin qilamiz, lekin noaniq deb belgilaymiz.
    return [(None, 1)], False


def generate_buyurtma_command(model, entries, deadline_display, customer):
    parts = [model]
    if len(entries) == 1 and entries[0][0] is None:
        parts.append("komplekt")
    else:
        for item, amount in entries:
            parts.append(item or "komplekt")
            parts.append(str(amount))
    parts.append(deadline_display.replace(" ", " "))
    if customer:
        parts.append(customer)
    return "/buyurtma " + " ".join(parts)


async def send_group_order_confirmation(context: ContextTypes.DEFAULT_TYPE, chat_id, message_id, text, sender_name):
    if OWNER_ID is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    conn.close()

    model = parse_model_from_text(text, all_models)
    if model is None:
        return  # Model aniqlanmadi - bu guruh xabari buyurtma emas bo'lishi mumkin, e'tiborsiz qoldiramiz.

    deadline_info = parse_deadline_from_text(text)
    entries, komplekt_aniq = parse_entries_from_text(text, model)

    lines = ["🔔 Guruhda yangi xabar - buyurtma bo'lishi mumkin:", ""]
    lines.append(f"Taxminiy model: {model}")
    if len(entries) == 1 and entries[0][0] is None:
        lines.append(f"Taxminiy tur: komplekt {'✅' if komplekt_aniq else '(❗ aniq topilmadi, tekshiring)'}")
    else:
        for item, amount in entries:
            lines.append(f"Taxminiy detal: {item} ({amount} ta)")

    if deadline_info:
        day, month, deadline_display = deadline_info
        lines.append(f"Taxminiy muddat: {deadline_display}")
    else:
        lines.append("Muddat: ❗ topilmadi")

    lines.append(f"Kimdan: {sender_name}")
    lines.append("")
    lines.append(f"Asl xabar:\n{text}")
    lines.append("\n❗ Diqqat bilan tekshiring - bot taxmin qilyapti, xato bo'lishi mumkin.")

    if deadline_info is None:
        lines.append("\n⚠️ Muddat aniqlanmagani uchun avtomatik tasdiqlash mumkin emas. "
                      "Kerak bo'lsa /buyurtma orqali qo'lda kiriting.")
        await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines))
        return

    day, month, deadline_display = deadline_info
    deadline = compute_deadline(day, month)
    if deadline is None:
        lines.append("\n⚠️ Sana noto'g'ri chiqdi, qo'lda kiriting.")
        await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines))
        return

    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        """
        INSERT INTO pending_group_orders
            (model, entries_json, deadline, deadline_display, customer, raw_text, source_chat_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
        """,
        (model, json.dumps(entries), deadline.isoformat(), deadline_display, sender_name, text, chat_id, now),
    )
    pending_id = cur.lastrowid
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"gord:{pending_id}:yes"),
                InlineKeyboardButton("✏️ Tahrirlash", callback_data=f"gord:{pending_id}:edit"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data=f"gord:{pending_id}:no"),
            ]
        ]
    )
    await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines), reply_markup=keyboard)


async def group_order_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return

    intake_id_raw = get_setting("order_intake_group_id")
    if intake_id_raw is None or str(chat.id) != intake_id_raw:
        return

    user = update.effective_user
    if user and user.is_bot:
        return

    text = update.message.text or update.message.caption
    if not text:
        return

    sender_name = user.full_name if user else (chat.title or "Nomalum")

    await send_group_order_confirmation(context, chat.id, update.message.message_id, text, sender_name)


async def gord_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, pending_id_str, action = query.data.split(":")
    pending_id = int(pending_id_str)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT model, entries_json, deadline, deadline_display, customer, status FROM pending_group_orders WHERE id = ?",
        (pending_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        await query.edit_message_text("Bu taklif topilmadi (eskirgan bo'lishi mumkin).")
        return

    model, entries_json, deadline, deadline_display, customer, status = row
    entries = json.loads(entries_json)

    if status != "kutilmoqda":
        conn.close()
        await query.edit_message_text(query.message.text + "\n\n(Bu allaqachon ko'rib chiqilgan)")
        return

    if action == "no":
        cur.execute("UPDATE pending_group_orders SET status = 'rad etildi' WHERE id = ?", (pending_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(query.message.text + "\n\n❌ Rad etildi.")
        return

    if action == "edit":
        cur.execute("UPDATE pending_group_orders SET status = 'tahrirga yuborildi' WHERE id = ?", (pending_id,))
        conn.commit()
        conn.close()
        suggested = generate_buyurtma_command(model, entries, deadline_display, customer)
        await query.edit_message_text(
            query.message.text + "\n\n✏️ Tahrirlash uchun yuborilgan (avtomatik yaratilmaydi)."
        )
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "Quyidagi buyruqni nusxalab, kerakli joyini to'g'rilab, keyin yuboring:\n\n"
                f"`{suggested}`"
            ),
            parse_mode="Markdown",
        )
        return

    # action == "yes"
    now = datetime.now().isoformat(timespec="seconds")
    created_ids = []
    for item, amount in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (model, item, amount, deadline, deadline_display, customer, now),
        )
        created_ids.append(cur.lastrowid)

    guruh_id = created_ids[0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created_ids)})",
        [guruh_id] + created_ids,
    )
    cur.execute("UPDATE pending_group_orders SET status = 'tasdiqlandi' WHERE id = ?", (pending_id,))
    conn.commit()
    conn.close()

    what_all = ", ".join(
        (f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)")
        for item, amount in entries
    )
    await query.edit_message_text(
        query.message.text + f"\n\n✅ Tasdiqlandi! Buyurtma №{guruh_id} yaratildi: {what_all}, muddat {deadline_display}."
    )


async def buyurtma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    await buyurtma_core(update, context, context.args)


async def buyurtma_core(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_args):
    # "12-avgust" kabi chiziqcha bilan yozilgan sanalarni ham qabul qilamiz.
    fixed_args = []
    for tok in raw_args:
        m = re.match(r"^(\d{1,2})-([a-zA-Z']+)$", tok)
        if m:
            fixed_args.append(m.group(1))
            fixed_args.append(m.group(2))
        else:
            fixed_args.append(tok)
    args = [a.lower() for a in fixed_args]

    usage = (
        "Foydalanish:\n"
        "/buyurtma <model> komplekt <kun> <oy> [mijoz]\n"
        "/buyurtma <model> <detal> <miqdor> <kun> <oy> [mijoz]\n"
        "/buyurtma <model> <detal1> <miqdor1> <detal2> <miqdor2> ... <kun> <oy> [mijoz]\n\n"
        "Misol:\n"
        "/buyurtma vena komplekt 5 avgust\n"
        "/buyurtma laura shkaf 2 5 avgust Mavaviy dokon\n"
        "/buyurtma maya shkaf 1 tumba 1 krovat 1 kamod 1 14 avgust"
    )
    if len(args) < 3:
        await update.message.reply_text(usage)
        return

    found = find_month_index(args)
    if found is None:
        await update.message.reply_text(
            "Sanani tushunolmadim. Oy nomini to'g'ri yozing (masalan: avgust) "
            "va undan oldin kunni yozing (masalan: 5 avgust).\n\n" + usage
        )
        return

    month_idx, day = found
    month = MONTH_NAMES[args[month_idx]]
    deadline = compute_deadline(day, month)
    if deadline is None:
        await update.message.reply_text("Sana noto'g'ri (masalan 32 kun yoki noto'g'ri oy).")
        return

    before = args[: month_idx - 1]
    customer_tokens = args[month_idx + 1 :]
    customer = " ".join(customer_tokens).strip() or None

    if not before:
        await update.message.reply_text(usage)
        return

    # Modelni bazadagi haqiqiy modellar bilan solishtirib topamiz
    # (ko'p so'zli modellarni ham, masalan "bella spalniy", to'g'ri aniqlash uchun).
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    all_models.sort(key=lambda m: -len(m.split()))

    model = None
    rest = None
    for candidate in all_models:
        candidate_tokens = candidate.split()
        if before[: len(candidate_tokens)] == candidate_tokens:
            model = candidate
            rest = before[len(candidate_tokens) :]
            break

    if model is None:
        conn.close()
        await update.message.reply_text(
            f"Model topilmadi. Mavjud modellar: {', '.join(sorted(set(all_models)))}"
        )
        return

    if not rest:
        conn.close()
        await update.message.reply_text(usage)
        return

    # Buyurtma turlari: komplekt, bitta detal, yoki bir nechta detal-miqdor jufti.
    entries = []  # (item_or_None, amount)

    if rest[0] == "komplekt":
        amount = 1
        remaining = rest[1:]
        if remaining and remaining[0].isdigit():
            amount = int(remaining[0])
        entries.append((None, amount))
    elif len(rest) % 2 == 0 and all(rest[i].isdigit() for i in range(1, len(rest), 2)):
        # juft-juft: detal miqdor detal miqdor ...
        for i in range(0, len(rest), 2):
            entries.append((rest[i], int(rest[i + 1])))
    else:
        conn.close()
        await update.message.reply_text(
            "Detal va miqdorni juft-juft yozing (masalan: shkaf 1 tumba 2), "
            "yoki 'komplekt' deb yozing.\n\n" + usage
        )
        return

    deadline_display = f"{day} {UZ_MONTH_BY_NUM[month]}"
    created = []
    now = datetime.now().isoformat(timespec="seconds")
    for item, amount in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (model, item, amount, deadline.isoformat(), deadline_display, customer, now),
        )
        created.append((cur.lastrowid, item, amount))

    guruh_id = created[0][0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created)})",
        [guruh_id] + [oid for oid, _, _ in created],
    )

    conn.commit()
    conn.close()

    what_all = ", ".join(
        (f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)")
        for _, item, amount in created
    )
    lines = [f"📝 Yangi buyurtma qabul qilindi (№{guruh_id}):", what_all]
    lines.append(f"Muddat: {deadline_display}")
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")
    await update.message.reply_text("\n".join(lines))


async def guruhlash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) < 2 or not all(a.isdigit() for a in args):
        await update.message.reply_text(
            "Eski (alohida) buyurtmalarni bitta guruhga birlashtiradi.\n\n"
            "Foydalanish: /guruhlash <yangi_raqam> <eski_raqam1> <eski_raqam2> ...\n"
            "Misol: /guruhlash 25 25 26 27 28 29"
        )
        return

    new_guruh_id = int(args[0])
    order_ids = [int(a) for a in args]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, model, item FROM orders WHERE id IN ({','.join('?' for _ in order_ids)})",
        order_ids,
    )
    found = cur.fetchall()
    if not found:
        conn.close()
        await update.message.reply_text("Bu raqamlar bilan hech qanday buyurtma topilmadi.")
        return

    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in order_ids)})",
        [new_guruh_id] + order_ids,
    )
    conn.commit()
    conn.close()

    lines = [f"✅ {len(found)} ta buyurtma №{new_guruh_id} guruhiga birlashtirildi:"]
    for oid, model, item in found:
        what = f"{model} komplekt" if item is None else f"{model} {item}"
        lines.append(f"• №{oid} — {what}")
    await update.message.reply_text("\n".join(lines))


async def bekor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Foydalanish: /bekor <buyurtma raqami>\nMisol: /bekor 14")
        return

    guruh_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT model, item FROM orders WHERE guruh_id = ? AND status = 'kutilmoqda'", (guruh_id,)
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await update.message.reply_text(
            f"№{guruh_id} buyurtma topilmadi yoki allaqachon bajarilgan/bekor qilingan."
        )
        return

    cur.execute("UPDATE orders SET status = 'bekor qilindi' WHERE guruh_id = ?", (guruh_id,))
    conn.commit()
    conn.close()

    what_all = ", ".join(
        (f"{model} komplekt" if item is None else f"{model} {item}") for model, item in rows
    )
    await update.message.reply_text(
        f"🗑 №{guruh_id} ({what_all}) bekor qilindi. Zaxiraga hech qanday ta'sir qilmadi."
    )


def format_group_text(group):
    today = date.today()
    deadline_date = date.fromisoformat(group["deadline"])
    days_left = (deadline_date - today).days
    if days_left > 0:
        days_text = f"{days_left} kun qoldi"
    elif days_left == 0:
        days_text = "bugun"
    else:
        days_text = f"muddati {abs(days_left)} kun o'tgan"

    lines = [f"📋 Buyurtma №{group['guruh_id']} — {group['deadline_display']} ({days_text})"]
    if group["customer"]:
        lines.append(f"Mijoz: {group['customer']}")
    lines.append("")
    for _, model, item, amount in group["items"]:
        what = f"{model} komplekt" if item is None else f"{model} {item}"
        lines.append(f"• {what}: {amount} ta")
    return "\n".join(lines)


def fetch_pending_order_groups():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, guruh_id, model, item, amount, deadline, deadline_display, customer
        FROM orders WHERE status = 'kutilmoqda' ORDER BY deadline ASC, guruh_id ASC, id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()

    groups = {}
    order_of_groups = []
    for oid, guruh_id, model, item, amount, deadline_iso, deadline_display, customer in rows:
        gid = guruh_id if guruh_id is not None else oid
        if gid not in groups:
            groups[gid] = {
                "guruh_id": gid,
                "deadline": deadline_iso,
                "deadline_display": deadline_display,
                "customer": customer,
                "items": [],
            }
            order_of_groups.append(gid)
        groups[gid]["items"].append((oid, model, item, amount))
    return [groups[gid] for gid in order_of_groups]


async def buyurtmalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = fetch_pending_order_groups()
    if not groups:
        await update.message.reply_text("Hozircha bajarilmagan buyurtma yo'q.")
        return

    await update.message.reply_text(f"📋 Bajarilmagan buyurtmalar ({len(groups)} ta):")
    for group in groups:
        text = format_group_text(group)
        button = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"✅ №{group['guruh_id']} topshirildi", callback_data=f"orddone:{group['guruh_id']}")]]
        )
        await update.message.reply_text(text, reply_markup=button)


def get_order_customer(guruh_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT customer FROM orders WHERE guruh_id = ? LIMIT 1", (guruh_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_customer_totals(customer: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(expected_value),0), COALESCE(SUM(received_amount),0) "
        "FROM mijoz_tolovlar WHERE LOWER(customer) = LOWER(?)",
        (customer,),
    )
    row = cur.fetchone()
    conn.close()
    return row[0], row[1]


def start_payment_prompt(context: ContextTypes.DEFAULT_TYPE, guruh_id: int, worker: str) -> str:
    customer = get_order_customer(guruh_id)
    expected_value = compute_order_sale_value(guruh_id)
    context.user_data["awaiting"] = "amount_received_for_order"
    context.user_data["pending_payment"] = {
        "guruh_id": guruh_id,
        "worker": worker,
        "customer": customer,
        "expected_value": expected_value,
    }
    lines = [f"👷 Ishchi: {worker}"]
    if expected_value > 0:
        lines.append(f"💰 Kutilayotgan summa (sotish narxiga ko'ra): {expected_value:,} so'm".replace(",", " "))
    lines.append("\nMijozdan qancha pul olindi? (hali olinmagan bo'lsa 0 yozing)")
    return "\n".join(lines)


async def finalize_payment(user, guruh_id: int, worker: str, customer, expected_value: int, received: int) -> str:
    result_text = await bajarildi_group_core(guruh_id, user, worker)
    record_payment(guruh_id, customer, expected_value, received)

    lines = [result_text, f"\n💵 Mijozdan olindi: {received:,} so'm".replace(",", " ")]
    if customer:
        total_expected, total_received = get_customer_totals(customer)
        qarz = total_expected - total_received
        if qarz > 0:
            lines.append(f"🏪 {customer} — umumiy qarzi: {qarz:,} so'm".replace(",", " "))
        elif qarz < 0:
            lines.append(f"🏪 {customer} — sizga {abs(qarz):,} so'm ortiqcha to'lagan".replace(",", " "))
        else:
            lines.append(f"🏪 {customer} — hisob teng (qarzi yo'q)")
    return "\n".join(lines)


async def orddone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not can_kirim(update):
        await query.answer("Sizda bu amalni bajarish huquqi yo'q.", show_alert=True)
        return
    await query.answer()

    guruh_id = int(query.data.split(":", 1)[1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM workers ORDER BY name")
    workers = [row[0] for row in cur.fetchall()]
    conn.close()

    buttons = [
        [InlineKeyboardButton(w, callback_data=f"workerdone:{guruh_id}:{w}")] for w in workers
    ]
    buttons.append([InlineKeyboardButton("➕ Yangi ishchi", callback_data=f"workerdone:{guruh_id}:__new__")])
    await query.edit_message_text("👷 Buyurtmani kim topshirdi?", reply_markup=InlineKeyboardMarkup(buttons))


async def workerdone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not can_kirim(update):
        await query.answer("Sizda bu amalni bajarish huquqi yo'q.", show_alert=True)
        return
    await query.answer()

    _, guruh_id_str, worker = query.data.split(":", 2)
    guruh_id = int(guruh_id_str)

    if worker == "__new__":
        context.user_data["awaiting"] = "worker_name_for_order"
        context.user_data["pending_order_id"] = guruh_id
        await query.edit_message_text("✍️ Yangi ishchining ismini yozing:")
        return

    prompt = start_payment_prompt(context, guruh_id, worker)
    await query.edit_message_text(prompt, reply_markup=None)


async def bajarildi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_kirim(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Foydalanish: /bajarildi <buyurtma raqami> <ishchi ismi> [olingan summa]\n"
        "Misol: /bajarildi 12 Hojiakbar\n"
        "Yoki summani bir martada: /bajarildi 12 Hojiakbar 1500000"
    )
    if not args or not args[0].isdigit() or len(args) < 2:
        await update.message.reply_text(usage)
        return

    guruh_id = int(args[0])

    if len(args) >= 3 and args[-1].isdigit():
        worker = " ".join(args[1:-1])
        received = int(args[-1])
        customer = get_order_customer(guruh_id)
        expected_value = compute_order_sale_value(guruh_id)
        text = await finalize_payment(update.effective_user, guruh_id, worker, customer, expected_value, received)
        await update.message.reply_text(text)
        return

    worker = " ".join(args[1:])
    prompt = start_payment_prompt(context, guruh_id, worker)
    await update.message.reply_text(prompt)


def compute_order_sale_value(guruh_id: int) -> int:
    """Guruhdagi barcha qatorlar uchun 'sotish' narxlari bo'yicha kutilayotgan
    umumiy summani hisoblaydi (mijozga qancha sotilishi kerak)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT model, item, amount FROM orders WHERE guruh_id = ?", (guruh_id,))
    rows = cur.fetchall()
    total = 0
    for model, item, amount in rows:
        rate = get_rate(cur, "sotish", model, item if item is not None else "komplekt")
        total += rate * amount
    conn.close()
    return total


def record_payment(guruh_id: int, customer: str, expected_value: int, received_amount: int):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO mijoz_tolovlar (guruh_id, customer, expected_value, received_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guruh_id) DO UPDATE SET expected_value = excluded.expected_value, "
        "received_amount = excluded.received_amount, created_at = excluded.created_at",
        (guruh_id, customer or "Nomalum", expected_value, received_amount, now),
    )
    conn.commit()
    conn.close()


def fulfill_single_order(cur, order_id, model, item, amount, worker, user_name, user_id, now):
    """Bitta buyurtma qatorini zaxiradan chiqaradi va (agar worker berilgan bo'lsa)
    to'lovni hisoblab work_log ga yozadi. Natijani (result_lines, payment_total, payment_note) qaytaradi.
    Tranzaksiyani boshqarish (commit/close) chaqiruvchiga qoladi."""
    if item is not None:
        targets = [(model, item)]
    else:
        cur.execute("SELECT item FROM products WHERE model = ?", (model,))
        targets = [(model, row_item) for (row_item,) in cur.fetchall()]

    if not targets:
        return [f"'{model}' modeli uchun hech qanday detal ro'yxatda topilmadi, chiqim qilinmadi."], 0, None

    per_item_qty = {}
    if item is None:
        cur.execute("SELECT item, soni FROM komplekt_tarkibi WHERE model = ?", (model,))
        per_item_qty = dict(cur.fetchall())

    result_lines = []
    for target_model, target_item in targets:
        deduct = amount * per_item_qty.get(target_item, 1) if item is None else amount

        product_key = normalize_product_name(f"{target_model} {target_item}")
        cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
        prow = cur.fetchone()
        current_qty = prow[0] if prow else 0
        new_qty = current_qty - deduct
        shortage = new_qty < 0
        if shortage:
            new_qty = 0

        cur.execute("UPDATE products SET quantity = ? WHERE name = ?", (new_qty, product_key))
        cur.execute(
            """
            INSERT INTO transactions (product, change_type, amount, user_name, user_id, created_at)
            VALUES (?, 'chiqim', ?, ?, ?, ?)
            """,
            (product_key, deduct, user_name, user_id, now),
        )
        warn = " ⚠️ yetarli emas edi!" if shortage else ""
        what = f"{model} komplekt" if item is None else f"{model} {item}"
        result_lines.append(f"• {target_item}: -{deduct}{warn}" if item is None else f"• {what}: -{deduct}{warn}")

    payment_total = 0
    payment_note = None
    if worker:
        rate_key = "komplekt" if item is None else item
        rate = get_rate(cur, "yigish", model, rate_key)
        payment_total = amount * rate

        cur.execute("INSERT OR IGNORE INTO workers (name, created_at) VALUES (?, ?)", (worker, now))
        cur.execute(
            """
            INSERT INTO work_log (worker, turi, order_id, model, item, amount, rate, total, paid, created_at)
            VALUES (?, 'yigish', ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (worker, order_id, model, rate_key, amount, rate, payment_total, now),
        )
        if rate == 0:
            payment_note = f"'{model} {rate_key}' uchun narx belgilanmagan"

    return result_lines, payment_total, payment_note


async def bajarildi_group_core(guruh_id: int, user, worker: str = None) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, model, item, amount FROM orders WHERE guruh_id = ? AND status = 'kutilmoqda'",
        (guruh_id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return f"№{guruh_id} buyurtma topilmadi yoki allaqachon bajarilgan."

    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None
    now = datetime.now().isoformat(timespec="seconds")

    all_result_lines = []
    total_payment = 0
    missing_rates = []
    for order_id, model, item, amount in rows:
        result_lines, payment_total, payment_note = fulfill_single_order(
            cur, order_id, model, item, amount, worker, user_name, user_id, now
        )
        all_result_lines.extend(result_lines)
        total_payment += payment_total
        if payment_note:
            missing_rates.append(payment_note)

    cur.execute("UPDATE orders SET status = 'bajarildi' WHERE guruh_id = ?", (guruh_id,))
    conn.commit()
    conn.close()

    lines = [f"✅ №{guruh_id} buyurtma bajarildi deb belgilandi.", "Zaxiradan chiqarildi:"]
    lines.extend(all_result_lines)
    if worker:
        if total_payment > 0:
            lines.append(f"\n👷 {worker} — yig'ish: {total_payment:,} so'm hisoblandi.".replace(",", " "))
        if missing_rates:
            lines.append("\n⚠️ Narx belgilanmagan: " + ", ".join(missing_rates))
    return "\n".join(lines)


async def mijozhisob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Foydalanish: /mijozhisob <do'kon nomi>\n"
            "Misol: /mijozhisob Mebel For Home"
        )
        return

    customer_query = " ".join(args)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT guruh_id, expected_value, received_amount, created_at FROM mijoz_tolovlar "
        "WHERE LOWER(customer) = LOWER(?) ORDER BY created_at",
        (customer_query,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            f"'{customer_query}' bo'yicha hech qanday yozuv topilmadi.\n"
            "Eslatma: nomi aniq mos kelishi kerak (masalan buyurtmadagi 'Kimdan' nomi bilan bir xil)."
        )
        return

    lines = [f"🏪 {customer_query} — hisob:\n"]
    total_expected = 0
    total_received = 0
    for guruh_id, expected, received, created_at in rows:
        total_expected += expected
        total_received += received
        lines.append(
            f"№{guruh_id}: buyurtma {expected:,} so'm — to'landi {received:,} so'm".replace(",", " ")
        )

    qarz = total_expected - total_received
    lines.append(f"\nJami buyurtma qiymati: {total_expected:,} so'm".replace(",", " "))
    lines.append(f"Jami to'landi: {total_received:,} so'm".replace(",", " "))
    if qarz > 0:
        lines.append(f"\n❗ Qarzi: {qarz:,} so'm".replace(",", " "))
    elif qarz < 0:
        lines.append(f"\n✅ Ortiqcha to'lagan: {abs(qarz):,} so'm".replace(",", " "))
    else:
        lines.append("\n✅ Hisob teng (qarzi yo'q)")

    await update.message.reply_text("\n".join(lines))


async def maosh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    conn = get_conn()
    cur = conn.cursor()

    if args:
        worker = " ".join(args)
        cur.execute(
            """
            SELECT turi, model, item, amount, rate, total, created_at
            FROM work_log WHERE worker = ? AND paid = 0 ORDER BY created_at
            """,
            (worker,),
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(f"👷 {worker} — to'lanmagan ish topilmadi.")
            return

        total_sum = sum(r[5] for r in rows)
        lines = [f"👷 {worker} — to'lanmagan ishlar ({len(rows)} ta):\n"]
        for turi, model, item, amount, rate, total, created_at in rows:
            icon = "📦" if turi == "upakovka" else "🚚"
            model_part = f"{model} " if model else ""
            lines.append(f"{icon} {model_part}{item} x{amount} = {total:,} so'm".replace(",", " "))
        lines.append(f"\n💰 Jami: {total_sum:,} so'm".replace(",", " "))
        lines.append(f"\nTo'langanda: /tolandi {worker}")
        await update.message.reply_text("\n".join(lines))
        return

    cur.execute(
        "SELECT worker, SUM(total) FROM work_log WHERE paid = 0 GROUP BY worker ORDER BY worker"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Hozircha hech kimga to'lanmagan ish yo'q.")
        return

    lines = ["💰 To'lanmagan maoshlar:\n"]
    for worker, total in rows:
        lines.append(f"• {worker}: {total:,} so'm".replace(",", " "))
    lines.append("\nBatafsil: /maosh <ism>")
    await update.message.reply_text("\n".join(lines))


async def tolandi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args:
        await update.message.reply_text("Foydalanish: /tolandi <ishchi ismi>\nMisol: /tolandi Hojiakbar")
        return

    worker = " ".join(args)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT SUM(total), COUNT(*) FROM work_log WHERE worker = ? AND paid = 0", (worker,))
    total, count = cur.fetchone()
    if not total:
        conn.close()
        await update.message.reply_text(f"👷 {worker} — to'lanmagan ish topilmadi.")
        return

    cur.execute("UPDATE work_log SET paid = 1 WHERE worker = ? AND paid = 0", (worker,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ {worker} — {total:,} so'm ({count} ta ish) to'landi deb belgilandi.".replace(",", " ")
    )


async def job_buyurtma_eslatma(context: ContextTypes.DEFAULT_TYPE):
    if WORKER_CHAT_ID is None:
        return

    groups = fetch_pending_order_groups()
    if not groups:
        await context.bot.send_message(chat_id=WORKER_CHAT_ID, text="Hozircha bajarilmagan buyurtma yo'q.")
        return

    text = "\n\n".join(format_group_text(g) for g in groups)
    await context.bot.send_message(chat_id=WORKER_CHAT_ID, text=text)


async def job_kunlik_qoldiq(context: ContextTypes.DEFAULT_TYPE):
    if GROUP_CHAT_ID is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT model, item, quantity FROM products ORDER BY model, item")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return

    lines = ["📦 Kunlik zaxira qoldig'i:"]
    current_model = None
    for model, item, quantity in rows:
        if model != current_model:
            lines.append(f"\n🔹 {model.capitalize()}")
            current_model = model
        lines.append(f"{stock_indicator(quantity)} {item}: {quantity} ta")

    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))


async def job_kam_qoldi(context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT name, quantity FROM products WHERE quantity <= ? ORDER BY quantity ASC, name ASC",
        (LOW_STOCK_THRESHOLD,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return

    lines = [f"⚠️ Kam qolgan mahsulotlar ({LOW_STOCK_THRESHOLD} tadan kam):\n"]
    for name, quantity in rows:
        lines.append(f"• {name}: {quantity} ta")
    lines.append("\nKesish xizmatiga buyurtma berishni unutmang.")

    await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines))


async def job_haftalik_hisobot(context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID is None:
        return

    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT product, SUM(amount) FROM transactions
        WHERE change_type = 'chiqim' AND created_at >= ?
        GROUP BY product ORDER BY SUM(amount) DESC
        """,
        (week_ago,),
    )
    sold_rows = cur.fetchall()

    cur.execute(
        """
        SELECT p.model, SUM(t.amount) FROM transactions t
        JOIN products p ON p.name = t.product
        WHERE t.change_type = 'chiqim' AND t.created_at >= ?
        GROUP BY p.model ORDER BY SUM(t.amount) DESC
        """,
        (week_ago,),
    )
    model_rows = cur.fetchall()
    conn.close()

    if not sold_rows:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text="📊 Haftalik hisobot:\n\nBu hafta hech qanday chiqim (sotuv) qayd etilmagan.",
        )
        return

    total_sold = sum(amount for _, amount in sold_rows)
    lines = [f"📊 Haftalik hisobot (oxirgi 7 kun):\n", f"Jami sotildi: {total_sold} ta\n"]

    lines.append("Mahsulotlar bo'yicha:")
    for product, amount in sold_rows:
        lines.append(f"• {product}: {amount} ta")

    if model_rows:
        top_model, top_amount = model_rows[0]
        lines.append(f"\n🏆 Eng ko'p sotilgan model: {top_model.capitalize()} ({top_amount} ta)")

    await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines))


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "BOT_TOKEN muhit o'zgaruvchisi topilmadi. "
            "Botni ishga tushirishdan oldin BOT_TOKEN ni sozlang."
        )

    init_db()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "yordam", "help"], start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("buyurtmaguruhi", buyurtmaguruhi))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['qoldiq']}$"), qoldiq))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['modellar']}$"), modellar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['buyurtmalar']}$"), buyurtmalar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['yordam']}$"), start))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['kirim']}$"), kirim_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['chiqim']}$"), chiqim_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['yangi_buyurtma']}$"), buyurtma_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{FINISH_BUTTON}$"), tayyor_button))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO) & filters.ChatType.GROUPS & ~filters.COMMAND,
        group_order_intake,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_awaiting_text))
    app.add_handler(CommandHandler("kirim", kirim))
    app.add_handler(CommandHandler("chiqim", chiqim))
    app.add_handler(CommandHandler("qoldiq", qoldiq))
    app.add_handler(CommandHandler("modellar", modellar))
    app.add_handler(CommandHandler("tarix", tarix))
    app.add_handler(CommandHandler("ochir", ochir))
    app.add_handler(CommandHandler("tozalash", tozalash))
    app.add_handler(CommandHandler("modelnomi", modelnomi))
    app.add_handler(CommandHandler("komplekttarkibi", komplekttarkibi))
    app.add_handler(CommandHandler("narx", narx))
    app.add_handler(CommandHandler("modelnarx", modelnarx))
    app.add_handler(CommandHandler("narxlar", narxlar))
    app.add_handler(CommandHandler("ishchilar", ishchilar))
    app.add_handler(CommandHandler("ishchiulash", ishchiulash))
    app.add_handler(CommandHandler("maosh", maosh))
    app.add_handler(CommandHandler("mijozhisob", mijozhisob))
    app.add_handler(CommandHandler("tolandi", tolandi))
    app.add_handler(CommandHandler("detalnomi", detalnomi))
    app.add_handler(CommandHandler("royxatga", royxatga))
    app.add_handler(CommandHandler("buyurtma", buyurtma))
    app.add_handler(CommandHandler("buyurtmalar", buyurtmalar))
    app.add_handler(CommandHandler("bajarildi", bajarildi))
    app.add_handler(CommandHandler("bekor", bekor))
    app.add_handler(CommandHandler("guruhlash", guruhlash))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(ob_callback, pattern=r"^ob:"))
    app.add_handler(CallbackQueryHandler(orddone_callback, pattern=r"^orddone:"))
    app.add_handler(CallbackQueryHandler(workerdone_callback, pattern=r"^workerdone:"))
    app.add_handler(CallbackQueryHandler(sb_callback, pattern=r"^sb:"))
    app.add_handler(CallbackQueryHandler(gord_callback, pattern=r"^gord:"))

    if app.job_queue is not None:
        # Har kuni ertalab soat 9:00 (Toshkent vaqti) kam qolgan mahsulotlarni tekshiradi.
        app.job_queue.run_daily(
            job_kam_qoldi, time=time(hour=9, minute=0, tzinfo=TASHKENT_TZ)
        )
        # Har kuni ertalab soat 9:00 (Toshkent vaqti) guruhga to'liq qoldiqni yuboradi.
        app.job_queue.run_daily(
            job_kunlik_qoldiq, time=time(hour=9, minute=0, tzinfo=TASHKENT_TZ)
        )
        # Har kuni soat 9:00 va 14:00 da ishchiga bajarilmagan buyurtmalarni eslatadi.
        app.job_queue.run_daily(
            job_buyurtma_eslatma, time=time(hour=9, minute=0, tzinfo=TASHKENT_TZ)
        )
        app.job_queue.run_daily(
            job_buyurtma_eslatma, time=time(hour=14, minute=0, tzinfo=TASHKENT_TZ)
        )
        # Har yakshanba kuni soat 20:00 (Toshkent vaqti) haftalik hisobot yuboradi.
        app.job_queue.run_daily(
            job_haftalik_hisobot,
            time=time(hour=20, minute=0, tzinfo=TASHKENT_TZ),
            days=(6,),
        )
    else:
        logger.warning(
            "job_queue mavjud emas - avtomatik ogohlantirish va hisobot ishlamaydi. "
            "requirements.txt da 'python-telegram-bot[job-queue]' borligini tekshiring."
        )

    if OWNER_ID is None:
        logger.warning(
            "OWNER_ID sozlanmagan - hamma foydalanuvchi kirim/chiqim qila oladi."
        )
    else:
        logger.info("OWNER_ID sozlangan: %s", OWNER_ID)

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
