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
            item TEXT PRIMARY KEY COLLATE NOCASE,  -- detal nomi, yoki 'komplekt'
            rate INTEGER NOT NULL
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
        CREATE TABLE IF NOT EXISTS work_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker TEXT NOT NULL COLLATE NOCASE,
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
        "/narx <detal> <summa> - detal (yoki 'komplekt') narxini belgilash\n"
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
    if not is_owner(update):
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
    conn.commit()
    conn.close()

    verb = "qo'shildi" if change_type == "kirim" else "ayirildi"
    emoji = "📥" if change_type == "kirim" else "📤"
    await update.effective_message.reply_text(
        f"{emoji} '{product_display}': {amount} ta {verb}.\n"
        f"Yangi qoldiq: {new_qty} ta.\n"
        f"Kiritdi: {user_name}"
    )


async def sb_start(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
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
    if not is_owner(update):
        await query.answer("Faqat egasi qila oladi.", show_alert=True)
        return
    await query.answer()
    data = query.data

    sb = context.user_data.get("sb")

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
    conn.commit()
    conn.close()

    lines = ["📝 Yangi buyurtma qabul qilindi:"]
    for order_id, item, amount in created:
        what = f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)"
        lines.append(f"№{order_id} — {what}")
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
        order_id = context.user_data.get("pending_order_id")
        context.user_data["awaiting"] = None
        context.user_data["pending_order_id"] = None
        if order_id is None:
            return
        result_text = await bajarildi_core(order_id, update.effective_user, text)
        await update.message.reply_text(result_text)
        return

    args = text.split()
    await change_stock_core(update, context, awaiting, args)
    # awaiting rejimi saqlanib qoladi - foydalanuvchi '✅ Tayyor' bosguncha
    # ketma-ket yana mahsulot yozishi mumkin.


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
        "Foydalanish: /narx <detal> <summa>\n"
        "Misol: /narx shkaf 20000\n"
        "Komplekt uchun: /narx komplekt 100000"
    )
    if len(args) < 2 or not args[-1].isdigit():
        await update.message.reply_text(usage)
        return

    rate = int(args[-1])
    item = " ".join(args[:-1]).lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO narxlar (item, rate) VALUES (?, ?) "
        "ON CONFLICT(item) DO UPDATE SET rate = excluded.rate",
        (item, rate),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ '{item}' narxi: {rate:,} so'm deb belgilandi.".replace(",", " "))


async def narxlar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item, rate FROM narxlar ORDER BY item")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "Hozircha hech qanday narx belgilanmagan.\nQo'shish: /narx <detal> <summa>"
        )
        return

    lines = ["💰 Narxlar:\n"]
    for item, rate in rows:
        lines.append(f"• {item}: {rate:,} so'm".replace(",", " "))
    await update.message.reply_text("\n".join(lines))


async def ishchilar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM workers ORDER BY name")
    rows = [row[0] for row in cur.fetchall()]
    conn.close()

    if not rows:
        await update.message.reply_text("Hozircha hech qanday ishchi qo'shilmagan.")
        return

    lines = ["👷 Ishchilar:\n"] + [f"• {name}" for name in rows]
    await update.message.reply_text("\n".join(lines))


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

    conn.commit()
    conn.close()

    lines = ["📝 Yangi buyurtma qabul qilindi:"]
    for order_id, item, amount in created:
        what = f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)"
        lines.append(f"№{order_id} — {what}")
    lines.append(f"Muddat: {deadline_display}")
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")
    await update.message.reply_text("\n".join(lines))


