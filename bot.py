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
    ReactionTypeEmoji,
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
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__)).strip()
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
    if "mod_type" not in orders_columns:
        # NULL = oddiy qator, '+' = komplektga qo'shilgan qo'shimcha, '-' = komplektdan ayirilgan (berilmaydi)
        cur.execute("ALTER TABLE orders ADD COLUMN mod_type TEXT")
    if "bajarildi_at" not in orders_columns:
        # Buyurtma haqiqatda qachon 'topshirildi' deb belgilanganini saqlaydi (hisobot uchun).
        cur.execute("ALTER TABLE orders ADD COLUMN bajarildi_at TEXT")
    if "dastavka" not in orders_columns:
        # 1 = mijoz uyiga o'rnatib berish yo'q, faqat jo'natib yuboriladi (masalan viloyatga).
        # Bunday holda o'rnatish xizmati narxi ayiriladi, ishchiga yig'ish puli yozilmaydi.
        cur.execute("ALTER TABLE orders ADD COLUMN dastavka INTEGER NOT NULL DEFAULT 0")
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
        CREATE TABLE IF NOT EXISTS qoshimcha_detallar (
            model TEXT NOT NULL COLLATE NOCASE,  -- '' = barcha modellarga tegishli
            item TEXT NOT NULL COLLATE NOCASE,
            PRIMARY KEY (model, item)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS xomashyo (
            name TEXT PRIMARY KEY COLLATE NOCASE,
            quantity INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS xomashyo_tarkibi (
            model TEXT NOT NULL COLLATE NOCASE,  -- '' = barcha modellarga tegishli
            item TEXT NOT NULL COLLATE NOCASE,
            xomashyo TEXT NOT NULL COLLATE NOCASE,
            miqdor INTEGER NOT NULL,
            PRIMARY KEY (model, item, xomashyo)
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
    if "source_message_id" not in pending_columns:
        cur.execute("ALTER TABLE pending_group_orders ADD COLUMN source_message_id INTEGER")

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
    "mijozlar": "🏪 Mijozlar",
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [MENU_BUTTONS["kirim"], MENU_BUTTONS["chiqim"]],
        [MENU_BUTTONS["qoldiq"], MENU_BUTTONS["modellar"]],
        [MENU_BUTTONS["yangi_buyurtma"], MENU_BUTTONS["buyurtmalar"]],
        [MENU_BUTTONS["mijozlar"], MENU_BUTTONS["yordam"]],
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

    try:
        amount = parse_amount(amount_raw)
    except ValueError:
        await update.effective_message.reply_text("Miqdor musbat butun son bo'lishi kerak. Misol: 5")
        return

    user = update.effective_user
    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None

    conn = get_conn()
    cur = conn.cursor()

    # Ko'p so'zli modellarni (masalan 'bella spalniy') to'g'ri aniqlash uchun avval
    # bazadagi mavjud modellar bilan solishtiramiz (eng uzunidan boshlab).
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    all_models.sort(key=lambda m: -len(m.split()))
    lowered_parts = [p.lower() for p in name_parts]

    model = None
    item = None
    for candidate in all_models:
        candidate_tokens = candidate.split()
        if lowered_parts[: len(candidate_tokens)] == candidate_tokens:
            model = candidate
            item = " ".join(name_parts[len(candidate_tokens):]).strip().lower()
            break

    if model is None:
        # Hech qanday mavjud modelga mos kelmadi - yangi model deb, birinchi so'zni
        # model deb olamiz (eski, oddiy xatti-harakat).
        model, item = split_model_item(product_display)

    if not item:
        conn.close()
        await update.effective_message.reply_text(
            "Mahsulot nomini <model> <detal> ko'rinishida yozing.\nMisol: laura tumba"
        )
        return

    product_key = normalize_product_name(f"{model} {item}")

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
    xom_line = ""
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

        # Tayyor mahsulot ishlab chiqarilgani uchun, unga ketadigan xomashyolarni
        # (agar tarkibi belgilangan bo'lsa) avtomatik omborxonadan ayirib boramiz.
        xom_needs = get_xom_requirements(cur, model, item)
        if xom_needs:
            xom_lines = []
            for xomashyo, per_unit in xom_needs.items():
                need = per_unit * amount
                cur.execute("SELECT quantity FROM xomashyo WHERE name = ?", (xomashyo,))
                xrow = cur.fetchone()
                current_xom = xrow[0] if xrow else 0
                new_xom = current_xom - need
                shortage = new_xom < 0
                if shortage:
                    new_xom = 0
                cur.execute(
                    "INSERT INTO xomashyo (name, quantity) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET quantity = excluded.quantity",
                    (xomashyo, new_xom),
                )
                warn = " ⚠️ yetarli emas edi!" if shortage else ""
                xom_lines.append(f"• {xomashyo}: -{need}{warn}")
            xom_line = "\n🧵 Xomashyodan ayirildi:\n" + "\n".join(xom_lines)

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
    if xom_line:
        text += xom_line
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
    if ob["komplekt"]:
        lines.append(f"\n✅ Komplekt: {ob['komplekt_qty']} ta")
    if ob["items"]:
        lines.append("\nTanlanganlar:")
        for it, qty in ob["items"].items():
            lines.append(f"✅ {it}: {qty} ta")
    if ob.get("extra_items"):
        lines.append("\nBoshqa modellardan qo'shilganlar:")
        for (m, it), qty in ob["extra_items"].items():
            lines.append(f"✅ {m}: {it} — {qty} ta")
    return "\n".join(lines)


def ob_item_keyboard(ob):
    buttons = []
    for it in ob["item_list"]:
        label = f"✅ {it} ({ob['items'][it]})" if it in ob["items"] else it
        buttons.append([InlineKeyboardButton(label, callback_data=f"ob:item:{ob['item_list'].index(it)}")])
    buttons.append([InlineKeyboardButton("📦 Komplekt (barchasi)", callback_data="ob:komplekt")])
    buttons.append([InlineKeyboardButton("🔁 Boshqa modeldan qo'shish", callback_data="ob:othermodel")])
    if ob["items"] or ob.get("extra_items") or ob["komplekt"]:
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
    if ob["qty_mode"] == "other_item":
        return f"📐 {ob['qty_other_model']}: {ob['qty_item']}: nechta?\n\nHozirgi son: {ob['qty_value']} ta"
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

    entries = []  # (model, item_or_None, amount)
    if ob["komplekt"]:
        entries.append((model, None, ob["komplekt_qty"]))
    else:
        for item, qty in ob["items"].items():
            entries.append((model, item, qty))
    for (other_model, item), qty in ob.get("extra_items", {}).items():
        entries.append((other_model, item, qty))

    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    created = []
    for entry_model, item, amount in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (entry_model, item, amount, deadline.isoformat(), deadline_display, customer, now),
        )
        created.append((cur.lastrowid, entry_model, item, amount))

    guruh_id = created[0][0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created)})",
        [guruh_id] + [oid for oid, _, _, _ in created],
    )

    entries_4 = [(m, i, a, None) for m, i, a in entries]
    shortage_text = shortage_warning_for_new_order(cur, entries_4)

    conn.commit()
    conn.close()

    what_all = ", ".join(
        (f"{entry_model} komplekt" if item is None else f"{entry_model} {item} ({amount} ta)")
        for _, entry_model, item, amount in created
    )
    lines = [f"📝 Yangi buyurtma qabul qilindi (№{guruh_id}):", what_all]
    lines.append(f"Muddat: {deadline_display}")
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")
    if shortage_text:
        lines.append(shortage_text)

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
            "extra_items": {},
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

    if data == "ob:othermodel":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT model FROM products ORDER BY model")
        other_models = [row[0] for row in cur.fetchall() if row[0] != ob["model"]]
        conn.close()
        if not other_models:
            await query.answer("Boshqa model topilmadi.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(m.capitalize(), callback_data=f"ob:othermodel:pick:{m}")]
            for m in other_models
        ]
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ob:othermodel:back")])
        await query.edit_message_text("🔁 Qaysi modeldan detal qo'shamiz?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "ob:othermodel:back":
        await ob_show_item_menu(query, ob)
        return

    if data.startswith("ob:othermodel:pick:"):
        other_model = data.split(":", 3)[3]
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT item FROM products WHERE model = ? ORDER BY item", (other_model,))
        other_item_list = [row[0] for row in cur.fetchall()]
        conn.close()
        ob["other_model_pending"] = other_model
        ob["other_item_list"] = other_item_list
        buttons = [
            [InlineKeyboardButton(it, callback_data=f"ob:othermodel:item:{i}")]
            for i, it in enumerate(other_item_list)
        ]
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ob:othermodel")])
        await query.edit_message_text(
            f"🔁 {other_model.capitalize()} — qaysi detal?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("ob:othermodel:item:"):
        idx = int(data.split(":", 3)[3])
        item = ob["other_item_list"][idx]
        ob["qty_mode"] = "other_item"
        ob["qty_other_model"] = ob["other_model_pending"]
        ob["qty_item"] = item
        ob["qty_value"] = ob["extra_items"].get((ob["qty_other_model"], item), 1)
        await query.edit_message_text(ob_qty_text(ob), reply_markup=ob_qty_keyboard())
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
            await ob_show_item_menu(query, ob)
        elif ob["qty_mode"] == "other_item":
            key = (ob["qty_other_model"], ob["qty_item"])
            ob["extra_items"][key] = ob["qty_value"]
            await ob_show_item_menu(query, ob)
        else:
            ob["items"][ob["qty_item"]] = ob["qty_value"]
            ob["komplekt"] = False
            await ob_show_item_menu(query, ob)
        return

    if data == "ob:items:done":
        if not ob["items"] and not ob["komplekt"] and not ob.get("extra_items"):
            await query.answer("Kamida bitta detal, komplekt, yoki boshqa modeldan detal tanlang.", show_alert=True)
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


def format_money(amount: int, currency: str = "som") -> str:
    """Summani chiroyli formatlaydi. currency='usd' bo'lsa dollar belgisi bilan,
    aks holda so'm bilan (ming ajratuvchi bo'shliq)."""
    formatted = f"{amount:,}".replace(",", " ")
    if currency == "usd":
        return f"{formatted}$"
    return f"{formatted} so'm"


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


async def buyurtmalartozalash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or args[0].upper() != "TASDIQ":
        await update.message.reply_text(
            "Bu buyruq BARCHA buyurtmalar tarixini butunlay o'chiradi:\n"
            "• Buyurtmalar (kutilayotgan va bajarilgan)\n"
            "• Ishchi puli yozuvlari (ish haqi tarixi)\n"
            "• Mijozlar bilan hisob-kitob (kim qancha to'lagan)\n"
            "• Guruhdan kelgan tasdiqlanmagan takliflar\n\n"
            "❗ Narxlar, model ro'yxati, ishchi bog'lanishi, sozlamalar TEGILMAYDI - "
            "faqat buyurtma tarixi tozalanadi.\n\n"
            "Tasdiqlash uchun aynan shu yozing:\n"
            "/buyurtmalartozalash TASDIQ"
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders")
    orders_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM work_log WHERE turi = 'yigish'")
    work_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM mijoz_tolovlar")
    tolov_count = cur.fetchone()[0]

    cur.execute("DELETE FROM orders")
    cur.execute("DELETE FROM work_log WHERE turi = 'yigish'")
    cur.execute("DELETE FROM mijoz_tolovlar")
    cur.execute("DELETE FROM pending_group_orders")
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🧹 Tozalandi:\n"
        f"• {orders_count} ta buyurtma qatori o'chirildi\n"
        f"• {work_count} ta yig'ish puli yozuvi o'chirildi\n"
        f"• {tolov_count} ta mijoz to'lov yozuvi o'chirildi\n\n"
        "Narxlar, modellar va sozlamalar saqlanib qoldi. "
        "Endi /buyurtma orqali haqiqiy buyurtmalarni qaytadan kiritishingiz mumkin."
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
        "Misol: /narx sotishayirish krovat 800000  (komplektdan ayirilganda kamayadigan summa)\n"
        "Komplekt uchun: /narx yigish komplekt 100000\n\n"
        "Bitta modelga maxsus narx uchun: /modelnarx <upakovka|yigish|sotish> <model> <detal> <summa>"
    )
    if len(args) < 3 or args[0].lower() not in ("upakovka", "yigish", "sotish", "sotishayirish", "ornatish") or not args[-1].isdigit():
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

    turi_label = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish", "sotishayirish": "Sotish (ayirish)", "ornatish": "O'rnatish xizmati"}[turi]
    currency = "usd" if turi in ("sotish", "sotishayirish", "ornatish") else "som"
    await update.message.reply_text(
        f"✅ {turi_label} — '{item}' (barcha modellar) narxi: {format_money(rate, currency)} deb belgilandi."
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
    if len(args) < 4 or args[0] not in ("upakovka", "yigish", "sotish", "sotishayirish", "ornatish") or not args[-1].isdigit():
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

    turi_label = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish", "sotishayirish": "Sotish (ayirish)", "ornatish": "O'rnatish xizmati"}[turi]
    currency = "usd" if turi in ("sotish", "sotishayirish", "ornatish") else "som"
    await update.message.reply_text(
        f"✅ {turi_label} — '{model} {item}' uchun maxsus narx: {format_money(rate, currency)}."
    )


async def narxtozalash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    conn = get_conn()
    cur = conn.cursor()
    # Eski xato: bir xabarda bir nechta /narx qatori yuborilganda, ular bitta uzun
    # "detal" nomiga yopishib qolgan (ichida '/narx' so'zi bor). Shularni topib tozalaymiz.
    cur.execute("SELECT turi, model, item, rate FROM narxlar WHERE item LIKE '%/narx%'")
    rows = cur.fetchall()

    if not rows:
        conn.close()
        await update.message.reply_text("Hech qanday chalkash yozuv topilmadi. Ro'yxat toza.")
        return

    cur.execute("DELETE FROM narxlar WHERE item LIKE '%/narx%'")
    conn.commit()
    conn.close()

    lines = [f"🧹 {len(rows)} ta chalkash yozuv o'chirildi:\n"]
    for turi, model, item, rate in rows:
        prefix = f"{model} " if model else ""
        lines.append(f"• [{turi}] {prefix}{item[:40]}...")
    await update.message.reply_text("\n".join(lines))


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
    turi_icons = {"upakovka": "📦", "yigish": "🚚", "sotish": "🏷️", "sotishayirish": "➖", "ornatish": "🔧"}
    turi_labels = {"upakovka": "Upakovka", "yigish": "Yig'ish", "sotish": "Sotish (qo'shilganda)", "sotishayirish": "Sotish (ayirilganda)", "ornatish": "O'rnatish xizmati"}
    for turi, model, item, rate in rows:
        if turi != current_turi:
            icon = turi_icons.get(turi, "•")
            label = turi_labels.get(turi, turi)
            lines.append(f"\n{icon} {label}:")
            current_turi = turi
        currency = "usd" if turi in ("sotish", "sotishayirish", "ornatish") else "som"
        if model:
            lines.append(f"• {model} {item} (maxsus): {format_money(rate, currency)}")
        else:
            lines.append(f"• {item}: {format_money(rate, currency)}")
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


async def qoshimchadetal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Bu detal oddiy 'komplekt' buyurtma qilinganda AVTOMATIK qo'shilmasin, "
        "faqat maxsus (+detal) bilan buyurtma qilinganda hisoblansin, deb belgilaydi.\n\n"
        "Foydalanish: /qoshimchadetal <detal> - barcha modellar uchun\n"
        "Yoki: /qoshimchadetal <model> <detal> - faqat bitta model uchun\n\n"
        "Ro'yxatni ko'rish: /qoshimchadetallar\n"
        "Olib tashlash: /qoshimchadetalochirish <detal> (yoki <model> <detal>)"
    )
    if not args or len(args) > 2:
        await update.message.reply_text(usage)
        return

    if len(args) == 1:
        model, item = "", args[0].lower()
    else:
        model, item = args[0].lower(), args[1].lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO qoshimcha_detallar (model, item) VALUES (?, ?)", (model, item)
    )
    conn.commit()
    conn.close()

    scope = "barcha modellar uchun" if not model else f"faqat '{model}' uchun"
    await update.message.reply_text(
        f"✅ '{item}' endi qo'shimcha detal deb belgilandi ({scope}). "
        f"Oddiy 'komplekt' buyurtmasida avtomatik hisoblanmaydi, faqat '+{item}' bilan qo'shilganda ishlaydi."
    )


async def narxochirish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Bitta maxsus (model-specific) narx yozuvini o'chiradi (masalan ishlatilmay qolgan mahsulot narxi).\n\n"
        "Foydalanish: /narxochirish <upakovka|yigish|sotish|sotishayirish|kesim> <model> <detal>\n"
        "Misol: /narxochirish sotish neo shkaf"
    )
    if len(args) < 3 or args[0].lower() not in ("upakovka", "yigish", "sotish", "sotishayirish", "ornatish", "kesim"):
        await update.message.reply_text(usage)
        return

    turi = args[0].lower()
    model = args[1].lower()
    item = " ".join(args[2:]).lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM narxlar WHERE turi = ? AND model = ? AND item = ?", (turi, model, item)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await update.message.reply_text(f"✅ '{model} {item}' ({turi}) narxi o'chirildi.")
    else:
        await update.message.reply_text(f"'{model} {item}' ({turi}) uchun maxsus narx topilmadi.")


