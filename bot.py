import os
import re
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# Config
# =========================
TZ = ZoneInfo("Asia/Kolkata")  # IST (UTC+5:30)
URL_PATH = "webhook"

# Conversation states
WATER_PICK = 101

ADD_NAME = 201

RENAME_PICK = 301
RENAME_NEW = 302

DELETE_PICK = 401
DELETE_CONFIRM = 402

NORMS_SET = 501

# =========================
# DB helpers
# =========================

def db_url() -> str:
    return os.environ["DATABASE_URL"]

def connect():
    # Neon обычно даёт sslmode=require в строке; если нет — добавим.
    url = db_url()
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg.connect(url, row_factory=dict_row)

def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS plants (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        archived BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_watered_at TIMESTAMPTZ
    );

    CREATE UNIQUE INDEX IF NOT EXISTS plants_user_name_uq
        ON plants(user_id, lower(name))
        WHERE archived = FALSE;

    CREATE TABLE IF NOT EXISTS norms (
        plant_id BIGINT PRIMARY KEY REFERENCES plants(id) ON DELETE CASCADE,
        interval_days INT NOT NULL CHECK (interval_days > 0)
    );

    CREATE TABLE IF NOT EXISTS water_log (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        plant_id BIGINT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
        watered_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS meta (
        user_id BIGINT PRIMARY KEY,
        last_autotoday_sent DATE
    );
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)

def now_ist() -> datetime:
    return datetime.now(tz=TZ)

def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def get_plants(user_id: int, include_archived: bool = False):
    q = """
    SELECT id, name, archived, last_watered_at
    FROM plants
    WHERE user_id = %s
    """
    if not include_archived:
        q += " AND archived = FALSE"
    q += " ORDER BY id"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (user_id,))
            return cur.fetchall()

def get_plant_by_number(user_id: int, number: int):
    plants = get_plants(user_id)
    if number < 1 or number > len(plants):
        return None
    return plants[number - 1]

def add_plant_db(user_id: int, name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "Имя пустое."
    with connect() as conn:
        with conn.cursor() as cur:
            # если есть архивное с таким именем — можно разархивировать
            cur.execute(
                """
                SELECT id, archived FROM plants
                WHERE user_id=%s AND lower(name)=lower(%s)
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, name),
            )
            row = cur.fetchone()
            if row and row["archived"]:
                cur.execute("UPDATE plants SET archived=FALSE WHERE id=%s", (row["id"],))
                return True, "Разархивировала существующее растение ✅"
            # иначе обычное добавление
            try:
                cur.execute(
                    "INSERT INTO plants(user_id, name) VALUES (%s, %s)",
                    (user_id, name),
                )
                return True, "Добавлено 🌱"
            except Exception:
                return False, f"У тебя уже есть «{name}». Хочешь другое имя?"

def rename_plant_db(user_id: int, plant_id: int, new_name: str) -> tuple[bool, str]:
    new_name = new_name.strip()
    if not new_name:
        return False, "Имя пустое."
    with connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "UPDATE plants SET name=%s WHERE id=%s AND user_id=%s",
                    (new_name, plant_id, user_id),
                )
                return True, "Переименовано ✅"
            except Exception:
                return False, f"Имя «{new_name}» уже занято."

def archive_plant_db(user_id: int, plant_id: int) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE plants SET archived=TRUE WHERE id=%s AND user_id=%s",
                (plant_id, user_id),
            )

def set_norm_db(plant_id: int, days: int) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO norms(plant_id, interval_days)
                VALUES (%s, %s)
                ON CONFLICT (plant_id)
                DO UPDATE SET interval_days = EXCLUDED.interval_days
                """,
                (plant_id, days),
            )

def get_norms_map(user_id: int) -> dict[int, int]:
    q = """
    SELECT n.plant_id, n.interval_days
    FROM norms n
    JOIN plants p ON p.id = n.plant_id
    WHERE p.user_id = %s AND p.archived = FALSE
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (user_id,))
            rows = cur.fetchall()
    return {r["plant_id"]: r["interval_days"] for r in rows}

def log_water(user_id: int, plant_ids: list[int], when: datetime | None = None) -> int:
    when = when or datetime.now(tz=timezone.utc)
    with connect() as conn:
        with conn.cursor() as cur:
            for pid in plant_ids:
                cur.execute(
                    "INSERT INTO water_log(user_id, plant_id, watered_at) VALUES (%s,%s,%s)",
                    (user_id, pid, when),
                )
                cur.execute(
                    "UPDATE plants SET last_watered_at=%s WHERE id=%s AND user_id=%s",
                    (when, pid, user_id),
                )
    return len(plant_ids)

def compute_today(user_id: int):
    plants = get_plants(user_id)
    norms = get_norms_map(user_id)

    overdue = []
    due_today = []

    now_local = now_ist()
    today_local = now_local.date()

    for p in plants:
        pid = p["id"]
        n = norms.get(pid)
        if not n:
            # если нормы не заданы — не показываем (чтобы не шуметь)
            continue

        last = p["last_watered_at"]
        if last is None:
            # нет данных — считаем "просрочено", потому что по факту нужно задать старт/полить
            overdue.append((p["name"], 9999, None, n))
            continue

        last_local_date = last.astimezone(TZ).date()
        next_due = last_local_date + timedelta(days=n)

        if next_due < today_local:
            days_over = (today_local - next_due).days
            overdue.append((p["name"], days_over, next_due, n))
        elif next_due == today_local:
            due_today.append((p["name"], 0, next_due, n))
        else:
            # "пока не нужно" мы решили не присылать
            pass

    # сортируем: сначала самые просроченные
    overdue.sort(key=lambda x: x[1], reverse=True)
    due_today.sort(key=lambda x: x[0].lower())
    return overdue, due_today

def get_last_autotoday_sent(user_id: int) -> date | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_autotoday_sent FROM meta WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return row["last_autotoday_sent"] if row else None

def set_last_autotoday_sent(user_id: int, d: date) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO meta(user_id, last_autotoday_sent)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET last_autotoday_sent = EXCLUDED.last_autotoday_sent
                """,
                (user_id, d),
            )