async def bekor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Foydalanish: /bekor <buyurtma raqami>\nMisol: /bekor 14")
        return

    order_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT model, item FROM orders WHERE id = ? AND status = 'kutilmoqda'", (order_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        await update.message.reply_text(
            f"№{order_id} buyurtma topilmadi yoki allaqachon bajarilgan/bekor qilingan."
        )
        return

    cur.execute("UPDATE orders SET status = 'bekor qilindi' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    model, item = row
    what = f"{model} komplekt" if item is None else f"{model} {item}"
    await update.message.reply_text(
        f"🗑 №{order_id} ({what}) bekor qilindi. Zaxiraga hech qanday ta'sir qilmadi."
    )


def format_single_order_line(row):
    order_id, model, item, amount, deadline_iso, deadline_display, customer = row
    today = date.today()
    deadline_date = date.fromisoformat(deadline_iso)
    days_left = (deadline_date - today).days
    if days_left > 0:
        days_text = f"{days_left} kun qoldi"
    elif days_left == 0:
        days_text = "bugun"
    else:
        days_text = f"muddati {abs(days_left)} kun o'tgan"

    what = f"{model} komplekt" if item is None else f"{model} {item} ({amount} ta)"
    line = f"№{order_id} — {what} — {deadline_display} ({days_text})"
    if customer:
        line += f" — {customer}"
    return line


def format_buyurtmalar_text(rows):
    if not rows:
        return "Hozircha bajarilmagan buyurtma yo'q."

    lines = ["📋 Bajarilmagan buyurtmalar:\n"]
    for row in rows:
        lines.append(format_single_order_line(row))
    return "\n".join(lines)


def fetch_pending_orders():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, model, item, amount, deadline, deadline_display, customer
        FROM orders WHERE status = 'kutilmoqda' ORDER BY deadline ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


async def buyurtmalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_pending_orders()
    if not rows:
        await update.message.reply_text("Hozircha bajarilmagan buyurtma yo'q.")
        return

    await update.message.reply_text(f"📋 Bajarilmagan buyurtmalar ({len(rows)} ta):")
    for row in rows:
        order_id = row[0]
        text = format_single_order_line(row)
        button = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"✅ №{order_id} topshirildi", callback_data=f"orddone:{order_id}")]]
        )
        await update.message.reply_text(text, reply_markup=button)


