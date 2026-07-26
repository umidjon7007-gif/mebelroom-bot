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
  /yordam yoki /start                - yordam matni

Faqat egasi (OWNER_ID muhit o'zgaruvchisida ko'rsatilgan foydalanuvchi)
/kirim, /chiqim va /ochir buyruqlaridan foydalana oladi. Boshqa hamma
(butun jamoa) faqat /qoldiq, /modellar va /tarix orqali ko'rib turadi,
o'zgartira olmaydi. Agar OWNER_ID sozlanmagan bo'lsa, cheklov ishlamaydi
(hamma hamma narsani qila oladi).
"""

import logging
import os
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Assalomu alaykum! Men zaxira botiman.\n\n"
        "Mahsulot nomini har doim shunday yozing: <model> <detal>\n"
        "Misol: laura tumba, vena shkaf\n\n"
        "Buyruqlar:\n"
        "/kirim <model> <detal> <miqdor> - mahsulot keldi\n"
        "/chiqim <model> <detal> <miqdor> - mahsulot sotildi/ketdi\n"
        "/qoldiq - barcha mahsulotlar qoldig'i\n"
        "/qoldiq <model> <detal> - bitta mahsulot qoldig'i\n"
        "/modellar - modellar bo'yicha tugmali ko'rinish\n"
        "/tarix <model> <detal> [soni] - oxirgi harakatlar\n"
        "/ochir <model> <detal> - mahsulotni ro'yxatdan o'chirish\n\n"
        "Misol:\n"
        "/kirim laura tumba 5\n"
        "/chiqim vena shkaf 2"
    )
    await update.message.reply_text(text)


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
    args = context.args
    if len(args) < 3:
        cmd = "/kirim" if change_type == "kirim" else "/chiqim"
        await update.message.reply_text(
            f"Foydalanish: {cmd} <model> <detal> <miqdor>\nMisol: {cmd} laura tumba 5"
        )
        return

    *name_parts, amount_raw = args
    product_display = " ".join(name_parts).strip()
    if not product_display:
        await update.message.reply_text("Mahsulot nomini kiriting.")
        return

    try:
        amount = parse_amount(amount_raw)
    except ValueError:
        await update.message.reply_text("Miqdor musbat butun son bo'lishi kerak. Misol: 5")
        return

    product_key = normalize_product_name(product_display)
    model, item = split_model_item(product_display)
    if not item:
        await update.message.reply_text(
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
            await update.message.reply_text(
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
            await update.message.reply_text(
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
    await update.message.reply_text(
        f"{emoji} '{product_display}': {amount} ta {verb}.\n"
        f"Yangi qoldiq: {new_qty} ta.\n"
        f"Kiritdi: {user_name}"
    )


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
            await update.message.reply_text(f"{row[0]}: {row[1]} ta qoldi.")
        return

    cur.execute("SELECT name, quantity FROM products ORDER BY name")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Hozircha hech qanday mahsulot ro'yxatga olinmagan.")
        return

    lines = ["📦 Zaxira qoldig'i:\n"]
    for name, quantity in rows:
        lines.append(f"• {name}: {quantity} ta")
    await update.message.reply_text("\n".join(lines))


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
        lines.append(f"• {item}: {quantity} ta")
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
    app.add_handler(CommandHandler("kirim", kirim))
    app.add_handler(CommandHandler("chiqim", chiqim))
    app.add_handler(CommandHandler("qoldiq", qoldiq))
    app.add_handler(CommandHandler("modellar", modellar))
    app.add_handler(CommandHandler("tarix", tarix))
    app.add_handler(CommandHandler("ochir", ochir))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))

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
