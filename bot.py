import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple, Dict

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
TZ = ZoneInfo("Asia/Kolkata")  # local timezone only (UTC+5:30)
REMINDER_HOUR = 11
REMINDER_MINUTE = 0

BOT_TOKEN_ENV = "BOT_TOKEN"
BASE_URL_ENV = "BASE_URL"
DB_URL_ENV = "DATABASE_URL"
PORT_ENV = "PORT"

WEBHOOK_PATH = "webhook"

# =========================
# DB helpers
# =========================
def _db_url() -> str:
    url = os.environ.get(DB_URL_ENV, "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is missing")
    return url

def _connect():
    # Neon/Render URLs often start with postgres:// ; psycopg accepts both.
    return psycopg.connect(_db_url(), row_factory=dict_row)

def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        LIMIT 1;
        """,
        (table, col),
    )
    return cur.fetchone() is not None

def ensure_schema() -> None:
    """Idempotent schema + safe migration active->archived (only if needed)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            # plants
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plants (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT now(),
                    UNIQUE(user_id, name)
                );
                """
            )

            # norms (watering interval in days)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS norms (
                    plant_id INT PRIMARY KEY REFERENCES plants(id) ON DELETE CASCADE,
                    interval_days INT NOT NULL CHECK (interval_days > 0)
                );
                """
            )

            # waterings log
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS waterings (
                    id SERIAL PRIMARY KEY,
                    plant_id INT REFERENCES plants(id) ON DELETE CASCADE,
                    watered_at DATE NOT NULL
                );
                """
            )

            # ---- migration: active -> archived (legacy) ----
            has_archived = _col_exists(cur, "plants", "archived")
            has_active = _col_exists(cur, "plants", "active")

            # If someone had legacy schema with "active", we convert it to archived.
            # If archived already exists, we do nothing.
            if has_active and not has_archived:
                cur.execute(
                    "ALTER TABLE plants ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;"
                )
                cur.execute("UPDATE plants SET archived = NOT active;")

# =========================
# DB operations
# =========================
def db_diag() -> Tuple[bool, str]:
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.execute("SELECT COUNT(*) AS c FROM plants;")
                c = cur.fetchone()["c"]
        return True, f"DB OK ✅ plants total: {c}"
    except Exception as e:
        return False, f"DB ERROR ❌ {type(e).__name__}: {e}"

def list_plants(user_id: int) -> List[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name
                FROM plants
                WHERE user_id=%s AND archived=FALSE
                ORDER BY id;
                """,
                (user_id,),
            )
            return cur.fetchall()

def add_plant(user_id: int, name: str) -> Tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "Имя пустое."
    with _connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO plants(user_id, name) VALUES (%s,%s) RETURNING id;",
                    (user_id, name),
                )
                _ = cur.fetchone()["id"]
                return True, f"Добавлено 🌱: {name}"
            except Exception:
                return False, f'У тебя уже есть «{name}». Хочешь другое имя?'

def rename_plant(user_id: int, plant_id: int, new_name: str) -> Tuple[bool, str]:
    new_name = new_name.strip()
    if not new_name:
        return False, "Имя пустое."
    with _connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "UPDATE plants SET name=%s WHERE id=%s AND user_id=%s AND archived=FALSE;",
                    (new_name, plant_id, user_id),
                )
                if cur.rowcount == 0:
                    return False, "Не нашла такое растение."
                return True, "Переименовано ✅"
            except Exception:
                return False, f'Имя «{new_name}» уже занято.'

def archive_plant(user_id: int, plant_id: int) -> Tuple[bool, str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE plants SET archived=TRUE WHERE id=%s AND user_id=%s AND archived=FALSE;",
                (plant_id, user_id),
            )
            if cur.rowcount == 0:
                return False, "Не нашла такое растение."
            return True, "Удалено (архивировано) ✅"

def set_norm(plant_id: int, days: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO norms(plant_id, interval_days)
                VALUES (%s,%s)
                ON CONFLICT (plant_id) DO UPDATE
                SET interval_days=EXCLUDED.interval_days;
                """,
                (plant_id, days),
            )

def get_norms(user_id: int) -> List[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.name, n.interval_days
                FROM plants p
                JOIN norms n ON n.plant_id = p.id
                WHERE p.user_id=%s AND p.archived=FALSE
                ORDER BY p.id;
                """,
                (user_id,),
            )
            return cur.fetchall()