# =========================
# Text builders
# =========================

def plants_list_text(plants) -> str:
    if not plants:
        return "Список пуст. Добавь первое растение:\n/add_plant"
    lines = ["Твои растения:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']}")
    return "\n".join(lines)

def norms_text(user_id: int) -> str:
    plants = get_plants(user_id)
    norms = get_norms_map(user_id)
    if not plants:
        return "Список пуст. Добавь растение: /add_plant"
    if not norms:
        return "Нормы пока не заданы. Задай: /set_norms"
    lines = ["Нормы полива:"]
    i = 1
    for p in plants:
        n = norms.get(p["id"])
        if n:
            lines.append(f"{i}. {p['name']} — раз в {n} дн.")
        i += 1
    return "\n".join(lines)

def last_watered_text(user_id: int) -> str:
    plants = get_plants(user_id)
    if not plants:
        return "Список пуст. Добавь растение: /add_plant"
    lines = ["Последний полив:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']} — {fmt_dt(p['last_watered_at'])}")
    return "\n".join(lines)

def today_text(user_id: int) -> str:
    overdue, due_today = compute_today(user_id)

    if not overdue and not due_today:
        return "Сегодня полив не нужен ✅"

    parts = []
    if overdue:
        parts.append("Просрочено:")
        for name, days_over, next_due, n in overdue:
            if next_due is None:
                parts.append(f"• {name} — нет даты последнего полива (норма {n} дн.)")
            elif days_over == 1:
                parts.append(f"• Вчера нужно было полить: {name}")
            else:
                parts.append(f"• {days_over} дн. назад нужно было полить: {name}")
    if due_today:
        if parts:
            parts.append("")
        parts.append("Сегодня:")
        for name, _, __, ___ in due_today:
            parts.append(f"• {name}")
    return "\n".join(parts)

# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant /plants /rename_plant /delete_plant\n"
        "/set_norms /norms\n"
        "/water /today /last_watered\n"
        "/db /cancel"
    )

async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(plants_list_text(get_plants(user_id)))

async def norms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(norms_text(user_id))

async def last_watered_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(last_watered_text(user_id))

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(today_text(user_id))

# ----- /db
async def db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        ensure_schema()
        plants = get_plants(user_id)
        await update.message.reply_text(f"DB OK ✅ plants for you: {len(plants)}")
    except Exception as e:
        await update.message.reply_text(f"DB FAIL ❌ {type(e).__name__}: {e}")

# ----- /cancel
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tmp", None)
    await update.message.reply_text("Ок, отмена ✅")
    return ConversationHandler.END

# =========================
# /add_plant conversation
# =========================

async def add_plant_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ADD_NAME

async def add_plant_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = (update.message.text or "").strip()
    ok, msg = add_plant_db(user_id, name)
    if ok:
        await update.message.reply_text(f"{msg}: {name}\n\nПосмотреть список: /plants")
        return ConversationHandler.END
    else:
        await update.message.reply_text(msg)
        return ADD_NAME

