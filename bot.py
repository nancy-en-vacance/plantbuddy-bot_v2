import os
import re
import logging
from datetime import datetime, timedelta, date, timezone
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

# --------------------
# Config
# --------------------
TZ = ZoneInfo("Asia/Kolkata")   # UTC+5:30
AUTO_TODAY_HOUR = 11
AUTO_TODAY_MINUTE = 0
URL_PATH = "webhook"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("plantbuddy")

# Conversation states
ADD_NAME = 10

RENAME_PICK = 20
RENAME_NEW = 21

DELETE_PICK = 30
DELETE_CONFIRM = 31

NORMS_SET = 40

WATER_PICK = 50


# --------------------
# DB helpers
# --------------------
def _db_url() -> str:
    return os.environ["DATABASE_URL"]


def _connect():
    url = _db_url()
    # render/neon часто требует ssl
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg.connect(url, row_factory=dict_row)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _status_mode(cur) -> str:
    """
    Возвращает 'archived' если есть plants.archived,
    иначе 'active' (старый режим).
    """
    if _column_exists(cur, "plants", "archived"):
        return "archived"
    return "active"


def ensure_schema():
    """
    Мягкая миграция:
    - если plants не было — создаём
    - если plants есть со старой колонкой active — работаем через неё
    - добавляем недостающие таблицы/колонки, не ломая данные
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            # базовая таблица plants (без статуса)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plants (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            # если нет ни archived ни active — добавим archived (новая схема)
            has_archived = _column_exists(cur, "plants", "archived")
            has_active = _column_exists(cur, "plants", "active")
            if not has_archived and not has_active:
                cur.execute("ALTER TABLE plants ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;")

            # last_watered_at
            if not _column_exists(cur, "plants", "last_watered_at"):
                cur.execute("ALTER TABLE plants ADD COLUMN last_watered_at TIMESTAMPTZ;")

            # norms
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS norms (
                    plant_id BIGINT PRIMARY KEY REFERENCES plants(id) ON DELETE CASCADE,
                    interval_days INT NOT NULL CHECK (interval_days > 0)
                );
                """
            )

            # water_log
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS water_log (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_id BIGINT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
                    watered_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            # meta (для авто-today)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    user_id BIGINT PRIMARY KEY,
                    last_autotoday_sent DATE
                );
                """
            )

            # индексы (мягко)
            mode = _status_mode(cur)
            if mode == "archived":
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS plants_user_name_uq
                    ON plants(user_id, lower(name))
                    WHERE archived = FALSE;
                    """
                )
            else:
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS plants_user_name_uq
                    ON plants(user_id, lower(name))
                    WHERE active = TRUE;
                    """
                )


def _active_where(cur) -> str:
    mode = _status_mode(cur)
    return "archived = FALSE" if mode == "archived" else "active = TRUE"


def get_plants(user_id: int):
    with _connect() as conn:
        with conn.cursor() as cur:
            where = _active_where(cur)
            cur.execute(
                f"""
                SELECT id, name, created_at, last_watered_at
                FROM plants
                WHERE user_id=%s AND {where}
                ORDER BY id
                """,
                (user_id,),
            )
            return cur.fetchall()


def add_plant_db(user_id: int, name: str) -> tuple[bool, str]:
    name = (name or "").strip()
    if not name:
        return False, "Имя пустое."

    with _connect() as conn:
        with conn.cursor() as cur:
            mode = _status_mode(cur)

            # если было "в архиве" (или inactive) — вернём назад
            cur.execute(
                """
                SELECT id
                FROM plants
                WHERE user_id=%s AND lower(name)=lower(%s)
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, name),
            )
            row = cur.fetchone()
            if row:
                pid = row["id"]
                if mode == "archived":
                    cur.execute("UPDATE plants SET archived=FALSE WHERE id=%s", (pid,))
                    return True, "Разархивировала ✅"
                else:
                    cur.execute("UPDATE plants SET active=TRUE WHERE id=%s", (pid,))
                    return True, "Разархивировала ✅"

            # иначе вставим новое
            try:
                if mode == "archived":
                    cur.execute(
                        "INSERT INTO plants(user_id, name, archived) VALUES (%s,%s,FALSE)",
                        (user_id, name),
                    )
                else:
                    # если active колонки нет (на всякий) — fallback: archived
                    if _column_exists(cur, "plants", "active"):
                        cur.execute(
                            "INSERT INTO plants(user_id, name, active) VALUES (%s,%s,TRUE)",
                            (user_id, name),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO plants(user_id, name, archived) VALUES (%s,%s,FALSE)",
                            (user_id, name),
                        )
                return True, "Добавлено 🌱"
            except Exception:
                return False, f"У тебя уже есть «{name}». Хочешь другое имя?"