async def qoshimchadetalochirish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or len(args) > 2:
        await update.message.reply_text(
            "Foydalanish: /qoshimchadetalochirish <detal> (yoki <model> <detal>)"
        )
        return

    if len(args) == 1:
        model, item = "", args[0].lower()
    else:
        model, item = args[0].lower(), args[1].lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM qoshimcha_detallar WHERE model = ? AND item = ?", (model, item))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await update.message.reply_text(f"✅ '{item}' qo'shimcha-detal ro'yxatidan olib tashlandi.")
    else:
        await update.message.reply_text(f"'{item}' bu ro'yxatda topilmadi.")


async def qoshimchadetallar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT model, item FROM qoshimcha_detallar ORDER BY model, item")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "Hozircha hech qanday detal 'qo'shimcha' deb belgilanmagan.\n"
            "Belgilash: /qoshimchadetal <detal>"
        )
        return

    lines = ["🧩 Qo'shimcha detallar (oddiy komplektga avtomatik kirmaydi):\n"]
    for model, item in rows:
        scope = "barcha modellar" if not model else model
        lines.append(f"• {item} ({scope})")
    await update.message.reply_text("\n".join(lines))


def get_xom_requirements(cur, model: str, item: str):
    """Berilgan model+detal uchun kerak bo'ladigan xomashyolar ro'yxatini qaytaradi
    {xomashyo: miqdor}. Model-maxsus yozuv umumiy (model='') yozuvni ustidan yozadi."""
    cur.execute("SELECT xomashyo, miqdor FROM xomashyo_tarkibi WHERE model = ''  AND item = ?", (item,))
    result = dict(cur.fetchall())
    cur.execute("SELECT xomashyo, miqdor FROM xomashyo_tarkibi WHERE model = ? AND item = ?", (model, item))
    result.update(dict(cur.fetchall()))
    return result