def get_last_watered(user_id: int) -> List[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.name, MAX(w.watered_at) AS last_watered
                FROM plants p
                LEFT JOIN waterings w ON w.plant_id = p.id
                WHERE p.user_id=%s AND p.archived=FALSE
                GROUP BY p.id, p.name
                ORDER BY p.id;
                """,
                (user_id,),
            )
            return cur.fetchall()

def log_watering(plant_ids: List[int], watered_on: date) -> int:
    if not plant_ids:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO waterings(plant_id, watered_at) VALUES (%s,%s);",
                [(pid, watered_on) for pid in plant_ids],
            )
            return len(plant_ids)

def init_last_watered(user_id: int, mapping: Dict[int, date]) -> int:
    """mapping: plant_id -> date"""
    if not mapping:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            # ensure plants belong to user and not archived
            cur.execute(
                "SELECT id FROM plants WHERE user_id=%s AND archived=FALSE;",
                (user_id,),
            )
            allowed = {row["id"] for row in cur.fetchall()}
            rows = [(pid, d) for pid, d in mapping.items() if pid in allowed]
            cur.executemany(
                "INSERT INTO waterings(plant_id, watered_at) VALUES (%s,%s);",
                rows,
            )
            return len(rows)

def get_users() -> List[int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_id FROM plants;")
            return [r["user_id"] for r in cur.fetchall()]

def today_due_for_user(user_id: int) -> Tuple[List[str], List[str]]:
    """Return (overdue_lines, today_lines). Excludes 'Пока не нужно' by design."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.name,
                    n.interval_days,
                    MAX(w.watered_at) AS last_watered
                FROM plants p
                LEFT JOIN norms n ON n.plant_id = p.id
                LEFT JOIN waterings w ON w.plant_id = p.id
                WHERE p.user_id=%s AND p.archived=FALSE
                GROUP BY p.id, p.name, n.interval_days
                ORDER BY p.id;
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    today = datetime.now(TZ).date()
    overdue, due_today = [], []

    for r in rows:
        name = r["name"]
        interval = r["interval_days"]
        last = r["last_watered"]

        if interval is None:
            # no norm -> ignore in today list (keeps output clean)
            continue

        if last is None:
            # no last watered -> treat as overdue
            overdue.append(f"• {name} — нет даты последнего полива")
            continue

        next_due = last + timedelta(days=int(interval))
        delta = (today - next_due).days

        if delta == 0:
            due_today.append(f"• {name} — сегодня")
        elif delta > 0:
            # yesterday phrasing for delta==1
            if delta == 1:
                overdue.append(f"• {name} — вчера нужно было полить")
            else:
                overdue.append(f"• {name} — просрочено на {delta} дн.")

    return overdue, due_today

# =========================
# Bot UX helpers
# =========================
def _fmt_plants(plants: List[dict]) -> str:
    lines = ["Твои растения:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']}")
    return "\n".join(lines)

def _parse_numbers(text: str) -> List[int]:
    nums = re.findall(r"\d+", text)
    return [int(x) for x in nums]

def _id_by_index(plants: List[dict], indices: List[int]) -> List[int]:
    ids = []
    for idx in indices:
        if 1 <= idx <= len(plants):
            ids.append(plants[idx - 1]["id"])
    # unique preserve order
    seen = set()
    out = []
    for pid in ids:
        if pid not in seen:
            out.append(pid)
            seen.add(pid)
    return out

def _parse_init_lines(text: str, plants: List[dict]) -> Dict[int, date]:
    """
    Accept lines:
      1=2026-01-10
      2 2026-01-12
      3:2026-01-05
    """
    mapping: Dict[int, date] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)\s*[:= ]\s*(\d{4}-\d{2}-\d{2})\s*$", line)
        if not m:
            continue
        idx = int(m.group(1))
        ds = m.group(2)
        try:
            d = date.fromisoformat(ds)
        except Exception:
            continue
        if 1 <= idx <= len(plants):
            mapping[plants[idx - 1]["id"]] = d
    return mapping

# =========================
# Conversation states
# =========================
ADD_NAME = 10
RENAME_PICK, RENAME_NEW = 20, 21
DELETE_PICK = 30
SETNORM_PICK, SETNORM_DAYS = 40, 41
WATER_PICK = 50
INITLAST_INPUT = 60

# =========================
# Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Я живой ✅")

async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, msg = db_diag()
    await update.message.reply_text(msg)

