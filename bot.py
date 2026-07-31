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
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [MENU_BUTTONS["kirim"], MENU_BUTTONS["chiqim"]],
        [MENU_BUTTONS["qoldiq"], MENU_BUTTONS["modellar"]],
        [MENU_BUTTONS["buyurtmalar"], MENU_BUTTONS["yordam"]],
    ],
    resize_keyboard=True,
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
        "/royxatga - bir nechta modelni birdaniga qo'shish\n"
        "/buyurtma <model> komplekt <kun> <oy> [mijoz] - buyurtma qabul qilish\n"
        "/buyurtma <model> <detal> <miqdor> <kun> <oy> [mijoz] - buyurtma qabul qilish\n"
        "/buyurtmalar - bajarilmagan buyurtmalar ro'yxati\n"
        "/bajarildi <raqam> - buyurtmani bajarilgan deb belgilaydi va zaxiradan chiqaradi\n\n"
        "Avtomatik xabarlar:\n"
        f"- Har kuni ertalab: {LOW_STOCK_THRESHOLD} tadan kam qolgan mahsulotlar haqida ogohlantirish\n"
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


async def kirim_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    context.user_data["awaiting"] = "kirim"
    await update.message.reply_text(
        "📥 Kirim uchun model, detal va miqdorni yozing.\nMisol: laura shkaf 5"
    )


async def chiqim_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return
    context.user_data["awaiting"] = "chiqim"
    await update.message.reply_text(
        "📤 Chiqim uchun model, detal va miqdorni yozing.\nMisol: laura shkaf 2"
    )


async def handle_awaiting_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return  # oddiy xabar, buyruq emas - e'tiborsiz qoldiramiz

    context.user_data["awaiting"] = None
    text = (update.message.text or "").strip()
    args = text.split()
    await change_stock_core(update, context, awaiting, args)


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

    args = context.args
    usage = (
        "Foydalanish:\n"
        "/buyurtma <model> komplekt <kun> <oy> [mijoz]\n"
        "/buyurtma <model> <detal> <miqdor> <kun> <oy> [mijoz]\n\n"
        "Misol:\n"
        "/buyurtma vena komplekt 5 avgust\n"
        "/buyurtma laura shkaf 2 5 avgust Mavaviy dokon"
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
    month = MONTH_NAMES[args[month_idx].lower()]
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

    model = before[0].lower()
    item = None
    amount = 1

    if len(before) >= 2 and before[1].lower() == "komplekt":
        remaining = before[2:]
        if remaining and remaining[0].isdigit():
            amount = int(remaining[0])
    else:
        if len(before) < 3:
            await update.message.reply_text(usage)
            return
        item = before[1].lower()
        if not before[2].isdigit():
            await update.message.reply_text("Miqdor son bo'lishi kerak. Misol: laura shkaf 2 5 avgust")
            return
        amount = int(before[2])

    deadline_display = f"{day} {UZ_MONTH_BY_NUM[month]}"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders (model, item, amount, deadline, deadline_display, customer, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'kutilmoqda', ?)
        """,
        (model, item, amount, deadline.isoformat(), deadline_display, customer, datetime.now().isoformat(timespec="seconds")),
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    what = f"{model} komplekt" if item is None else f"{model} {item}"
    lines = [
        "📝 Yangi buyurtma qabul qilindi:",
        f"№{order_id} — {what}" + (f" ({amount} ta)" if item else ""),
        f"Muddat: {deadline_display}",
    ]
    if customer:
        lines.append(f"Kimdan: {customer}")
    lines.append("Holati: Kutilmoqda")
    await update.message.reply_text("\n".join(lines))


async def buyurtmalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if not rows:
        await update.message.reply_text("Hozircha bajarilmagan buyurtma yo'q.")
        return

    today = date.today()
    lines = ["📋 Bajarilmagan buyurtmalar:\n"]
    for order_id, model, item, amount, deadline_iso, deadline_display, customer in rows:
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
        lines.append(line)

    await update.message.reply_text("\n".join(lines))


async def bajarildi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await deny_access(update)
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Foydalanish: /bajarildi <buyurtma raqami>\nMisol: /bajarildi 12")
        return

    order_id = int(args[0])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT model, item, amount FROM orders WHERE id = ? AND status = 'kutilmoqda'",
        (order_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        await update.message.reply_text(f"№{order_id} buyurtma topilmadi yoki allaqachon bajarilgan.")
        return

    model, item, amount = row

    if item is not None:
        targets = [(model, item)]
    else:
        cur.execute("SELECT item FROM products WHERE model = ?", (model,))
        targets = [(model, row_item) for (row_item,) in cur.fetchall()]

    if not targets:
        conn.close()
        await update.message.reply_text(
            f"'{model}' modeli uchun hech qanday detal ro'yxatda topilmadi, chiqim qilinmadi."
        )
        return

    user = update.effective_user
    user_name = user.full_name if user else "noma'lum"
    user_id = user.id if user else None
    now = datetime.now().isoformat(timespec="seconds")

    result_lines = []
    for target_model, target_item in targets:
        product_key = normalize_product_name(f"{target_model} {target_item}")
        cur.execute("SELECT quantity FROM products WHERE name = ?", (product_key,))
        prow = cur.fetchone()
        current_qty = prow[0] if prow else 0
        new_qty = current_qty - amount
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
            (product_key, amount, user_name, user_id, now),
        )
        warn = " ⚠️ yetarli emas edi!" if shortage else ""
        result_lines.append(f"• {target_item}: -{amount}{warn}")

    cur.execute("UPDATE orders SET status = 'bajarildi' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    what = f"{model} komplekt" if item is None else f"{model} {item}"
    lines = [f"✅ №{order_id} buyurtma bajarildi deb belgilandi.", f"{what} zaxiradan chiqarildi:"]
    lines.extend(result_lines)
    await update.message.reply_text("\n".join(lines))


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
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['qoldiq']}$"), qoldiq))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['modellar']}$"), modellar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['buyurtmalar']}$"), buyurtmalar))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['yordam']}$"), start))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['kirim']}$"), kirim_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTONS['chiqim']}$"), chiqim_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_awaiting_text))
    app.add_handler(CommandHandler("kirim", kirim))
    app.add_handler(CommandHandler("chiqim", chiqim))
    app.add_handler(CommandHandler("qoldiq", qoldiq))
    app.add_handler(CommandHandler("modellar", modellar))
    app.add_handler(CommandHandler("tarix", tarix))
    app.add_handler(CommandHandler("ochir", ochir))
    app.add_handler(CommandHandler("tozalash", tozalash))
    app.add_handler(CommandHandler("royxatga", royxatga))
    app.add_handler(CommandHandler("buyurtma", buyurtma))
    app.add_handler(CommandHandler("buyurtmalar", buyurtmalar))
    app.add_handler(CommandHandler("bajarildi", bajarildi))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))

    if app.job_queue is not None:
        # Har kuni ertalab soat 9:00 (Toshkent vaqti) kam qolgan mahsulotlarni tekshiradi.
        app.job_queue.run_daily(
            job_kam_qoldi, time=time(hour=9, minute=0, tzinfo=TASHKENT_TZ)
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