# =========================
# /rename_plant conversation
# =========================

async def rename_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Что переименовать? Введи номер:\n\n" + plants_list_text(plants)
    )
    return RENAME_PICK

async def rename_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Нужен номер. Пример: 3")
        return RENAME_PICK
    num = int(txt)
    plants = context.user_data.get("tmp", {}).get("plants") or []
    if num < 1 or num > len(plants):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return RENAME_PICK
    context.user_data["tmp"]["pick"] = num
    await update.message.reply_text("Ок. Новое имя?")
    return RENAME_NEW

async def rename_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    new_name = (update.message.text or "").strip()
    tmp = context.user_data.get("tmp") or {}
    plants = tmp.get("plants") or []
    num = tmp.get("pick")
    plant = plants[num - 1]
    ok, msg = rename_plant_db(user_id, plant["id"], new_name)
    await update.message.reply_text(msg)
    context.user_data.pop("tmp", None)
    return ConversationHandler.END

# =========================
# /delete_plant conversation (archive)
# =========================

async def delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Что удалить (архивировать)? Введи номер:\n\n" + plants_list_text(plants)
    )
    return DELETE_PICK

async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Нужен номер. Пример: 2")
        return DELETE_PICK
    num = int(txt)
    plants = context.user_data.get("tmp", {}).get("plants") or []
    if num < 1 or num > len(plants):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return DELETE_PICK
    context.user_data["tmp"]["pick"] = num
    plant = plants[num - 1]
    await update.message.reply_text(
        f"Точно архивировать «{plant['name']}»?\nОтветь: yes / no"
    )
    return DELETE_CONFIRM

async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    ans = (update.message.text or "").strip().lower()
    if ans not in ("yes", "no"):
        await update.message.reply_text("Ответь: yes или no")
        return DELETE_CONFIRM
    if ans == "no":
        await update.message.reply_text("Ок, не трогаю ✅")
        context.user_data.pop("tmp", None)
        return ConversationHandler.END

    tmp = context.user_data.get("tmp") or {}
    plants = tmp.get("plants") or []
    num = tmp.get("pick")
    plant = plants[num - 1]
    archive_plant_db(user_id, plant["id"])
    await update.message.reply_text("Готово ✅ (в архив)")
    context.user_data.pop("tmp", None)
    return ConversationHandler.END

# =========================
# /set_norms conversation
# =========================

def parse_norm_pairs(text: str) -> list[tuple[int, int]]:
    """
    Accept:
      "3=5" or "3=5, 4=7" or "3=5 4=7"
    Also allow ":" instead of "=".
    """
    text = text.replace(":", "=")
    chunks = re.split(r"[,\n ]+", text.strip())
    pairs = []
    for c in chunks:
        if not c:
            continue
        if "=" not in c:
            continue
        a, b = c.split("=", 1)
        a = a.strip()
        b = b.strip()
        if a.isdigit() and b.isdigit():
            pairs.append((int(a), int(b)))
    return pairs

async def set_norms_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Задай нормы полива в формате номер=дни.\n"
        "Можно несколько через запятую.\n\n"
        "Пример: 1=7, 3=4, 10=3\n\n"
        + plants_list_text(plants)
    )
    return NORMS_SET

async def set_norms_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text or ""
    pairs = parse_norm_pairs(text)
    plants = context.user_data.get("tmp", {}).get("plants") or []

    if not pairs:
        await update.message.reply_text("Не вижу пар. Пример: 1=7, 3=4")
        return NORMS_SET

    updated = 0
    bad = []
    for num, days in pairs:
        if num < 1 or num > len(plants) or days <= 0:
            bad.append(f"{num}={days}")
            continue
        plant = plants[num - 1]
        set_norm_db(plant["id"], days)
        updated += 1

    msg = [f"Готово ✅ Обновлено норм: {updated}"]
    if bad:
        msg.append("Пропустила (проверь номера): " + ", ".join(bad))
    await update.message.reply_text("\n".join(msg))
    context.user_data.pop("tmp", None)
    return ConversationHandler.END

# =========================
# /water conversation (multi)
# =========================

def parse_numbers(text: str) -> list[int]:
    # accept "1,3,5" or "1 3 5" or mixed
    nums = re.findall(r"\d+", text)
    out = []
    for n in nums:
        try:
            out.append(int(n))
        except Exception:
            pass
    # unique, keep order
    seen = set()
    res = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res