def rename_plant_db(user_id: int, plant_id: int, new_name: str) -> tuple[bool, str]:
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "Имя пустое."
    with _connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "UPDATE plants SET name=%s WHERE id=%s AND user_id=%s",
                    (new_name, plant_id, user_id),
                )
                return True, "Переименовано ✅"
            except Exception:
                return False, "Такое имя уже занято."


def archive_plant_db(user_id: int, plant_id: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            mode = _status_mode(cur)
            if mode == "archived":
                cur.execute(
                    "UPDATE plants SET archived=TRUE WHERE id=%s AND user_id=%s",
                    (plant_id, user_id),
                )
            else:
                cur.execute(
                    "UPDATE plants SET active=FALSE WHERE id=%s AND user_id=%s",
                    (plant_id, user_id),
                )


def set_norm_db(user_id: int, plant_id: int, days: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            # проверим что растение принадлежит юзеру и активно
            where = _active_where(cur)
            cur.execute(
                f"SELECT 1 FROM plants WHERE id=%s AND user_id=%s AND {where}",
                (plant_id, user_id),
            )
            if not cur.fetchone():
                return
            cur.execute(
                """
                INSERT INTO norms(plant_id, interval_days)
                VALUES (%s,%s)
                ON CONFLICT (plant_id)
                DO UPDATE SET interval_days = EXCLUDED.interval_days
                """,
                (plant_id, days),
            )


def get_norms_map(user_id: int) -> dict[int, int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            where = _active_where(cur)
            cur.execute(
                f"""
                SELECT n.plant_id, n.interval_days
                FROM norms n
                JOIN plants p ON p.id = n.plant_id
                WHERE p.user_id=%s AND {where}
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return {r["plant_id"]: r["interval_days"] for r in rows}


def log_water(user_id: int, plant_ids: list[int]) -> int:
    when = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            where = _active_where(cur)
            ok_ids = []
            for pid in plant_ids:
                cur.execute(
                    f"SELECT 1 FROM plants WHERE id=%s AND user_id=%s AND {where}",
                    (pid, user_id),
                )
                if cur.fetchone():
                    ok_ids.append(pid)

            for pid in ok_ids:
                cur.execute(
                    "INSERT INTO water_log(user_id, plant_id, watered_at) VALUES (%s,%s,%s)",
                    (user_id, pid, when),
                )
                cur.execute(
                    "UPDATE plants SET last_watered_at=%s WHERE id=%s AND user_id=%s",
                    (when, pid, user_id),
                )
    return len(ok_ids)


def compute_today(user_id: int):
    plants = get_plants(user_id)
    norms = get_norms_map(user_id)

    overdue = []
    due_today = []

    today_local = datetime.now(TZ).date()

    for p in plants:
        pid = p["id"]
        n = norms.get(pid)
        if not n:
            continue

        last = p["last_watered_at"]
        if last is None:
            overdue.append((p["name"], None, n))
            continue

        last_local = last.astimezone(TZ).date()
        due = last_local + timedelta(days=n)

        if due < today_local:
            days_over = (today_local - due).days
            overdue.append((p["name"], days_over, n))
        elif due == today_local:
            due_today.append((p["name"], 0, n))

    # просрочка — по убыванию, сегодня — по алфавиту
    overdue.sort(key=lambda x: (x[1] is None, x[1] or 10**9), reverse=True)
    due_today.sort(key=lambda x: x[0].lower())
    return overdue, due_today


def get_last_autotoday_sent(user_id: int) -> date | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_autotoday_sent FROM meta WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return row["last_autotoday_sent"] if row else None


def set_last_autotoday_sent(user_id: int, d: date) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO meta(user_id, last_autotoday_sent)
                VALUES (%s,%s)
                ON CONFLICT (user_id)
                DO UPDATE SET last_autotoday_sent = EXCLUDED.last_autotoday_sent
                """,
                (user_id, d),
            )


# --------------------
# Formatting
# --------------------
def plants_list_text(plants) -> str:
    if not plants:
        return "Список пуст. Добавь первое растение: /add_plant"
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
    for i, p in enumerate(plants, 1):
        n = norms.get(p["id"])
        if n:
            lines.append(f"{i}. {p['name']} — раз в {n} дн.")
    return "\n".join(lines)


def last_watered_text(user_id: int) -> str:
    plants = get_plants(user_id)
    if not plants:
        return "Список пуст. Добавь растение: /add_plant"

    def fmt(dt):
        if not dt:
            return "—"
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

    lines = ["Последний полив:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']} — {fmt(p['last_watered_at'])}")
    return "\n".join(lines)


def today_text(user_id: int) -> str:
    overdue, due_today = compute_today(user_id)

    if not overdue and not due_today:
        return "Сегодня полив не нужен ✅"

    parts = []
    if overdue:
        parts.append("Просрочено:")
        for name, days_over, n in overdue:
            if days_over is None:
                parts.append(f"• {name} — нет даты последнего полива (норма {n} дн.)")
            elif days_over == 1:
                parts.append(f"• Вчера нужно было полить: {name}")
            else:
                parts.append(f"• {days_over} дн. назад нужно было полить: {name}")

    if due_today:
        if parts:
            parts.append("")
        parts.append("Сегодня:")
        for name, _, __ in due_today:
            parts.append(f"• {name}")

    return "\n".join(parts)


def parse_numbers(text: str) -> list[int]:
    # берём все числа из строки (поддерживает "3,4,5 6 7 10,8,9")
    nums = re.findall(r"\d+", text or "")
    return [int(x) for x in nums]


def parse_norm_pairs(text: str) -> list[tuple[int, int]]:
    # формат: "1=7, 3=4" или "1:7 3:4"
    t = (text or "").replace(":", "=")
    chunks = re.split(r"[,\n ]+", t.strip())
    pairs = []
    for c in chunks:
        if not c or "=" not in c:
            continue
        a, b = c.split("=", 1)
        a, b = a.strip(), b.strip()
        if a.isdigit() and b.isdigit():
            pairs.append((int(a), int(b)))
    return pairs


# --------------------
# Commands
# --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant — добавить растение\n"
        "/plants — список\n"
        "/rename_plant — переименовать\n"
        "/delete_plant — удалить (в архив)\n"
        "/set_norms — задать нормы\n"
        "/norms — показать нормы\n"
        "/water — отметить полив (можно несколько)\n"
        "/last_watered — последний полив\n"
        "/today — что полить\n"
        "/db — диагностика\n"
        "/cancel — отмена"
    )


async def db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        ensure_schema()
        plants = get_plants(user_id)
        await update.message.reply_text(f"DB OK ✅ plants for you: {len(plants)}")
    except Exception as e:
        await update.message.reply_text(f"DB FAIL ❌ {type(e).__name__}: {e}")


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


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tmp", None)
    await update.message.reply_text("Ок, отмена ✅")
    return ConversationHandler.END


# --------------------
# /add_plant
# --------------------
async def add_plant_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ADD_NAME


async def add_plant_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = (update.message.text or "").strip()
    ok, msg = add_plant_db(user_id, name)
    if ok:
        await update.message.reply_text(f"Добавлено 🌱: {name}\n\nПосмотреть список: /plants")
        return ConversationHandler.END
    await update.message.reply_text(msg)
    return ADD_NAME


# --------------------
# /rename_plant
# --------------------
async def rename_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text("Что переименовать? Введи номер:\n\n" + plants_list_text(plants))
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


# --------------------
# /delete_plant (archive)
# --------------------
async def delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text("Что удалить (в архив)? Введи номер:\n\n" + plants_list_text(plants))
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
    await update.message.reply_text(f"Точно архивировать «{plant['name']}»?\nОтветь: yes / no")
    return DELETE_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ans = (update.message.text or "").strip().lower()
    if ans not in ("yes", "no"):
        await update.message.reply_text("Ответь: yes или no")
        return DELETE_CONFIRM
    if ans == "no":
        await update.message.reply_text("Ок, не трогаю ✅")
        context.user_data.pop("tmp", None)
        return ConversationHandler.END

    user_id = update.effective_user.id
    tmp = context.user_data.get("tmp") or {}
    plants = tmp.get("plants") or []
    num = tmp.get("pick")
    plant = plants[num - 1]
    archive_plant_db(user_id, plant["id"])
    await update.message.reply_text("Готово ✅ (в архив)")
    context.user_data.pop("tmp", None)
    return ConversationHandler.END


# --------------------
# /set_norms
# --------------------
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
        "Пример: 1=7, 3=4\n\n" + plants_list_text(plants)
    )
    return NORMS_SET


async def set_norms_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("tmp", {}).get("plants") or []
    pairs = parse_norm_pairs(update.message.text or "")

    if not pairs:
        await update.message.reply_text("Не поняла формат. Пример: 1=7, 3=4")
        return NORMS_SET

    updated = 0
    for idx, days in pairs:
        if 1 <= idx <= len(plants) and days > 0:
            set_norm_db(user_id, plants[idx - 1]["id"], days)
            updated += 1

    await update.message.reply_text(f"Готово ✅ Обновлено норм: {updated}\n\nПосмотреть: /norms")
    context.user_data.pop("tmp", None)
    return ConversationHandler.END


# --------------------
# /water (multi)
# --------------------
async def water_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = get_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END

    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Что ты полила? Введи номера через запятую:\n\n"
        + plants_list_text(plants)
        + "\n\nПример: 1,3,5"
    )
    return WATER_PICK