async def xomkirim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_kirim(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) < 2 or not args[-1].isdigit():
        await update.message.reply_text(
            "Xomashyo (aksessuar) omboriga qo'shish.\n\n"
            "Foydalanish: /xomkirim <nom> <miqdor>\n"
            "Misol: /xomkirim ilgak 500"
        )
        return

    name = " ".join(args[:-1]).lower()
    amount = int(args[-1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO xomashyo (name, quantity) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET quantity = quantity + excluded.quantity",
        (name, amount),
    )
    cur.execute("SELECT quantity FROM xomashyo WHERE name = ?", (name,))
    new_qty = cur.fetchone()[0]
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ '{name}': +{amount} ta. Yangi qoldiq: {new_qty} ta.")


async def xomqoldiq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, quantity FROM xomashyo ORDER BY name")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Xomashyo ombori hozircha bo'sh. Qo'shish: /xomkirim <nom> <miqdor>")
        return

    lines = ["🧵 Xomashyo qoldig'i:\n"]
    for name, qty in rows:
        lines.append(f"{stock_indicator(qty)} {name}: {qty} ta")
    await update.message.reply_text("\n".join(lines))


async def xomtarkibi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Bitta DETAL uchun (masalan shkaf) qancha xomashyo ketishini belgilaydi (BARCHA modellar uchun).\n\n"
        "Ko'rish: /xomtarkibi <detal>\n"
        "Belgilash: /xomtarkibi <detal> <xomashyo> <miqdor>\n"
        "Misol: /xomtarkibi shkaf ilgak 8\n\n"
        "Faqat bitta modelga maxsus qilish uchun: /xommodeltarkibi <model> <detal> <xomashyo> <miqdor>"
    )
    if not args:
        await update.message.reply_text(usage)
        return

    conn = get_conn()
    cur = conn.cursor()

    if len(args) == 1:
        item = args[0].lower()
        cur.execute("SELECT xomashyo, miqdor FROM xomashyo_tarkibi WHERE model = '' AND item = ?", (item,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text(f"'{item}' uchun hali xomashyo tarkibi belgilanmagan.\n\n{usage}")
            return
        lines = [f"🧩 '{item}' uchun xomashyo tarkibi (barcha modellar):\n"]
        lines.extend(f"• {x}: {m} ta" for x, m in rows)
        await update.message.reply_text("\n".join(lines))
        return

    if len(args) != 3 or not args[-1].isdigit():
        conn.close()
        await update.message.reply_text(usage)
        return

    item, xomashyo, miqdor = args[0].lower(), args[1].lower(), int(args[2])
    cur.execute(
        "INSERT INTO xomashyo_tarkibi (model, item, xomashyo, miqdor) VALUES ('', ?, ?, ?) "
        "ON CONFLICT(model, item, xomashyo) DO UPDATE SET miqdor = excluded.miqdor",
        (item, xomashyo, miqdor),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ '{item}' uchun (barcha modellar): 1 tasiga {miqdor} ta '{xomashyo}' kerak deb belgilandi."
    )


async def dastavka_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Buyurtmani 'faqat jo'natiladi, o'rnatilmaydi' (masalan viloyatga) deb "
            "belgilaydi/bekor qiladi. Bunday holda sotish narxidan o'rnatish xizmati "
            "ayiriladi, ishchiga yig'ish puli yozilmaydi.\n\n"
            "Foydalanish: /dastavka <buyurtma raqami>\n"
            "Misol: /dastavka 80"
        )
        return

    guruh_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT dastavka, status FROM orders WHERE guruh_id = ? LIMIT 1", (guruh_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        await update.message.reply_text(f"№{guruh_id} buyurtma topilmadi.")
        return

    current, status = row
    new_value = 0 if current else 1
    cur.execute("UPDATE orders SET dastavka = ? WHERE guruh_id = ?", (new_value, guruh_id))
    conn.commit()
    conn.close()

    if new_value:
        await update.message.reply_text(
            f"🚚 №{guruh_id} endi 'dastavka' (faqat jo'natiladi, o'rnatilmaydi) deb belgilandi."
        )
    else:
        await update.message.reply_text(f"↩️ №{guruh_id} uchun 'dastavka' belgisi olib tashlandi.")


async def hisobtuzatish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Buyurtma qiymatini HOZIRGI narxlar asosida qayta hisoblab, to'g'irlaydi "
            "(masalan narx keyinroq to'g'irlangan bo'lsa).\n\n"
            "Foydalanish: /hisobtuzatish <buyurtma raqami>\n"
            "Misol: /hisobtuzatish 77"
        )
        return

    guruh_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT expected_value, received_amount, customer FROM mijoz_tolovlar WHERE guruh_id = ?", (guruh_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        await update.message.reply_text(f"№{guruh_id} uchun to'lov yozuvi topilmadi.")
        return

    old_expected, received, customer = row
    new_expected = compute_order_sale_value(guruh_id)
    cur.execute("UPDATE mijoz_tolovlar SET expected_value = ? WHERE guruh_id = ?", (new_expected, guruh_id))
    conn.commit()
    conn.close()

    lines = [
        f"✅ №{guruh_id} qiymati tuzatildi: {format_money(old_expected, 'usd')} → {format_money(new_expected, 'usd')}"
    ]
    if customer:
        total_expected, total_received = get_customer_totals(customer)
        qarz = total_expected - total_received
        if qarz > 0:
            lines.append(f"🏪 {customer} — yangi umumiy qarzi: {format_money(qarz, 'usd')}")
        elif qarz < 0:
            lines.append(f"🏪 {customer} — sizga {format_money(abs(qarz), 'usd')} ortiqcha to'lagan")
        else:
            lines.append(f"🏪 {customer} — hisob teng (qarzi yo'q)")
    await update.message.reply_text("\n".join(lines))


async def ishchinomitolash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Xato kiritilgan ishchi ismini to'g'irlaydi (barcha yozuvlarda).\n\n"
            "Foydalanish: /ishchinomitolash <eski ism> <yangi ism>\n"
            "Misol: /ishchinomitolash 350 Hojiakbar"
        )
        return

    old_name, new_name = args[0], args[1]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE work_log SET worker = ? WHERE worker = ?", (new_name, old_name))
    work_log_count = cur.rowcount
    cur.execute("UPDATE workers SET name = ? WHERE name = ?", (new_name, old_name))
    cur.execute("INSERT OR IGNORE INTO workers (name, created_at) VALUES (?, ?)", (new_name, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ '{old_name}' → '{new_name}' deb to'g'irlandi ({work_log_count} ta ish yozuvida)."
    )


async def kirimtuzatish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_kirim(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Xato ko'p kiritilgan /kirim ni to'g'irlaydi - zaxirani, ishchi puli hisobini va "
        "xomashyo ayirilishini HAMMASINI birdek tuzatadi.\n\n"
        "Foydalanish: /kirimtuzatish <model> <detal> <ortiqcha miqdor>\n"
        "Misol: 4 ta kiritilgan, aslida 2 ta bo'lishi kerak edi (ortiqcha 2 ta):\n"
        "/kirimtuzatish bella spalniy kamod 2"
    )
    if len(args) < 3 or not args[-1].isdigit():
        await update.message.reply_text(usage)
        return

    excess = int(args[-1])
    middle = args[:-1]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    all_models.sort(key=lambda m: -len(m.split()))

    matched_model = None
    item = None
    for candidate in all_models:
        candidate_tokens = candidate.lower().split()
        lowered_middle = [t.lower() for t in middle]
        if lowered_middle[: len(candidate_tokens)] == candidate_tokens:
            matched_model = candidate.lower()
            item = " ".join(middle[len(candidate_tokens):]).lower()
            break

    if matched_model is None or not item:
        conn.close()
        await update.message.reply_text(
            f"Model yoki detal aniqlanmadi.\n\n{usage}\n\nMavjud modellar: {', '.join(sorted(set(all_models)))}"
        )
        return

    product_key = normalize_product_name(f"{matched_model} {item}")
    cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
    prow = cur.fetchone()
    if prow is None:
        conn.close()
        await update.message.reply_text(f"'{matched_model} {item}' topilmadi.")
        return

    current_qty = prow[0]
    new_qty = max(0, current_qty - excess)
    actual_removed = current_qty - new_qty
    cur.execute("UPDATE products SET quantity = ? WHERE name = ?", (new_qty, product_key))

    user = update.effective_user
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO transactions (product, change_type, amount, user_name, user_id, created_at) "
        "VALUES (?, 'chiqim', ?, ?, ?, ?)",
        (product_key, actual_removed, user.full_name if user else "noma'lum", user.id if user else None, now),
    )

    lines = [
        f"↩️ Tuzatildi: '{matched_model} {item}' dan {actual_removed} ta ayirildi. "
        f"Yangi qoldiq: {new_qty} ta."
    ]

    cur.execute(
        """
        SELECT id, worker, amount, rate FROM work_log
        WHERE turi = 'upakovka' AND model = ? AND item = ? AND paid = 0
        ORDER BY created_at DESC LIMIT 1
        """,
        (matched_model, item),
    )
    wrow = cur.fetchone()
    if wrow:
        wid, worker, wamount, rate = wrow
        reduce_by = min(excess, wamount)
        new_wamount = wamount - reduce_by
        new_wtotal = new_wamount * rate
        if new_wamount > 0:
            cur.execute("UPDATE work_log SET amount = ?, total = ? WHERE id = ?", (new_wamount, new_wtotal, wid))
            lines.append(
                f"👷 {worker} — upakovka puli tuzatildi: {wamount} ta → {new_wamount} ta "
                f"({format_money(new_wtotal, 'som')})"
            )
        else:
            cur.execute("DELETE FROM work_log WHERE id = ?", (wid,))
            lines.append(f"👷 {worker} — bu ishga hisoblangan upakovka puli butunlay bekor qilindi.")

    xom_needs = get_xom_requirements(cur, matched_model, item)
    if xom_needs:
        xom_lines = []
        for xomashyo, per_unit in xom_needs.items():
            restore = per_unit * excess
            cur.execute("SELECT quantity FROM xomashyo WHERE name = ?", (xomashyo,))
            xrow = cur.fetchone()
            current_xom = xrow[0] if xrow else 0
            new_xom = current_xom + restore
            cur.execute(
                "INSERT INTO xomashyo (name, quantity) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET quantity = excluded.quantity",
                (xomashyo, new_xom),
            )
            xom_lines.append(f"• {xomashyo}: +{restore}")
        lines.append("🧵 Xomashyoga qaytarildi:\n" + "\n".join(xom_lines))

    conn.commit()
    conn.close()
    await update.message.reply_text("\n".join(lines))