async def water_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END

    context.user_data["tmp"] = {"plants": plants}
    lines = [
        "Что ты полила? Введи номера через запятую:\n",
    ]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']}")
    lines.append("\nПример: 1,3,5")
    await update.message.reply_text("\n".join(lines))
    return WATER_PICK

async def water_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text or ""
    nums = parse_numbers(text)
    plants = context.user_data.get("tmp", {}).get("plants") or []

    if not nums:
        await update.message.reply_text("Не вижу номеров. Пример: 2,4,5")
        return WATER_PICK

    plant_ids = []
    picked_names = []
    bad = []
    for n in nums:
        if n < 1 or n > len(plants):
            bad.append(str(n))
            continue
        plant = plants[n - 1]
        plant_ids.append(plant["id"])
        picked_names.append(plant["name"])

    if not plant_ids:
        await update.message.reply_text("Все номера неверные. Попробуй ещё раз.")
        return WATER_PICK

    count = log_water(user_id, plant_ids, when=datetime.now(tz=timezone.utc))
    lines = ["Зафиксировала полив ✅"]
    for name in picked_names:
        lines.append(f"• {name}")
    lines.append(f"\nОбновлено: {count}")
    if bad:
        lines.append(f"\nПропустила номера: {', '.join(bad)}")
    await update.message.reply_text("\n".join(lines))

    context.user_data.pop("tmp", None)
    return ConversationHandler.END

# =========================
# Auto-today (daily 11:00 IST)
# =========================

async def autotoday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Если chat_id не задан — пропускаем
    chat_id = context.job.chat_id
    user_id = context.job.data.get("user_id")
    if not chat_id or not user_id:
        return

    # 1 раз в день
    today_local = now_ist().date()
    last_sent = get_last_autotoday_sent(user_id)
    if last_sent == today_local:
        return

    await context.bot.send_message(chat_id=chat_id, text=today_text(user_id))
    set_last_autotoday_sent(user_id, today_local)

async def enable_autotoday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Включает ежедневный автопуш /today в 11:00 IST для этого чата.
    Команда не в списке, но пусть будет (вдруг пригодится).
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # удалим старые такие же jobs, чтобы не дублировать
    for j in context.job_queue.get_jobs_by_name(f"autotoday:{chat_id}:{user_id}"):
        j.schedule_removal()

    # рассчитать ближайшие 11:00 IST
    now_local = now_ist()
    target = now_local.replace(hour=11, minute=0, second=0, microsecond=0)
    if now_local >= target:
        target = target + timedelta(days=1)

    # job_queue использует datetime в UTC
    when_utc = target.astimezone(timezone.utc)

    context.job_queue.run_repeating(
        autotoday_job,
        interval=timedelta(days=1),
        first=when_utc,
        name=f"autotoday:{chat_id}:{user_id}",
        chat_id=chat_id,
        data={"user_id": user_id},
    )

    await update.message.reply_text("Ок ✅ буду присылать /today каждый день в 11:00 (IST)")

# =========================
# Main
# =========================

def main() -> None:
    ensure_schema()

    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    app = Application.builder().token(token).build()

    # Conversations (order matters!)
    water_conv = ConversationHandler(
        entry_points=[CommandHandler("water", water_entry)],
        states={WATER_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_pick)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="water_conv",
        persistent=False,
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_plant", add_plant_entry)],
        states={ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plant_name)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="add_conv",
        persistent=False,
    )

    rename_conv = ConversationHandler(
        entry_points=[CommandHandler("rename_plant", rename_entry)],
        states={
            RENAME_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_pick)],
            RENAME_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_new)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="rename_conv",
        persistent=False,
    )

    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("delete_plant", delete_entry)],
        states={
            DELETE_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_pick)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="delete_conv",
        persistent=False,
    )

    norms_conv = ConversationHandler(
        entry_points=[CommandHandler("set_norms", set_norms_entry)],
        states={NORMS_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norms_apply)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="norms_conv",
        persistent=False,
    )

    # Register handlers
    app.add_handler(water_conv)
    app.add_handler(add_conv)
    app.add_handler(rename_conv)
    app.add_handler(delete_conv)
    app.add_handler(norms_conv)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("norms", norms_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("last_watered", last_watered_cmd))
    app.add_handler(CommandHandler("db", db_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # optional: enable auto-today (we can later expose it as a normal step)
    app.add_handler(CommandHandler("enable_autotoday", enable_autotoday_cmd))

    # Run webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=URL_PATH,
        webhook_url=f"{base_url}/{URL_PATH}",
    )

if __name__ == "__main__":
    main()