async def water_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("tmp", {}).get("plants") or []
    nums = parse_numbers(update.message.text or "")

    # уникальные, в диапазоне
    nums = sorted({n for n in nums if 1 <= n <= len(plants)})
    if not nums:
        await update.message.reply_text("Не вижу номеров. Пример: 2,4,5")
        return WATER_PICK

    plant_ids = [plants[n - 1]["id"] for n in nums]
    count = log_water(user_id, plant_ids)

    names = [plants[n - 1]["name"] for n in nums]
    text = "Зафиксировала полив ✅\n" + "\n".join([f"• {x}" for x in names]) + f"\n\nОбновлено: {count}"
    await update.message.reply_text(text)

    context.user_data.pop("tmp", None)
    return ConversationHandler.END


# --------------------
# Auto-today job
# --------------------
async def auto_today_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Каждому пользователю из meta шлём today 1 раз в день.
    Если пользователя ещё нет в meta — создадим запись при первом /db или /start.
    """
    app = context.application
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM meta")
                users = [r["user_id"] for r in cur.fetchall()]
    except Exception as e:
        log.error("auto_today_job: meta fetch failed: %s", e)
        return

    today_local = datetime.now(TZ).date()

    for uid in users:
        try:
            last_sent = get_last_autotoday_sent(uid)
            if last_sent == today_local:
                continue
            msg = today_text(uid)
            await app.bot.send_message(chat_id=uid, text=msg)
            set_last_autotoday_sent(uid, today_local)
        except Exception as e:
            log.error("auto_today_job: send failed for %s: %s", uid, e)


async def ensure_user_meta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Хелпер: гарантируем, что user есть в meta,
    чтобы авто-today знал куда слать.
    """
    user_id = update.effective_user.id
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meta(user_id, last_autotoday_sent) VALUES (%s, NULL) ON CONFLICT DO NOTHING",
                (user_id,),
            )