async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь первое растение:\n/add_plant")
        return
    await update.message.reply_text(_fmt_plants(plants))

# --- add plant ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ADD_NAME

async def add_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = (update.message.text or "").strip()
    ok, msg = add_plant(user_id, name)
    await update.message.reply_text(msg + "\n\nПосмотреть список: /plants")
    return ConversationHandler.END

# --- rename ---
async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. /add_plant")
        return ConversationHandler.END
    context.user_data["plants_cache"] = plants
    await update.message.reply_text("Что переименовать? Введи номер:\n\n" + _fmt_plants(plants))
    return RENAME_PICK

async def rename_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plants = context.user_data.get("plants_cache", [])
    idxs = _parse_numbers(update.message.text or "")
    if len(idxs) != 1:
        await update.message.reply_text("Нужен один номер. Пример: 3")
        return RENAME_PICK
    idx = idxs[0]
    if not (1 <= idx <= len(plants)):
        await update.message.reply_text("Нет такого номера.")
        return RENAME_PICK
    context.user_data["rename_pid"] = plants[idx - 1]["id"]
    await update.message.reply_text("Ок. Введи новое имя:")
    return RENAME_NEW

async def rename_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    pid = int(context.user_data["rename_pid"])
    new_name = (update.message.text or "").strip()
    ok, msg = rename_plant(user_id, pid, new_name)
    await update.message.reply_text(msg)
    return ConversationHandler.END

# --- delete/archive ---
async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст.")
        return ConversationHandler.END
    context.user_data["plants_cache"] = plants
    await update.message.reply_text("Что удалить (архивировать)? Введи номер:\n\n" + _fmt_plants(plants))
    return DELETE_PICK

async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("plants_cache", [])
    idxs = _parse_numbers(update.message.text or "")
    if len(idxs) != 1:
        await update.message.reply_text("Нужен один номер. Пример: 2")
        return DELETE_PICK
    idx = idxs[0]
    if not (1 <= idx <= len(plants)):
        await update.message.reply_text("Нет такого номера.")
        return DELETE_PICK
    pid = plants[idx - 1]["id"]
    ok, msg = archive_plant(user_id, pid)
    await update.message.reply_text(msg)
    return ConversationHandler.END

# --- norms ---
async def norms_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = get_norms(user_id)
    if not rows:
        await update.message.reply_text("Нормы пока не заданы. /set_norms")
        return
    lines = ["Нормы полива:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['name']} — раз в {r['interval_days']} дн.")
    await update.message.reply_text("\n".join(lines))

async def set_norms_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. /add_plant")
        return ConversationHandler.END
    context.user_data["plants_cache"] = plants
    await update.message.reply_text("Для какого растения задать норму? Введи номер:\n\n" + _fmt_plants(plants))
    return SETNORM_PICK

async def set_norms_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plants = context.user_data.get("plants_cache", [])
    idxs = _parse_numbers(update.message.text or "")
    if len(idxs) != 1:
        await update.message.reply_text("Нужен один номер. Пример: 1")
        return SETNORM_PICK
    idx = idxs[0]
    if not (1 <= idx <= len(plants)):
        await update.message.reply_text("Нет такого номера.")
        return SETNORM_PICK
    context.user_data["norm_pid"] = plants[idx - 1]["id"]
    await update.message.reply_text("Введи интервал в днях (например: 5)")
    return SETNORM_DAYS

async def set_norms_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pid = int(context.user_data["norm_pid"])
    idxs = _parse_numbers(update.message.text or "")
    if len(idxs) != 1 or idxs[0] <= 0:
        await update.message.reply_text("Нужно число дней > 0. Пример: 7")
        return SETNORM_DAYS
    days = idxs[0]
    set_norm(pid, days)
    await update.message.reply_text("Норма сохранена ✅\n\nПосмотреть: /norms")
    return ConversationHandler.END

# --- last watered ---
async def cmd_last_watered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = get_last_watered(user_id)
    if not rows:
        await update.message.reply_text("Список пуст.")
        return
    lines = ["Последний полив:"]
    for i, r in enumerate(rows, 1):
        lw = r["last_watered"]
        lw_txt = lw.isoformat() if lw else "нет данных"
        lines.append(f"{i}. {r['name']} — {lw_txt}")
    await update.message.reply_text("\n".join(lines))

