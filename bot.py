"""
Zaxira Bot - Mebel ustaxonasi uchun mahsulot kirim/chiqim va qoldiq kuzatuv boti.

Buyruqlar:
  /kirim <mahsulot> <miqdor>   - mahsulot keldi (zaxiraga qo'shiladi)
  /chiqim <mahsulot> <miqdor>  - mahsulot ketdi/sotildi (zaxiradan ayiriladi)
  /qoldiq                       - barcha mahsulotlar qoldig'ini ko'rsatadi
  /qoldiq <mahsulot>            - bitta mahsulot qoldig'ini ko'rsatadi
  /tarix <mahsulot> [soni]       - mahsulot bo'yicha oxirgi harakatlar tarixi
  /ochir <mahsulot>              - mahsulotni ro'yxatdan o'chiradi (ehtiyot bo'ling)
  /yordam yoki /start            - yordam matni

Har bir yozuv kim tomonidan kiritilgani va vaqti bilan saqlanadi,
shuning uchun butun jamoa (bir nechta xodim) bitta guruhda yoki
shaxsiy chatda foydalanishi mumkin.
"""

import logging
import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "zaxira.db")


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
    conn.commit()
    conn.close()


def normalize_product_name(name: str) -> str:
    return name.strip().lower()


def parse_amount(raw: str) -> int:
    amount = int(raw)
    if amount <= 0:
        raise ValueError("Miqdor musbat son bo'lishi kerak")
    return amount


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Assalomu alaykum! Men zaxira botiman.\n\n"
        "Buyruqlar:\n"
        "/kirim <mahsulot> <miqdor> - mahsulot keldi\n"
        "/chiqim <mahsulot> <miqdor> - mahsulot sotildi/ketdi\n"
        "/qoldiq - barcha mahsulotlar qoldig'i\n"
        "/qoldiq <mahsulot> - bitta mahsulot qoldig'i\n"
        "/tarix <mahsulot> [soni] - oxirgi harakatlar\n"
        "/ochir <mahsulot> - mahsulotni ro'yxatdan o'chirish\n\n"
        "Misol:\n"
        "/kirim shkaf 5\n"
        "/chiqim krovat 2"
    )
    await update.message.reply_text(text)


async def kirim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await change_stock(update, context, "kirim")


async def chiqim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await change_stock(update, context, "chiqim")


async def change_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, change_type: str):
    args = context.args
    if len(args) < 2:
        cmd = "/kirim" if change_type == "kirim" else "/chiqim"
        await update.message.reply_text(
            f"Foydalanish: {cmd} <mahsulot nomi> <miqdor>\nMisol: {cmd} shkaf 5"
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
            "INSERT INTO products (name, quantity) VALUES (?, ?)",
            (product_key, 0),
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


async def tarix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Foydalanish: /tarix <mahsulot> [soni]\nMisol: /tarix shkaf 10")
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
    args = context.args
    if not args:
        await update.message.reply_text("Foydalanish: /ochir <mahsulot>")
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
    app.add_handler(CommandHandler("tarix", tarix))
    app.add_handler(CommandHandler("ochir", ochir))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