# --------------------
# Main
# --------------------
def main() -> None:
    ensure_schema()

    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    app = Application.builder().token(token).build()

    # Base commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("norms", norms_cmd))
    app.add_handler(CommandHandler("last_watered", last_watered_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("db", db_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # make sure meta exists on common entrypoints
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/(start|db|today|plants|norms|water|add_plant|set_norms|rename_plant|delete_plant)\b"), ensure_user_meta))

    # Conversations
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_plant", add_plant_entry)],
        states={ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plant_name)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )
    rename_conv = ConversationHandler(
        entry_points=[CommandHandler("rename_plant", rename_entry)],
        states={
            RENAME_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_pick)],
            RENAME_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_new)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )
    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("delete_plant", delete_entry)],
        states={
            DELETE_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_pick)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )
    norms_conv = ConversationHandler(
        entry_points=[CommandHandler("set_norms", set_norms_entry)],
        states={NORMS_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norms_apply)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )
    water_conv = ConversationHandler(
        entry_points=[CommandHandler("water", water_entry)],
        states={WATER_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_pick)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    app.add_handler(add_conv)
    app.add_handler(rename_conv)
    app.add_handler(delete_conv)
    app.add_handler(norms_conv)
    app.add_handler(water_conv)

    # Auto-today schedule (11:00 local)
    app.job_queue.run_daily(
        auto_today_job,
        time=datetime.now(TZ).replace(hour=AUTO_TODAY_HOUR, minute=AUTO_TODAY_MINUTE, second=0, microsecond=0).timetz(),
        name="auto_today_11_ist",
    )

    # Webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=URL_PATH,
        webhook_url=f"{base_url}/{URL_PATH}",
    )


if __name__ == "__main__":
    main()