# --- init last watered (different dates for different plants) ---
async def init_last_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. /add_plant")
        return ConversationHandler.END
    context.user_data["plants_cache"] = plants
    await update.message.reply_text(
        "Ок, зададим последний полив для каждого растения.\n"
        "Пришли мне список строк в формате:\n"
        "1=2026-01-10\n"
        "2 2026-01-12\n"
        "3:2026-01-05\n\n"
        "Нумерация — как в /plants.\n\n"
        + _fmt_plants(plants)
    )
    return INITLAST_INPUT

async def init_last_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("plants_cache", [])
    text = update.message.text or ""
    mapping = _parse_init_lines(text, plants)
    if not mapping:
        await update.message.reply_text("Не распознала ни одной строки. Проверь формат, пожалуйста.")
        return INITLAST_INPUT
    updated = init_last_watered(user_id, mapping)
    await update.message.reply_text(f"Инициализация завершена ✅\nОбновлено растений: {updated}")
    return ConversationHandler.END

# --- water (multi select) ---
async def water_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. /add_plant")
        return ConversationHandler.END
    context.user_data["plants_cache"] = plants
    await update.message.reply_text(
        "Что ты полила? Введи номера через запятую:\n\n"
        + _fmt_plants(plants)
        + "\n\nПример: 1,3,5"
    )
    return WATER_PICK

async def water_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("plants_cache", [])
    idxs = _parse_numbers(update.message.text or "")
    pids = _id_by_index(plants, idxs)
    if not pids:
        await update.message.reply_text("Не распознала номера. Пример: 2,4,7")
        return WATER_PICK
    today = datetime.now(TZ).date()
    n = log_watering(pids, today)
    names = [p["name"] for p in plants if p["id"] in set(pids)]
    await update.message.reply_text(
        "Зафиксировала полив ✅\n"
        + "\n".join([f"• {nm}" for nm in names])
        + f"\n\nОбновлено: {n}"
    )
    return ConversationHandler.END

# --- today ---
def _format_today(overdue: List[str], today: List[str]) -> str:
    if not overdue and not today:
        return "Сегодня полив не нужен ✅"
    parts = []
    if overdue:
        parts.append("Просрочено:")
        parts.extend(overdue)
    if today:
        parts.append("\nСегодня:")
        parts.extend(today)
    return "\n".join(parts).strip()

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    overdue, due_today = today_due_for_user(user_id)
    await update.message.reply_text(_format_today(overdue, due_today))

# --- cancel ---
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отмена ✅")
    return ConversationHandler.END

# =========================
# Reminders without JobQueue (no paid/extra deps)
# =========================
async def _send_daily_updates(app: Application) -> None:
    """Loop forever: every day at 11:00 IST, send /today-style update to all users."""
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), time(REMINDER_HOUR, REMINDER_MINUTE), TZ)
        if now >= target:
            target = target + timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        await asyncio.sleep(sleep_s)

        try:
            user_ids = get_users()
            for uid in user_ids:
                overdue, due_today = today_due_for_user(uid)
                msg = _format_today(overdue, due_today)
                await app.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            # keep loop alive even if one send fails
            pass

# =========================
# Main
# =========================
def main() -> None:
    ensure_schema()

    token = os.environ[BOT_TOKEN_ENV]
    base_url = os.environ[BASE_URL_ENV].rstrip("/")
    port = int(os.environ.get(PORT_ENV, "10000"))

    app = Application.builder().token(token).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("plants", cmd_plants))
    app.add_handler(CommandHandler("norms", norms_show))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("last_watered", cmd_last_watered))
    app.add_handler(CommandHandler("db", cmd_db))

    # conversations
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add_plant", add_start)],
            states={ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_name)]},
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("rename_plant", rename_start)],
            states={
                RENAME_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_pick)],
                RENAME_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_new)],
            },
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("delete_plant", delete_start)],
            states={DELETE_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_pick)]},
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("set_norms", set_norms_start)],
            states={
                SETNORM_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norms_pick)],
                SETNORM_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norms_days)],
            },
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("water", water_start)],
            states={WATER_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_pick)]},
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("init_last", init_last_start)],
            states={INITLAST_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_last_input)]},
            fallbacks=[CommandHandler("cancel", cmd_cancel)],
        )
    )

    # start reminders loop (no JobQueue)
    app.create_task(_send_daily_updates(app))

    # webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{base_url}/{WEBHOOK_PATH}",
    )

if __name__ == "__main__":
    main()