async def orddone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(update):
        await query.answer("Faqat egasi bajara oladi.", show_alert=True)
        return
    await query.answer()

    order_id = int(query.data.split(":", 1)[1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM workers ORDER BY name")
    workers = [row[0] for row in cur.fetchall()]
    conn.close()

    buttons = [
        [InlineKeyboardButton(w, callback_data=f"workerdone:{order_id}:{w}")] for w in workers
    ]
    buttons.append([InlineKeyboardButton("➕ Yangi ishchi", callback_data=f"workerdone:{order_id}:__new__")])
    await query.edit_message_text("👷 Buyurtmani kim topshirdi?", reply_markup=InlineKeyboardMarkup(buttons))


async def workerdone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(update):
        await query.answer("Faqat egasi bajara oladi.", show_alert=True)
        return
    await query.answer()

    _, order_id_str, worker = query.data.split(":", 2)
    order_id = int(order_id_str)

    if worker == "__new__":
        context.user_data["awaiting"] = "worker_name_for_order"
        context.user_data["pending_order_id"] = order_id
        await query.edit_message_text("✍️ Yangi ishchining ismini yozing:")
        return

    result_text = await bajarildi_core(order_id, update.effective_user, worker)
    await query.edit_message_text(result_text, reply_markup=None)


async def bajarildi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit() or len(args) < 2:
        await update.message.reply_text(
            "Foydalanish: /bajarildi <buyurtma raqami> <ishchi ismi>\n"
            "Misol: /bajarildi 12 Hojiakbar"
        )
        return

    order_id = int(args[0])
    worker = " ".join(args[1:])
    user = update.effective_user
    text = await bajarildi_core(order_id, user, worker)
    await update.message.reply_text(text)


async def bajarildi_core(order_id: int, user, worker: str = None) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT model, item, amount FROM orders WHERE id = ? AND status = 'kutilmoqda'",
        (order_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return f"№{order_id} buyurtma topilmadi yoki allaqachon bajarilgan."

    model, item, amount = row

    if item is not None:
        targets = [(model, item)]
    else:
        cur.execute("SELECT item FROM products WHERE model = ?", (model,))
        targets = [(model, row_item) for (row_item,) in cur.fetchall()]

    if not targets:
        conn.close()
        return f"'{model}' modeli uchun hech qanday detal ro'yxatda topilmadi, chiqim qilinmadi."

    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None
    now = datetime.now().isoformat(timespec="seconds")

    # Komplekt buyurtmasi bo'lsa, har bir detal uchun kerakli sonni
    # komplekt_tarkibi jadvalidan olamiz (sozlanmagan bo'lsa - standart 1).
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

        cur.execute(
            "UPDATE products SET quantity = ? WHERE name = ?",
            (new_qty, product_key),
        )
        cur.execute(
            """
            INSERT INTO transactions (product, change_type, amount, user_name, user_id, created_at)
            VALUES (?, 'chiqim', ?, ?, ?, ?)
            """,
            (product_key, deduct, user_name, user_id, now),
        )
        warn = " ⚠️ yetarli emas edi!" if shortage else ""
        result_lines.append(f"• {target_item}: -{deduct}{warn}")

    cur.execute("UPDATE orders SET status = 'bajarildi' WHERE id = ?", (order_id,))

    payment_line = ""
    if worker:
        # To'lov hisobi: komplekt buyurtmasi bo'lsa "komplekt" narxi * buyurtma soni,
        # aks holda o'sha detal narxi * buyurtma soni (zaxiradan chiqarilgan aniq
        # miqdordan mustaqil - komplekt tarkibidagi ko'paytmalarga qarab emas).
        rate_key = "komplekt" if item is None else item
        cur.execute("SELECT rate FROM narxlar WHERE item = ?", (rate_key,))
        rrow = cur.fetchone()
        rate = rrow[0] if rrow else 0
        total = amount * rate

        cur.execute(
            "INSERT OR IGNORE INTO workers (name, created_at) VALUES (?, ?)",
            (worker, now),
        )
        cur.execute(
            """
            INSERT INTO work_log (worker, order_id, model, item, amount, rate, total, paid, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (worker, order_id, model, rate_key, amount, rate, total, now),
        )
        if rate == 0:
            payment_line = f"\n👷 {worker} — narx belgilanmagan ('{rate_key}' uchun /narx bilan belgilang)."
        else:
            payment_line = f"\n👷 {worker} — {total:,} so'm hisoblandi ({amount} x {rate:,}).".replace(",", " ")

    conn.commit()
    conn.close()

    what = f"{model} komplekt" if item is None else f"{model} {item}"
    lines = [f"✅ №{order_id} buyurtma bajarildi deb belgilandi.", f"{what} zaxiradan chiqarildi:"]
    lines.extend(result_lines)
    if payment_line:
        lines.append(payment_line)
    return "\n".join(lines)


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
            SELECT model, item, amount, rate, total, created_at
            FROM work_log WHERE worker = ? AND paid = 0 ORDER BY created_at
            """,
            (worker,),
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(f"👷 {worker} — to'lanmagan ish topilmadi.")
            return

        total_sum = sum(r[4] for r in rows)
        lines = [f"👷 {worker} — to'lanmagan ishlar ({len(rows)} ta):\n"]
        for model, item, amount, rate, total, created_at in rows:
            lines.append(f"• {model} {item} x{amount} = {total:,} so'm".replace(",", " "))
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

    rows = fetch_pending_orders()
    text = format_buyurtmalar_text(rows)
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
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['qoldiq']}$"), qoldiq))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['modellar']}$"), modellar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['buyurtmalar']}$"), buyurtmalar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['yordam']}$"), start))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['kirim']}$"), kirim_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['chiqim']}$"), chiqim_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['yangi_buyurtma']}$"), buyurtma_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{FINISH_BUTTON}$"), tayyor_button))
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
    app.add_handler(CommandHandler("narxlar", narxlar))
    app.add_handler(CommandHandler("ishchilar", ishchilar))
    app.add_handler(CommandHandler("maosh", maosh))
    app.add_handler(CommandHandler("tolandi", tolandi))
    app.add_handler(CommandHandler("detalnomi", detalnomi))
    app.add_handler(CommandHandler("royxatga", royxatga))
    app.add_handler(CommandHandler("buyurtma", buyurtma))
    app.add_handler(CommandHandler("buyurtmalar", buyurtmalar))
    app.add_handler(CommandHandler("bajarildi", bajarildi))
    app.add_handler(CommandHandler("bekor", bekor))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(ob_callback, pattern=r"^ob:"))
    app.add_handler(CallbackQueryHandler(orddone_callback, pattern=r"^orddone:"))
    app.add_handler(CallbackQueryHandler(workerdone_callback, pattern=r"^workerdone:"))
    app.add_handler(CallbackQueryHandler(sb_callback, pattern=r"^sb:"))

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