async def xommodeltarkibi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Foydalanish: /xommodeltarkibi <model> <detal> <xomashyo> <miqdor>\n"
        "Misol: /xommodeltarkibi neo shkaf ilgak 12"
    )
    if len(args) != 4 or not args[-1].isdigit():
        await update.message.reply_text(usage)
        return

    model, item, xomashyo, miqdor = args[0].lower(), args[1].lower(), args[2].lower(), int(args[3])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO xomashyo_tarkibi (model, item, xomashyo, miqdor) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(model, item, xomashyo) DO UPDATE SET miqdor = excluded.miqdor",
        (model, item, xomashyo, miqdor),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ '{model} {item}' uchun (maxsus): 1 tasiga {miqdor} ta '{xomashyo}' kerak deb belgilandi."
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


async def detalochirish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    usage = (
        "Bitta model ichidan bitta detalni ro'yxatdan o'chiradi (faqat 0 bo'lsa ishlaydi).\n\n"
        "Foydalanish: /detalochirish <model> <detal>\n"
        "Misol: /detalochirish neo krovat110"
    )
    if len(args) < 2:
        await update.message.reply_text(usage)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT model FROM products")
    all_models = [row[0] for row in cur.fetchall()]
    all_models.sort(key=lambda m: -len(m.split()))
    lowered = [a.lower() for a in args]

    model = None
    item = None
    for candidate in all_models:
        candidate_tokens = candidate.split()
        if lowered[: len(candidate_tokens)] == candidate_tokens:
            model = candidate
            item = " ".join(args[len(candidate_tokens):]).strip().lower()
            break

    if model is None or not item:
        conn.close()
        await update.message.reply_text(f"Model yoki detal aniqlanmadi.\n\n{usage}")
        return

    product_key = normalize_product_name(f"{model} {item}")
    cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        await update.message.reply_text(f"'{model} {item}' topilmadi.")
        return

    if row[0] != 0:
        conn.close()
        await update.message.reply_text(
            f"⚠️ '{model} {item}' da hali {row[0]} ta bor, o'chirib bo'lmaydi.\n"
            "Avval boshqa modelga ko'chiring yoki 0 ga tushiring."
        )
        return

    cur.execute("DELETE FROM products WHERE name = ?", (product_key,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 '{model} {item}' ro'yxatdan o'chirildi.")


async def modelochirish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Modelni RO'YXATDAN butunlay o'chiradi (faqat hammasi 0 bo'lsa ishlaydi).\n\n"
            "Foydalanish: /modelochirish <model>\n"
            "Misol: /modelochirish kafini"
        )
        return

    model = args[0].lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item, quantity FROM products WHERE model = ?", (model,))
    rows = cur.fetchall()

    if not rows:
        conn.close()
        await update.message.reply_text(f"'{model}' nomli model topilmadi.")
        return

    nonzero = [(item, qty) for item, qty in rows if qty != 0]
    if nonzero:
        conn.close()
        lines = [f"⚠️ '{model}' modelida hali miqdor bor, o'chirib bo'lmaydi:"]
        lines.extend(f"• {item}: {qty} ta" for item, qty in nonzero)
        lines.append(
            "\nAvval bu miqdorlarni boshqa modelga ko'chiring (/chiqim va /kirim orqali), "
            "keyin qayta urinib ko'ring."
        )
        await update.message.reply_text("\n".join(lines))
        return

    cur.execute("DELETE FROM products WHERE model = ?", (model,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🗑 '{model}' modeli ro'yxatdan butunlay o'chirildi ({len(rows)} ta detal, barchasi 0 edi)."
    )


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
    '1 ta shkaf 1 ta parta') aniqlashga harakat qiladi. Shuningdek, '+krovat'/'-krovat'
    yoki 'krovat qo'shiladi' / 'krovat ayiriladi' kabi qo'shimcha/ayirma ko'rsatmalarini ham taniydi.
    Qaytaradi: (entries, komplekt_aniq) - entries ro'yxati [(item_or_None, amount, mod_type), ...]
    mod_type: None (oddiy), '+' (qo'shimcha), '-' (ayirilgan, mijozga berilmaydi)."""
    lowered = text.lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT item FROM products WHERE model = ?", (model,))
    model_items = [row[0] for row in cur.fetchall()]
    conn.close()

    candidate_items = set(model_items or GENERIC_ITEM_WORDS)

    entries = []
    seen_items = set()
    consumed_spans = []

    def is_consumed(pos):
        return any(s <= pos < e for s, e in consumed_spans)

    # 1) Ochiq "+item" / "-item" belgisi (ixtiyoriy son bilan): "+krovat", "-1 ta krovat"
    for m in re.finditer(r"([+\-])\s*(\d+)?\s*(?:ta\s+)?([a-zʻʼ']+)", lowered):
        if is_consumed(m.start()):
            continue
        sign, qty_str, item_word = m.groups()
        for cand in candidate_items:
            if cand.lower() == item_word and cand not in seen_items:
                amount = int(qty_str) if qty_str else 1
                mod_type = "+" if sign == "+" else "-"
                entries.append((cand, amount, mod_type))
                seen_items.add(cand)
                consumed_spans.append(m.span())
                break

    # 2) So'z bilan ko'rsatilgan qo'shish/ayirish: "krovat qo'shiladi", "krovat ayiriladi/kerak emas"
    for cand in sorted(candidate_items - seen_items, key=lambda x: -len(x)):
        cand_l = cand.lower()
        m_add = re.search(
            rf"(\d+)?\s*ta?\s*\b{re.escape(cand_l)}\b[^.\n]{{0,15}}?(qo['ʻ]?shiladi|qoshiladi)", lowered
        )
        if m_add and not is_consumed(m_add.start()):
            qty = int(m_add.group(1)) if m_add.group(1) else 1
            entries.append((cand, qty, "+"))
            seen_items.add(cand)
            consumed_spans.append(m_add.span())
            continue
        m_sub = re.search(
            rf"(\d+)?\s*ta?\s*\b{re.escape(cand_l)}\b[^.\n]{{0,20}}?"
            rf"(ayiriladi|kerak emas|chiqarilsin|olib tashlanadi)",
            lowered,
        )
        if m_sub and not is_consumed(m_sub.start()):
            qty = int(m_sub.group(1)) if m_sub.group(1) else 1
            entries.append((cand, qty, "-"))
            seen_items.add(cand)
            consumed_spans.append(m_sub.span())

    # 3) "N ta <item>" yoki "<item> N ta" ko'rinishidagi oddiy (mod'siz) uchrashuvlar.
    for m in re.finditer(r"(\d+)\s*ta\s+([a-zʻʼ']+)", lowered):
        if is_consumed(m.start()):
            continue
        item_word = m.group(2)
        for cand in candidate_items:
            if cand.lower() == item_word and cand not in seen_items:
                entries.append((cand, int(m.group(1)), None))
                seen_items.add(cand)
                break
    for m in re.finditer(r"([a-zʻʼ']+)\s+(\d+)\s*ta\b", lowered):
        if is_consumed(m.start()):
            continue
        item_word = m.group(1)
        for cand in candidate_items:
            if cand.lower() == item_word and cand not in seen_items:
                entries.append((cand, int(m.group(2)), None))
                seen_items.add(cand)
                break

    if entries:
        has_mod = any(mt in ("+", "-") for _, _, mt in entries)
        has_komplekt_entry = any(it is None for it, _, _ in entries)
        if has_mod and "komplekt" in lowered and not has_komplekt_entry:
            entries.insert(0, (None, 1, None))
        return entries, False

    if "komplekt" in lowered:
        return [(None, 1, None)], True

    # Miqdorsiz, lekin nomi tilga olingan detallarni ham tekshiramiz (masalan faqat "shkaf").
    for item in sorted(candidate_items, key=lambda x: -len(x)):
        if re.search(rf"\b{re.escape(item.lower())}\b", lowered):
            entries.append((item, 1, None))
    if entries:
        return entries, False

    # Hech narsa aniqlanmadi - komplekt deb taxmin qilamiz, lekin noaniq deb belgilaymiz.
    return [(None, 1, None)], False


def generate_buyurtma_command(model, entries, deadline_display, customer):
    parts = [model]
    if len(entries) == 1 and entries[0][0] is None and entries[0][2] is None:
        parts.append("komplekt")
    else:
        for item, amount, mod_type in entries:
            if item is None:
                parts.append("komplekt")
                parts.append(str(amount))
            elif mod_type in ("+", "-"):
                parts.append(f"{mod_type}{item}")
                parts.append(str(amount))
            else:
                parts.append(item)
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

    lowered_text = text.lower()
    dastavka_detected = any(
        kw in lowered_text for kw in ("viloyat", "dastavka", "pochta", "jo'natiladi", "jonatiladi")
    )

    lines = ["🔔 Guruhda yangi xabar - buyurtma bo'lishi mumkin:", ""]
    lines.append(f"Taxminiy model: {model}")
    if len(entries) == 1 and entries[0][0] is None:
        lines.append(f"Taxminiy tur: komplekt {'✅' if komplekt_aniq else '(❗ aniq topilmadi, tekshiring)'}")
    else:
        for item, amount, mod_type in entries:
            mark = "➕ " if mod_type == "+" else ("➖ " if mod_type == "-" else "")
            label = "komplekt" if item is None else item
            lines.append(f"Taxminiy detal: {mark}{label} ({amount} ta)")

    if dastavka_detected:
        lines.append(
            "🚚 Ehtimol DASTAVKA (viloyatga/jo'natish) - agar shunday bo'lsa, tasdiqlagach "
            "/dastavka buyrug'i bilan belgilashni unutmang."
        )

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
            (model, entries_json, deadline, deadline_display, customer, raw_text, source_chat_id, source_message_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
        """,
        (model, json.dumps(entries), deadline.isoformat(), deadline_display, sender_name, text, chat_id, message_id, now),
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
        "SELECT model, entries_json, deadline, deadline_display, customer, status, source_chat_id, source_message_id "
        "FROM pending_group_orders WHERE id = ?",
        (pending_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        await query.edit_message_text("Bu taklif topilmadi (eskirgan bo'lishi mumkin).")
        return

    model, entries_json, deadline, deadline_display, customer, status, source_chat_id, source_message_id = row
    entries = [tuple(e) for e in json.loads(entries_json)]

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
    for item, amount, mod_type in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, mod_type, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (model, item, amount, mod_type, deadline, deadline_display, customer, now),
        )
        created_ids.append(cur.lastrowid)

    guruh_id = created_ids[0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created_ids)})",
        [guruh_id] + created_ids,
    )
    cur.execute("UPDATE pending_group_orders SET status = 'tasdiqlandi' WHERE id = ?", (pending_id,))

    entries_4 = [(model, item, amount, mod_type) for item, amount, mod_type in entries]
    shortage_text = shortage_warning_for_new_order(cur, entries_4)

    conn.commit()
    conn.close()

    def describe(item, amount, mod_type):
        base = f"{model} komplekt" if item is None else f"{model} {item}"
        mark = "➕ " if mod_type == "+" else ("➖ " if mod_type == "-" else "")
        return f"{mark}{base} ({amount} ta)"

    what_all = ", ".join(describe(item, amount, mod_type) for item, amount, mod_type in entries)
    final_text = (
        query.message.text
        + f"\n\n✅ Tasdiqlandi! Buyurtma №{guruh_id} yaratildi: {what_all}, muddat {deadline_display}."
    )
    if shortage_text:
        final_text += shortage_text
    await query.edit_message_text(final_text)

    if source_chat_id and source_message_id:
        try:
            await context.bot.set_message_reaction(
                chat_id=source_chat_id,
                message_id=source_message_id,
                reaction=[ReactionTypeEmoji("👍")],
            )
        except Exception:
            pass  # Reaksiya qo'yib bo'lmasa ham (masalan xabar juda eski), asosiy oqim davom etadi.


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
        "Komplektga qo'shimcha yoki ayirma uchun '+' yoki '-' belgisini detal oldiga yozing:\n"
        "/buyurtma <model> komplekt +krovat 1 <kun> <oy> [mijoz]   (qo'shimcha krovat)\n"
        "/buyurtma <model> komplekt -krovat 1 <kun> <oy> [mijoz]   (krovatsiz, chegirma bilan)\n\n"
        "Boshqa modeldan bitta detal qo'shish uchun 'model:detal miqdor' yozing:\n"
        "/buyurtma <asosiy model> komplekt <boshqa_model>:<detal> <miqdor> <kun> <oy> [mijoz]\n\n"
        "Misol:\n"
        "/buyurtma vena komplekt 5 avgust\n"
        "/buyurtma laura shkaf 2 5 avgust Mavaviy dokon\n"
        "/buyurtma maya shkaf 1 tumba 1 krovat 1 kamod 1 14 avgust\n"
        "/buyurtma neo komplekt +krovat 1 25 avgust Mebel For Home\n"
        "/buyurtma aven komplekt anta:shkaf 1 25 avgust Mebel For Home"
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

    # Buyurtma turlari: komplekt, bitta detal, bir nechta detal-miqdor jufti,
    # ixtiyoriy '+detal'/'​-detal' (komplektga qo'shimcha/ayirma), va boshqa modeldan
    # bitta detal qo'shish uchun 'model:detal' yozuvi (masalan 'anta:shkaf').
    entries = []  # (model, item_or_None, amount, mod_type)
    idx = 0

    if rest[idx] == "komplekt":
        idx += 1
        amount = 1
        if idx < len(rest) and rest[idx].isdigit():
            amount = int(rest[idx])
            idx += 1
        entries.append((model, None, amount, None))

    malformed = False
    while idx < len(rest):
        tok = rest[idx]
        if tok[0] in "+-" and len(tok) > 1:
            mod_type = tok[0]
            item_word = tok[1:]
            idx += 1
            amount = 1
            if idx < len(rest) and rest[idx].isdigit():
                amount = int(rest[idx])
                idx += 1
            entries.append((model, item_word, amount, mod_type))
        elif ":" in tok and not tok.startswith(":") and not tok.endswith(":"):
            other_model, item_word = tok.split(":", 1)
            idx += 1
            amount = 1
            if idx < len(rest) and rest[idx].isdigit():
                amount = int(rest[idx])
                idx += 1
            entries.append((other_model.lower(), item_word.lower(), amount, None))
        elif idx + 1 < len(rest) and rest[idx + 1].isdigit():
            entries.append((model, tok, int(rest[idx + 1]), None))
            idx += 2
        else:
            malformed = True
            break

    if malformed or not entries:
        conn.close()
        await update.message.reply_text(
            "Detal va miqdorni juft-juft yozing (masalan: shkaf 1 tumba 2), "
            "'komplekt' deb yozing, '+detal'/'​-detal' bilan qo'shimcha/ayirma qiling, "
            "yoki boshqa modeldan detal qo'shish uchun 'model:detal miqdor' yozing "
            "(masalan: anta:shkaf 1).\n\n" + usage
        )
        return

    deadline_display = f"{day} {UZ_MONTH_BY_NUM[month]}"
    created = []
    now = datetime.now().isoformat(timespec="seconds")
    for entry_model, item, amount, mod_type in entries:
        cur.execute(
            """
            INSERT INTO orders (model, item, amount, mod_type, deadline, deadline_display, customer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
            """,
            (entry_model, item, amount, mod_type, deadline.isoformat(), deadline_display, customer, now),
        )
        created.append((cur.lastrowid, entry_model, item, amount, mod_type))

    guruh_id = created[0][0]
    cur.execute(
        f"UPDATE orders SET guruh_id = ? WHERE id IN ({','.join('?' for _ in created)})",
        [guruh_id] + [oid for oid, _, _, _, _ in created],
    )

    shortage_text = shortage_warning_for_new_order(cur, entries)

    conn.commit()
    conn.close()

    def describe(entry_model, item, amount, mod_type):
        base = f"{entry_model} komplekt" if item is None else f"{entry_model} {item}"
        prefix = "➕ " if mod_type == "+" else ("➖ " if mod_type == "-" else "")
        return f"{prefix}{base} ({amount} ta)" if item is not None else base

    what_all = ", ".join(
        describe(entry_model, item, amount, mod_type) for _, entry_model, item, amount, mod_type in created
    )
    lines = [f"📝 Yangi buyurtma qabul qilindi (№{guruh_id}):", what_all]
    lines.append(f"Muddat: {deadline_display}")
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")
    if shortage_text:
        lines.append(shortage_text)
    await update.message.reply_text("\n".join(lines))


async def topshirilganibekor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Tasodifan 'topshirildi' deb belgilangan buyurtmani qaytadan 'kutilmoqda' holatiga "
            "o'tkazadi (zaxirani, ishchi pulini, mijoz hisobini qaytaradi, lekin buyurtmaning "
            "o'zini O'CHIRMAYDI).\n\n"
            "Foydalanish: /topshirilganibekor <buyurtma raqami>\n"
            "Misol: /topshirilganibekor 58"
        )
        return

    guruh_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, model, item, amount, mod_type, status FROM orders WHERE guruh_id = ?",
        (guruh_id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await update.message.reply_text(f"№{guruh_id} buyurtma topilmadi.")
        return

    status = rows[0][5]
    if status != "bajarildi":
        conn.close()
        await update.message.reply_text(f"№{guruh_id} hozir 'bajarildi' holatida emas ({status}), tegilmadi.")
        return

    order_ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in order_ids)

    excluded_by_model = {}
    for _, model, item, amount, mod_type, _ in rows:
        if mod_type == "-" and item is not None:
            excluded_by_model.setdefault(model, set()).add(item)

    stock_restored_lines = []
    for _, model, item, amount, mod_type, _ in rows:
        if mod_type == "-":
            continue

        if item is not None:
            targets = [(model, item, amount)]
        else:
            cur.execute("SELECT item, soni FROM komplekt_tarkibi WHERE model = ?", (model,))
            per_item_qty = dict(cur.fetchall())
            cur.execute("SELECT item FROM products WHERE model = ?", (model,))
            all_items = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT item FROM qoshimcha_detallar WHERE model IN ('', ?)", (model,))
            addon_only = {r[0] for r in cur.fetchall()}
            excluded = excluded_by_model.get(model, set())
            targets = [
                (model, it, amount * per_item_qty.get(it, 1))
                for it in all_items
                if it not in excluded and it not in addon_only
            ]

        for m, it, qty in targets:
            product_key = normalize_product_name(f"{m} {it}")
            cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
            prow = cur.fetchone()
            if prow is None:
                continue
            new_qty = prow[0] + qty
            cur.execute("UPDATE products SET quantity = ? WHERE name = ?", (new_qty, product_key))
            stock_restored_lines.append(f"• {it}: +{qty}")

    cur.execute(
        f"SELECT worker, total, paid FROM work_log WHERE order_id IN ({placeholders}) AND turi = 'yigish'",
        order_ids,
    )
    work_rows = cur.fetchall()
    cur.execute(
        f"DELETE FROM work_log WHERE order_id IN ({placeholders}) AND turi = 'yigish'", order_ids
    )
    cur.execute("DELETE FROM mijoz_tolovlar WHERE guruh_id = ?", (guruh_id,))
    cur.execute(
        f"UPDATE orders SET status = 'kutilmoqda', bajarildi_at = NULL WHERE id IN ({placeholders})",
        order_ids,
    )

    conn.commit()
    conn.close()

    lines = [f"↩️ №{guruh_id} qaytadan 'kutilmoqda' holatiga o'tkazildi."]
    if stock_restored_lines:
        lines.append("\nZaxiradan ayirildi (qaytarildi):")
        lines.extend(stock_restored_lines)
    if work_rows:
        lines.append("\n⚠️ Ishchi puli yozuvlari bekor qilindi:")
        for worker, total, paid in work_rows:
            paid_note = " (diqqat: bu to'langan deb belgilangan edi!)" if paid else ""
            lines.append(f"• {worker}: {format_money(total, 'som')}{paid_note}")
    lines.append("\nMijoz hisobidagi tegishli yozuv ham olib tashlandi.")
    await update.message.reply_text("\n".join(lines))


async def buyurtmaochirish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Buyurtmani BUTUNLAY o'chiradi (zaxirani, ishchi pulini, mijoz hisobini ham qaytaradi).\n\n"
            "Foydalanish: /buyurtmaochirish <buyurtma raqami>\n"
            "Misol: /buyurtmaochirish 42"
        )
        return

    guruh_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, model, item, amount, mod_type, status FROM orders WHERE guruh_id = ?",
        (guruh_id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        await update.message.reply_text(f"№{guruh_id} buyurtma topilmadi.")
        return

    status = rows[0][5]
    order_ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in order_ids)

    stock_restored_lines = []
    if status == "bajarildi":
        # Qaysi detallar '-' bilan ayirilgan edi (ular hech qachon zaxiradan chiqmagan edi)
        excluded_by_model = {}
        for _, model, item, amount, mod_type, _ in rows:
            if mod_type == "-" and item is not None:
                excluded_by_model.setdefault(model, set()).add(item)

        for _, model, item, amount, mod_type, _ in rows:
            if mod_type == "-":
                continue  # bu qator hech narsa chiqarmagan edi, qaytarish ham shart emas

            if item is not None:
                targets = [(model, item, amount)]
            else:
                cur.execute("SELECT item, soni FROM komplekt_tarkibi WHERE model = ?", (model,))
                per_item_qty = dict(cur.fetchall())
                cur.execute("SELECT item FROM products WHERE model = ?", (model,))
                all_items = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT item FROM qoshimcha_detallar WHERE model IN ('', ?)", (model,))
                addon_only = {r[0] for r in cur.fetchall()}
                excluded = excluded_by_model.get(model, set())
                targets = [
                    (model, it, amount * per_item_qty.get(it, 1))
                    for it in all_items
                    if it not in excluded and it not in addon_only
                ]

            for m, it, qty in targets:
                product_key = normalize_product_name(f"{m} {it}")
                cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
                prow = cur.fetchone()
                if prow is None:
                    continue
                new_qty = prow[0] + qty
                cur.execute("UPDATE products SET quantity = ? WHERE name = ?", (new_qty, product_key))
                stock_restored_lines.append(f"• {it}: +{qty}")

    cur.execute(
        f"SELECT worker, total, paid FROM work_log WHERE order_id IN ({placeholders}) AND turi = 'yigish'",
        order_ids,
    )
    work_rows = cur.fetchall()
    cur.execute(
        f"DELETE FROM work_log WHERE order_id IN ({placeholders}) AND turi = 'yigish'", order_ids
    )

    cur.execute("DELETE FROM mijoz_tolovlar WHERE guruh_id = ?", (guruh_id,))
    cur.execute(f"DELETE FROM orders WHERE id IN ({placeholders})", order_ids)

    conn.commit()
    conn.close()

    lines = [f"🗑 №{guruh_id} butunlay o'chirildi."]
    if stock_restored_lines:
        lines.append("\nZaxiraga qaytarildi:")
        lines.extend(stock_restored_lines)
    if work_rows:
        lines.append("\n⚠️ Ishchi puli yozuvlari ham o'chirildi:")
        for worker, total, paid in work_rows:
            paid_note = " (diqqat: bu to'langan deb belgilangan edi!)" if paid else ""
            lines.append(f"• {worker}: {format_money(total, 'som')}{paid_note}")
    lines.append("\nMijoz hisobidagi tegishli yozuv ham tozalandi (agar mavjud bo'lsa).")
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
    if days_left > 1:
        days_text = f"{days_left} kun qoldi"
        urgency = "🟢"
    elif days_left == 1:
        days_text = "ertaga"
        urgency = "🟡"
    elif days_left == 0:
        days_text = "bugun"
        urgency = "🟡"
    else:
        days_text = f"muddati {abs(days_left)} kun o'tgan"
        urgency = "🔴"

    lines = [f"{urgency} Buyurtma №{group['guruh_id']} — {group['deadline_display']} ({days_text})"]
    if group["customer"]:
        lines.append(f"Mijoz: {group['customer']}")
    lines.append("")
    for _, model, item, amount, mod_type in group["items"]:
        what = f"{model} komplekt" if item is None else f"{model} {item}"
        mark = "➕ " if mod_type == "+" else ("➖ " if mod_type == "-" else "")
        lines.append(f"• {mark}{what}: {amount} ta")
    return "\n".join(lines)


def fetch_pending_order_groups():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, guruh_id, model, item, amount, deadline, deadline_display, customer, mod_type
        FROM orders WHERE status = 'kutilmoqda' ORDER BY deadline ASC, guruh_id ASC, id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()

    groups = {}
    order_of_groups = []
    for oid, guruh_id, model, item, amount, deadline_iso, deadline_display, customer, mod_type in rows:
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
        groups[gid]["items"].append((oid, model, item, amount, mod_type))
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
        lines.append(f"💰 Kutilayotgan summa (sotish narxiga ko'ra): {format_money(expected_value, 'usd')}")
    lines.append("\nMijozdan qancha pul olindi? ($ da, faqat son yozing — hali olinmagan bo'lsa 0)")
    return "\n".join(lines)


async def finalize_payment(user, guruh_id: int, worker: str, customer, expected_value: int, received: int) -> str:
    result_text = await bajarildi_group_core(guruh_id, user, worker)
    record_payment(guruh_id, customer, expected_value, received)

    lines = [result_text, f"\n💵 Mijozdan olindi: {format_money(received, 'usd')}"]
    if customer:
        total_expected, total_received = get_customer_totals(customer)
        qarz = total_expected - total_received
        if qarz > 0:
            lines.append(f"🏪 {customer} — umumiy qarzi: {format_money(qarz, 'usd')}")
        elif qarz < 0:
            lines.append(f"🏪 {customer} — sizga {format_money(abs(qarz), 'usd')} ortiqcha to'lagan")
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


def explode_entries_to_items(cur, entries):
    """entries - [(model, item_or_None, amount, mod_type), ...].
    Har bir qatorni haqiqatda qaysi (model, detal) larga va qancha miqdorga
    ta'sir qilishini hisoblaydi (komplektni yoyib, ayirilganlarni chetlab).
    Qaytaradi: {(model, item): miqdor}."""
    need = {}

    excluded_by_model = {}
    for entry_model, item, amount, mod_type in entries:
        if mod_type == "-" and item is not None:
            excluded_by_model.setdefault(entry_model, set()).add(item)

    for entry_model, item, amount, mod_type in entries:
        if mod_type == "-":
            continue
        if item is not None:
            need[(entry_model, item)] = need.get((entry_model, item), 0) + amount
        else:
            cur.execute("SELECT item, soni FROM komplekt_tarkibi WHERE model = ?", (entry_model,))
            per_item_qty = dict(cur.fetchall())
            cur.execute("SELECT item FROM products WHERE model = ?", (entry_model,))
            all_items = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT item FROM qoshimcha_detallar WHERE model IN ('', ?)", (entry_model,))
            addon_only = {r[0] for r in cur.fetchall()}
            excluded = excluded_by_model.get(entry_model, set())
            for it in all_items:
                if it in excluded or it in addon_only:
                    continue
                qty = amount * per_item_qty.get(it, 1)
                need[(entry_model, it)] = need.get((entry_model, it), 0) + qty
    return need


def compute_all_pending_demand(cur):
    """Barcha 'kutilmoqda' buyurtmalar bo'yicha har bir (model, detal) uchun
    zarur bo'lgan JAMI miqdorni hisoblaydi."""
    cur.execute("SELECT id, guruh_id, model, item, amount, mod_type FROM orders WHERE status = 'kutilmoqda'")
    rows = cur.fetchall()

    by_guruh = {}
    for oid, guruh_id, model, item, amount, mod_type in rows:
        by_guruh.setdefault(guruh_id, []).append((model, item, amount, mod_type))

    total_demand = {}
    for guruh_id, entries in by_guruh.items():
        need = explode_entries_to_items(cur, entries)
        for key, qty in need.items():
            total_demand[key] = total_demand.get(key, 0) + qty
    return total_demand


def shortage_warning_for_new_order(cur, entries):
    """Yangi yaratilgan buyurtma tarkibidagi detallar bo'yicha, agar BARCHA
    kutilayotgan buyurtmalar hisobga olinganda zaxira yetarli bo'lmasa,
    ogohlantirish matnini qaytaradi (aks holda None)."""
    my_items = set(explode_entries_to_items(cur, entries).keys())
    if not my_items:
        return None

    total_demand = compute_all_pending_demand(cur)
    lines = []
    for model, item in sorted(my_items):
        needed = total_demand.get((model, item), 0)
        product_key = normalize_product_name(f"{model} {item}")
        cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
        row = cur.fetchone()
        available = row[0] if row else 0
        if available < needed:
            lines.append(
                f"• {model} {item}: kerak {needed} ta, hozir bor {available} ta "
                f"(yetishmaydi: {needed - available} ta)"
            )

    if not lines:
        return None

    return (
        "\n\n⚠️ ZAXIRA OGOHLANTIRISHI — bu va boshqa kutilayotgan buyurtmalarni "
        "hisobga olganda quyidagilar yetarli emas (kesim/usluga buyurtma qiling):\n"
        + "\n".join(lines)
    )


def compute_order_sale_value(guruh_id: int) -> int:
    """Guruhdagi barcha qatorlar uchun 'sotish' narxlari bo'yicha kutilayotgan
    umumiy summani hisoblaydi (mijozga qancha sotilishi kerak).
    mod_type == '+' (qo'shilgan) - 'sotish' narxi qo'shiladi.
    mod_type == '-' (ayirilgan) - 'sotishayirish' narxi ayiriladi.
    mod_type == None va item bor - oddiy 'sotish' narxi (mustaqil sotilgan detal).
    dastavka == 1 bo'lsa - har bir qatordan (mod_type != '-') 'ornatish' xizmati
    narxi qo'shimcha ayiriladi (mijoz uyiga o'rnatib berish yo'q, faqat jo'natiladi)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT model, item, amount, mod_type, dastavka FROM orders WHERE guruh_id = ?", (guruh_id,))
    rows = cur.fetchall()
    total = 0
    for model, item, amount, mod_type, dastavka in rows:
        rate_key = item if item is not None else "komplekt"
        if mod_type == "-":
            rate = get_rate(cur, "sotishayirish", model, rate_key)
            total -= rate * amount
        else:
            rate = get_rate(cur, "sotish", model, rate_key)
            total += rate * amount
            if dastavka:
                ornatish_rate = get_rate(cur, "ornatish", model, rate_key)
                total -= ornatish_rate * amount
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


def fulfill_single_order(cur, order_id, model, item, amount, mod_type, worker, user_name, user_id, now, excluded_items=None):
    """Bitta buyurtma qatorini zaxiradan chiqaradi va (agar worker berilgan bo'lsa)
    to'lovni hisoblab work_log ga yozadi. Natijani (result_lines, payment_total, payment_note) qaytaradi.
    mod_type == '-' bo'lsa (komplektdan ayirilgan detal) - zaxiradan hech narsa chiqarilmaydi,
    ishchiga ham pul hisoblanmaydi, chunki bu detal mijozga berilmayapti.
    excluded_items - komplekt (item=None) yoyilganda o'tkazib yuborilishi kerak bo'lgan detallar
    to'plami (o'sha guruhda '-' bilan ayirilgan detallar, ular komplekt tarkibidan ham chiqarilmasligi kerak).
    Tranzaksiyani boshqarish (commit/close) chaqiruvchiga qoladi."""
    excluded_items = excluded_items or set()

    if mod_type == "-":
        what = f"{model} {item}" if item else model
        return [f"• {what}: ayirildi (mijozga berilmadi, zaxiraga tegilmadi)"], 0, None

    if item is not None:
        targets = [(model, item)]
    else:
        cur.execute("SELECT item FROM products WHERE model = ?", (model,))
        all_items = [row_item for (row_item,) in cur.fetchall()]
        cur.execute("SELECT item FROM qoshimcha_detallar WHERE model IN ('', ?)", (model,))
        addon_only = {row[0] for row in cur.fetchall()}
        targets = [
            (model, row_item) for row_item in all_items
            if row_item not in excluded_items and row_item not in addon_only
        ]

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
        mod_note = " (➕ qo'shimcha)" if mod_type == "+" else ""
        result_lines.append(
            f"• {target_item}: -{deduct}{warn}" if item is None else f"• {what}: -{deduct}{mod_note}{warn}"
        )

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
        "SELECT id, model, item, amount, mod_type, dastavka FROM orders WHERE guruh_id = ? AND status = 'kutilmoqda'",
        (guruh_id,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return f"№{guruh_id} buyurtma topilmadi yoki allaqachon bajarilgan."

    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None
    now = datetime.now().isoformat(timespec="seconds")
    is_dastavka = any(d for _, _, _, _, _, d in rows)

    # Ayirilgan ('-') detallarni model bo'yicha yig'amiz - komplekt (item=None) yoyilganda
    # bu detallar zaxiradan chiqarilmasligi kerak (mijoz ularni olmayapti).
    excluded_by_model = {}
    for _, model, item, _, mod_type, _ in rows:
        if mod_type == "-" and item is not None:
            excluded_by_model.setdefault(model, set()).add(item)

    all_result_lines = []
    total_payment = 0
    missing_rates = []
    for order_id, model, item, amount, mod_type, dastavka in rows:
        excluded_items = excluded_by_model.get(model, set()) if item is None else None
        effective_worker = None if dastavka else worker
        result_lines, payment_total, payment_note = fulfill_single_order(
            cur, order_id, model, item, amount, mod_type, effective_worker, user_name, user_id, now, excluded_items
        )
        all_result_lines.extend(result_lines)
        total_payment += payment_total
        if payment_note:
            missing_rates.append(payment_note)

    cur.execute(
        "UPDATE orders SET status = 'bajarildi', bajarildi_at = ? WHERE guruh_id = ?",
        (now, guruh_id),
    )
    conn.commit()
    conn.close()

    lines = [f"✅ №{guruh_id} buyurtma bajarildi deb belgilandi.", "Zaxiradan chiqarildi:"]
    lines.extend(all_result_lines)
    if is_dastavka:
        lines.append(
            "\n🚚 Dastavka (o'rnatishsiz jo'natish) - ishchiga yig'ish puli yozilmadi, "
            "sotish narxidan o'rnatish xizmati ayirildi."
        )
    elif worker:
        if total_payment > 0:
            lines.append(f"\n👷 {worker} — yig'ish: {total_payment:,} so'm hisoblandi.".replace(",", " "))
        if missing_rates:
            lines.append("\n⚠️ Narx belgilanmagan: " + ", ".join(missing_rates))
    return "\n".join(lines)


def build_mijoz_hisob_text(customer_query: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT guruh_id, expected_value, received_amount, created_at FROM mijoz_tolovlar "
        "WHERE LOWER(customer) = LOWER(?) ORDER BY created_at",
        (customer_query,),
    )
    rows = cur.fetchall()

    if not rows:
        conn.close()
        return (
            f"'{customer_query}' bo'yicha hech qanday yozuv topilmadi.\n"
            "Eslatma: nomi aniq mos kelishi kerak (masalan buyurtmadagi 'Kimdan' nomi bilan bir xil)."
        )

    lines = [f"🏪 {customer_query} — hisob:"]
    total_expected = 0
    total_received = 0
    for guruh_id, expected, received, payment_created_at in rows:
        total_expected += expected
        total_received += received

        cur.execute(
            "SELECT id, model, item, amount, mod_type, deadline_display, bajarildi_at "
            "FROM orders WHERE guruh_id = ?",
            (guruh_id,),
        )
        order_rows = cur.fetchall()

        if order_rows:
            deadline_display = order_rows[0][5]
            bajarildi_at = order_rows[0][6]
            order_ids = [r[0] for r in order_rows]

            what_parts = []
            for _, m, item, amount, mod_type, _, _ in order_rows:
                base = f"{m} komplekt" if item is None else f"{m} {item}"
                mark = "➕" if mod_type == "+" else ("➖" if mod_type == "-" else "")
                what_parts.append(f"{mark}{base}" if mark else base)
            what = ", ".join(what_parts)

            placeholders = ",".join("?" for _ in order_ids)
            cur.execute(
                f"SELECT DISTINCT worker FROM work_log WHERE order_id IN ({placeholders}) AND turi = 'yigish'",
                order_ids,
            )
            workers = [w[0] for w in cur.fetchall()]
            worker_str = ", ".join(workers) if workers else "noma'lum"
        else:
            what = "(buyurtma topilmadi, o'chirilgan bo'lishi mumkin)"
            deadline_display = "-"
            bajarildi_at = None
            worker_str = "noma'lum"

        bajarildi_date = bajarildi_at.split("T")[0] if bajarildi_at else payment_created_at.split("T")[0]

        lines.append(f"\n📦 №{guruh_id} — {what}")
        lines.append(f"   Muddat: {deadline_display}  |  O'rnatilgan: {bajarildi_date}  |  Ishchi: {worker_str}")
        lines.append(
            f"   Buyurtma qiymati: {format_money(expected, 'usd')}  →  To'landi: {format_money(received, 'usd')}"
        )

    conn.close()

    qarz = total_expected - total_received
    lines.append(f"\n\nJami buyurtma qiymati: {format_money(total_expected, 'usd')}")
    lines.append(f"Jami to'landi: {format_money(total_received, 'usd')}")
    if qarz > 0:
        lines.append(f"\n❗ Qarzi: {format_money(qarz, 'usd')}")
    elif qarz < 0:
        lines.append(f"\n✅ Ortiqcha to'lagan: {format_money(abs(qarz), 'usd')}")
    else:
        lines.append("\n✅ Hisob teng (qarzi yo'q)")
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
    await update.message.reply_text(build_mijoz_hisob_text(customer_query))


async def mijozlar_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT customer FROM mijoz_tolovlar ORDER BY customer")
    customers = [row[0] for row in cur.fetchall()]

    if not customers:
        conn.close()
        await update.message.reply_text(
            "Hozircha hech qanday do'kon bilan hisob-kitob yozuvi yo'q.\n"
            "Buyurtma 'topshirildi' deb belgilanib, to'lov kiritilgach shu yerda paydo bo'ladi."
        )
        return

    buttons = []
    for customer in customers:
        cur.execute(
            "SELECT COALESCE(SUM(expected_value),0), COALESCE(SUM(received_amount),0) "
            "FROM mijoz_tolovlar WHERE customer = ?",
            (customer,),
        )
        total_expected, total_received = cur.fetchone()
        qarz = total_expected - total_received
        if qarz > 0:
            tag = f"❗ qarzi {format_money(qarz, 'usd')}"
        elif qarz < 0:
            tag = f"✅ ortiqcha {format_money(abs(qarz), 'usd')}"
        else:
            tag = "✅ teng"
        buttons.append([InlineKeyboardButton(f"{customer} — {tag}", callback_data=f"mij:{customer}")])
    conn.close()

    await update.message.reply_text("🏪 Do'konni tanlang:", reply_markup=InlineKeyboardMarkup(buttons))


async def mijoz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    customer = query.data.split(":", 1)[1]
    text = build_mijoz_hisob_text(customer)
    await query.edit_message_text(text)


async def kopsotilgan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT model, SUM(amount) as jami
        FROM orders
        WHERE status = 'bajarildi' AND (mod_type IS NULL OR mod_type != '-')
        GROUP BY model
        ORDER BY jami DESC
        """
    )
    model_rows = cur.fetchall()

    cur.execute(
        """
        SELECT model, item, SUM(amount) as jami
        FROM orders
        WHERE status = 'bajarildi' AND (mod_type IS NULL OR mod_type != '-') AND item IS NOT NULL
        GROUP BY model, item
        ORDER BY jami DESC
        LIMIT 10
        """
    )
    item_rows = cur.fetchall()
    conn.close()

    if not model_rows:
        await update.message.reply_text("Hozircha hech qanday buyurtma 'bajarildi' deb belgilanmagan.")
        return

    lines = ["🏆 Eng ko'p sotilgan modellar (umumiy, barcha vaqt):\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (model, jami) in enumerate(model_rows[:15]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {model}: {jami} ta")

    if item_rows:
        lines.append("\n📦 Eng ko'p sotilgan aniq detallar (top 10):")
        for model, item, jami in item_rows:
            lines.append(f"• {model} {item}: {jami} ta")

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
            turi_label = "Upakovka" if turi == "upakovka" else "Yig'ish"
            model_part = f"{model} " if model else ""
            date_part = created_at.split("T")[0] if created_at else "-"
            lines.append(
                f"{icon} {turi_label}: {model_part}{item} — {amount} ta x {format_money(rate, 'som')} "
                f"= {format_money(total, 'som')}  ({date_part})"
            )
        lines.append(f"\n💰 Jami to'lanmagan: {format_money(total_sum, 'som')}")
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
        lines.append(f"• {worker}: {format_money(total, 'som')}")
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


async def job_oylik_hisobot(context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID is None:
        return

    tomorrow = date.today() + timedelta(days=1)
    if tomorrow.day != 1:
        return  # bugun oyning oxirgi kuni emas - hali eslatish vaqti kelmadi

    today = date.today()
    month_start = today.replace(day=1).isoformat()
    month_end = today.isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT model, SUM(amount) as jami
        FROM orders
        WHERE status = 'bajarildi' AND (mod_type IS NULL OR mod_type != '-')
              AND bajarildi_at IS NOT NULL AND date(bajarildi_at) BETWEEN ? AND ?
        GROUP BY model
        ORDER BY jami DESC
        """,
        (month_start, month_end),
    )
    model_rows = cur.fetchall()
    conn.close()

    if not model_rows:
        return  # bu oy hech narsa sotilmagan - bekorga xabar yubormaymiz

    oy_nomlari = {
        1: "Yanvar", 2: "Fevral", 3: "Mart", 4: "Aprel", 5: "May", 6: "Iyun",
        7: "Iyul", 8: "Avgust", 9: "Sentyabr", 10: "Oktyabr", 11: "Noyabr", 12: "Dekabr",
    }
    lines = [f"📅 {oy_nomlari[today.month]} oyi yakuni — eng ko'p sotilgan modellar:\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (model, jami) in enumerate(model_rows[:15]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {model}: {jami} ta")

    await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(lines))


async def job_muddat_eslatma(context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID is None:
        return

    groups = fetch_pending_order_groups()
    if not groups:
        return

    today = date.today()
    urgent = []
    for g in groups:
        deadline_date = date.fromisoformat(g["deadline"])
        days_left = (deadline_date - today).days
        if days_left <= 1:
            urgent.append(g)

    if not urgent:
        return  # dolzarb buyurtma yo'q - bekorga xabar yubormaymiz

    text = "🔔 Bugun/ertaga topshirilishi kerak (yoki muddati o'tgan) buyurtmalar:\n\n" + "\n\n".join(
        format_group_text(g) for g in urgent
    )
    await context.bot.send_message(chat_id=OWNER_ID, text=text)


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


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Har qanday kutilmagan xato yuz berganda, uni to'g'ridan-to'g'ri egaga
    Telegram orqali yuboradi - Railway loglariga kirishning hojati bo'lmaydi."""
    import traceback

    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    tb_short = tb[-3500:]  # Telegram xabar uzunligi cheklangan

    logging.error("Kutilmagan xato:\n%s", tb)

    if OWNER_ID is not None:
        try:
            update_info = ""
            if isinstance(update, Update) and update.effective_message:
                update_info = f"Xabar: {update.effective_message.text}\n\n"
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"🐞 Botda kutilmagan xato yuz berdi:\n\n{update_info}```\n{tb_short}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass  # xato haqida xabar berishning o'zi xato bersa, jim o'tkazamiz


def main():
    token = os.environ.get("BOT_TOKEN")
    if token:
        token = token.strip()  # Railway'ga joylashtirilganda tasodifan qo'shilib qoladigan
                                 # bo'sh qator/probel tokenni buzib qo'yishining oldini oladi.
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
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['mijozlar']}$"), mijozlar_button))
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
    app.add_handler(CommandHandler("buyurtmalartozalash", buyurtmalartozalash))
    app.add_handler(CommandHandler("modelnomi", modelnomi))
    app.add_handler(CommandHandler("modelochirish", modelochirish))
    app.add_handler(CommandHandler("detalochirish", detalochirish))
    app.add_handler(CommandHandler("komplekttarkibi", komplekttarkibi))
    app.add_handler(CommandHandler("xomkirim", xomkirim))
    app.add_handler(CommandHandler("xomqoldiq", xomqoldiq))
    app.add_handler(CommandHandler("xomtarkibi", xomtarkibi))
    app.add_handler(CommandHandler("xommodeltarkibi", xommodeltarkibi))
    app.add_handler(CommandHandler("kirimtuzatish", kirimtuzatish))
    app.add_handler(CommandHandler("hisobtuzatish", hisobtuzatish))
    app.add_handler(CommandHandler("dastavka", dastavka_toggle))
    app.add_handler(CommandHandler("ishchinomitolash", ishchinomitolash))
    app.add_handler(CommandHandler("qoshimchadetal", qoshimchadetal))
    app.add_handler(CommandHandler("qoshimchadetalochirish", qoshimchadetalochirish))
    app.add_handler(CommandHandler("narxochirish", narxochirish))
    app.add_handler(CommandHandler("qoshimchadetallar", qoshimchadetallar))
    app.add_handler(CommandHandler("narx", narx))
    app.add_handler(CommandHandler("modelnarx", modelnarx))
    app.add_handler(CommandHandler("narxlar", narxlar))
    app.add_handler(CommandHandler("narxtozalash", narxtozalash))
    app.add_handler(CommandHandler("ishchilar", ishchilar))
    app.add_handler(CommandHandler("ishchiulash", ishchiulash))
    app.add_handler(CommandHandler("maosh", maosh))
    app.add_handler(CommandHandler("kopsotilgan", kopsotilgan))
    app.add_handler(CommandHandler("mijozhisob", mijozhisob))
    app.add_handler(CommandHandler("tolandi", tolandi))
    app.add_handler(CommandHandler("detalnomi", detalnomi))
    app.add_handler(CommandHandler("royxatga", royxatga))
    app.add_handler(CommandHandler("buyurtma", buyurtma))
    app.add_handler(CommandHandler("buyurtmalar", buyurtmalar))
    app.add_handler(CommandHandler("bajarildi", bajarildi))
    app.add_handler(CommandHandler("bekor", bekor))
    app.add_handler(CommandHandler("guruhlash", guruhlash))
    app.add_handler(CommandHandler("buyurtmaochirish", buyurtmaochirish))
    app.add_handler(CommandHandler("topshirilganibekor", topshirilganibekor))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(mijoz_callback, pattern=r"^mij:"))
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
        # Har kuni ertalab soat 9:00 (Toshkent vaqti) egaga bugun/ertaga/o'tgan
        # muddatli buyurtmalarni alohida eslatib turadi.
        app.job_queue.run_daily(
            job_muddat_eslatma, time=time(hour=9, minute=0, tzinfo=TASHKENT_TZ)
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
        # Har kuni soat 21:00 da tekshiradi - agar bugun oyning oxirgi kuni bo'lsa,
        # shu oyning eng ko'p sotilgan modellari haqida hisobot yuboradi.
        app.job_queue.run_daily(
            job_oylik_hisobot, time=time(hour=21, minute=0, tzinfo=TASHKENT_TZ)
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
    app.add_error_handler(global_error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
