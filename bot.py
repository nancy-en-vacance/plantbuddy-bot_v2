import os
import re
from datetime import datetime, timezone, timedelta, time as dtime
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

TZ = ZoneInfo("Asia/Kolkata")
URL_PATH = "webhook"

AUTO_TODAY_AT = dtime(hour=11, minute=0, tzinfo=TZ)

ADD_NAME = 10
RENAME_PICK = 20
RENAME_NEW = 21
DELETE_PICK = 30
DELETE_CONFIRM = 31
NORMS_SET = 40
WATER_PICK = 50


def _db_url() -> str:
    return os.environ["DATABASE_URL"]


def _connect():
    url = _db_url()
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg.connect(url, row_factory=dict_row)


def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name=%s
          AND column_name=%s
        """,
        (table, col),
    )
    return cur.fetchone() is not None


def ensure_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
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

            # поддерживаем старую схему, где был active вместо archived
            has_active = _col_exists(cur, "plants", "active")
            has_archived = _col_exists(cur, "plants", "archived")

            if has_active and not has_archived:
                cur.execute("ALTER TABLE plants ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;")
                # считаем active=FALSE как archived=TRUE
                cur.execute("UPDATE plants SET archived = NOT active;")

            if not has_archived:
                cur.execute("ALTER TABLE plants ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;")

            if not _col_exists(cur, "plants", "last_watered_at"):
                cur.execute("ALTER TABLE plants ADD COLUMN last_watered_at TIMESTAMPTZ;")

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS plants_user_name_uq
                ON plants(user_id, lower(name))
                WHERE archived = FALSE;
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS norms (
                    plant_id BIGINT PRIMARY KEY REFERENCES plants(id) ON DELETE CASCADE,
                    interval_days INT NOT NULL CHECK (interval_days > 0)
                );
                """
            )

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

            # кто получит авто-today (user_id -> chat_id)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    user_id BIGINT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    last_autotoday_sent DATE
                );
                """
            )


def _upsert_chat(user_id: int, chat_id: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO meta(user_id, chat_id)
                VALUES (%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
                """,
                (user_id, chat_id),
            )


def _get_all_recipients():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, chat_id, last_autotoday_sent FROM meta")
            return cur.fetchall()


def _mark_autotoday_sent(user_id: int, day_local) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE meta SET last_autotoday_sent=%s WHERE user_id=%s",
                (day_local, user_id),
            )


def _get_plants(user_id: int):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, last_watered_at
                FROM plants
                WHERE user_id=%s AND archived=FALSE
                ORDER BY id
                """,
                (user_id,),
            )
            return cur.fetchall()


def _add_plant(user_id: int, name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "Имя пустое."
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, archived
                FROM plants
                WHERE user_id=%s AND lower(name)=lower(%s)
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, name),
            )
            row = cur.fetchone()
            if row and row["archived"]:
                cur.execute("UPDATE plants SET archived=FALSE WHERE id=%s", (row["id"],))
                return True, "Разархивировала ✅"
            try:
                cur.execute(
                    "INSERT INTO plants(user_id, name, archived) VALUES (%s,%s,FALSE)",
                    (user_id, name),
                )
                return True, "Добавлено 🌱"
            except Exception:
                return False, f"У тебя уже есть «{name}». Хочешь другое имя?"


def _rename_plant(user_id: int, plant_id: int, new_name: str) -> tuple[bool, str]:
    new_name = new_name.strip()
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
                return False, f"Имя «{new_name}» уже занято."


has_archived = _col_exists(cur, "plants", "archived")
has_active = _col_exists(cur, "plants", "active")

if has_active and not has_archived:
    cur.execute(
        "ALTER TABLE plants ADD COLUMN archived BOOLEAN NOT NULL DEFAULT FALSE;"
    )
    cur.execute("UPDATE plants SET archived = NOT active;")

# НИЧЕГО не делаем, если archived уже есть


def _set_norm(plant_id: int, days: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO norms(plant_id, interval_days)
                VALUES (%s,%s)
                ON CONFLICT (plant_id) DO UPDATE
                SET interval_days = EXCLUDED.interval_days
                """,
                (plant_id, days),
            )


def _get_norms_map(user_id: int) -> dict[int, int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.plant_id, n.interval_days
                FROM norms n
                JOIN plants p ON p.id=n.plant_id
                WHERE p.user_id=%s AND p.archived=FALSE
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return {r["plant_id"]: r["interval_days"] for r in rows}


def _log_water(user_id: int, plant_ids: list[int], when_utc: datetime) -> int:
    with _connect() as conn:
        with conn.cursor() as cur:
            for pid in plant_ids:
                cur.execute(
                    "INSERT INTO water_log(user_id, plant_id, watered_at) VALUES (%s,%s,%s)",
                    (user_id, pid, when_utc),
                )
                cur.execute(
                    "UPDATE plants SET last_watered_at=%s WHERE id=%s AND user_id=%s",
                    (when_utc, pid, user_id),
                )
    return len(plant_ids)


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def _plants_text(plants) -> str:
    if not plants:
        return "Список пуст. Добавь первое растение: /add_plant"
    lines = ["Твои растения:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']}")
    return "\n".join(lines)


def _norms_text(user_id: int) -> str:
    plants = _get_plants(user_id)
    norms = _get_norms_map(user_id)
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


def _last_watered_text(user_id: int) -> str:
    plants = _get_plants(user_id)
    if not plants:
        return "Список пуст. Добавь растение: /add_plant"
    lines = ["Последний полив:"]
    for i, p in enumerate(plants, 1):
        lines.append(f"{i}. {p['name']} — {_fmt_dt(p['last_watered_at'])}")
    return "\n".join(lines)


def _today_text(user_id: int) -> str:
    plants = _get_plants(user_id)
    norms = _get_norms_map(user_id)

    now_local = datetime.now(TZ)
    today_local = now_local.date()

    overdue = []
    due_today = []

    for p in plants:
        interval = norms.get(p["id"])
        if not interval:
            continue

        last = p["last_watered_at"]
        if last is None:
            overdue.append((p["name"], None, interval))
            continue

        last_date = last.astimezone(TZ).date()
        next_due = last_date + timedelta(days=interval)

        if next_due < today_local:
            days_over = (today_local - next_due).days
            overdue.append((p["name"], days_over, interval))
        elif next_due == today_local:
            due_today.append(p["name"])

    overdue.sort(key=lambda x: (9999 if x[1] is None else x[1]), reverse=True)
    due_today.sort(key=lambda x: x.lower())

    if not overdue and not due_today:
        return "Сегодня полив не нужен ✅"

    parts = []
    if overdue:
        parts.append("Просрочено:")
        for name, days_over, interval in overdue:
            if days_over is None:
                parts.append(f"• {name} — нет даты последнего полива (норма {interval} дн.)")
            elif days_over == 1:
                parts.append(f"• Вчера нужно было полить: {name}")
            else:
                parts.append(f"• {days_over} дн. назад нужно было полить: {name}")

    if due_today:
        parts.append("")
        parts.append("Сегодня:")
        for name in due_today:
            parts.append(f"• {name}")

    return "\n".join(parts)


def _parse_int_list(text: str) -> list[int]:
    nums = re.findall(r"\d+", text or "")
    return [int(x) for x in nums]


def _parse_norm_pairs(text: str) -> list[tuple[int, int]]:
    text = (text or "").replace(":", "=")
    chunks = re.split(r"[,\n ]+", text.strip())
    out = []
    for c in chunks:
        if "=" not in c:
            continue
        a, b = c.split("=", 1)
        if a.strip().isdigit() and b.strip().isdigit():
            out.append((int(a.strip()), int(b.strip())))
    return out


# =========================
# HANDLERS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    _upsert_chat(uid, chat_id)

    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant — добавить растение\n"
        "/plants — список\n"
        "/rename_plant — переименовать\n"
        "/delete_plant — удалить (в архив)\n"
        "/set_norms — задать норму полива (раз в N дней)\n"
        "/norms — показать нормы\n"
        "/water — отметить полив (можно несколько)\n"
        "/last_watered — показать последний полив\n"
        "/today — что полить сегодня\n"
        "/db — диагностика базы\n"
        "/cancel — отмена"
    )


async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(_plants_text(_get_plants(uid)))


async def norms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(_norms_text(uid))


async def last_watered_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(_last_watered_text(uid))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(_today_text(uid))


async def db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    try:
        ensure_schema()
        plants = _get_plants(uid)
        await update.message.reply_text(f"DB OK ✅ plants for you: {len(plants)}")
    except Exception as e:
        await update.message.reply_text(f"DB FAIL ❌ {type(e).__name__}: {e}")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("tmp", None)
    await update.message.reply_text("Ок, отмена ✅")
    return ConversationHandler.END


# /add_plant
async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    ok, msg = _add_plant(uid, update.message.text or "")
    await update.message.reply_text(msg + ("\n\nПосмотреть список: /plants" if ok else ""))
    return ConversationHandler.END if ok else ADD_NAME


# /rename_plant
async def rename_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    plants = _get_plants(uid)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text("Что переименовать? Введи номер:\n\n" + _plants_text(plants))
    return RENAME_PICK


async def rename_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Нужен номер. Пример: 3")
        return RENAME_PICK
    num = int(txt)
    plants = context.user_data.get("tmp", {}).get("plants") or []
    if not (1 <= num <= len(plants)):
        await update.message.reply_text("Неверный номер.")
        return RENAME_PICK
    context.user_data["tmp"]["pick"] = num
    await update.message.reply_text("Ок. Новое имя?")
    return RENAME_NEW


async def rename_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    tmp = context.user_data.get("tmp") or {}
    plants = tmp.get("plants") or []
    num = tmp.get("pick")
    plant = plants[num - 1]
    ok, msg = _rename_plant(uid, plant["id"], update.message.text or "")
    await update.message.reply_text(msg)
    context.user_data.pop("tmp", None)
    return ConversationHandler.END


# /delete_plant
async def delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    plants = _get_plants(uid)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text("Что удалить (в архив)? Введи номер:\n\n" + _plants_text(plants))
    return DELETE_PICK


async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Нужен номер.")
        return DELETE_PICK
    num = int(txt)
    plants = context.user_data.get("tmp", {}).get("plants") or []
    if not (1 <= num <= len(plants)):
        await update.message.reply_text("Неверный номер.")
        return DELETE_PICK
    context.user_data["tmp"]["pick"] = num
    plant = plants[num - 1]
    await update.message.reply_text(f"Точно архивировать «{plant['name']}»?\nОтветь: yes / no")
    return DELETE_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    ans = (update.message.text or "").strip().lower()
    if ans not in ("yes", "no"):
        await update.message.reply_text("Ответь: yes или no")
        return DELETE_CONFIRM
    if ans == "no":
        await update.message.reply_text("Ок ✅")
        context.user_data.pop("tmp", None)
        return ConversationHandler.END

    tmp = context.user_data.get("tmp") or {}
    plants = tmp.get("plants") or []
    num = tmp.get("pick")
    plant = plants[num - 1]
    _archive_plant(uid, plant["id"])
    await update.message.reply_text("Готово ✅ (в архив)")
    context.user_data.pop("tmp", None)
    return ConversationHandler.END


# /set_norms
async def norms_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    plants = _get_plants(uid)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Задай нормы в формате номер=дни.\nПример: 1=7, 3=4\n\n" + _plants_text(plants)
    )
    return NORMS_SET


async def norms_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plants = context.user_data.get("tmp", {}).get("plants") or []
    pairs = _parse_norm_pairs(update.message.text or "")
    if not pairs:
        await update.message.reply_text("Не вижу пар. Пример: 1=7, 3=4")
        return NORMS_SET

    updated = 0
    for num, days in pairs:
        if 1 <= num <= len(plants) and days > 0:
            _set_norm(plants[num - 1]["id"], days)
            updated += 1

    context.user_data.pop("tmp", None)
    await update.message.reply_text(f"Сохранила ✅ Обновлено: {updated}\n\nПроверить: /norms")
    return ConversationHandler.END


# /water
async def water_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    plants = _get_plants(uid)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END
    context.user_data["tmp"] = {"plants": plants}
    await update.message.reply_text(
        "Что ты полила? Введи номера через запятую:\n\n"
        + _plants_text(plants)
        + "\n\nПример: 1,3,5"
    )
    return WATER_PICK


async def water_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    plants = context.user_data.get("tmp", {}).get("plants") or []
    nums = _parse_int_list(update.message.text or "")
    nums = sorted(set([n for n in nums if 1 <= n <= len(plants)]))
    if not nums:
        await update.message.reply_text("Не вижу корректных номеров. Пример: 2,4,5")
        return WATER_PICK

    plant_ids = [plants[n - 1]["id"] for n in nums]
    names = [plants[n - 1]["name"] for n in nums]
    _log_water(uid, plant_ids, datetime.now(tz=timezone.utc))

    context.user_data.pop("tmp", None)
    await update.message.reply_text(
        "Зафиксировала полив ✅\n" + "\n".join([f"• {n}" for n in names]) + f"\n\nОбновлено: {len(names)}"
    )
    return ConversationHandler.END


# ===== auto-today job =====
async def auto_today_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_local = datetime.now(TZ)
    day_local = now_local.date()

    for r in _get_all_recipients():
        user_id = int(r["user_id"])
        chat_id = int(r["chat_id"])
        last_sent = r["last_autotoday_sent"]

        # не шлём второй раз в тот же день
        if last_sent == day_local:
            continue

        text = _today_text(user_id)
        await context.bot.send_message(chat_id=chat_id, text=text)
        _mark_autotoday_sent(user_id, day_local)


def main() -> None:
    ensure_schema()

    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    app = Application.builder().token(token).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("norms", norms_cmd))
    app.add_handler(CommandHandler("last_watered", last_watered_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("db", db_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # conversations
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add_plant", add_entry)],
            states={ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)]},
            fallbacks=[CommandHandler("cancel", cancel_cmd)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("rename_plant", rename_entry)],
            states={
                RENAME_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_pick)],
                RENAME_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_new)],
            },
            fallbacks=[CommandHandler("cancel", cancel_cmd)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("delete_plant", delete_entry)],
            states={
                DELETE_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_pick)],
                DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
            },
            fallbacks=[CommandHandler("cancel", cancel_cmd)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("set_norms", norms_entry)],
            states={NORMS_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, norms_set)]},
            fallbacks=[CommandHandler("cancel", cancel_cmd)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("water", water_entry)],
            states={WATER_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_pick)]},
            fallbacks=[CommandHandler("cancel", cancel_cmd)],
        )
    )

    # auto /today at 11:00 local (Asia/Kolkata)
    # будет работать только если установлен [job-queue] (мы его добавили)
    if app.job_queue is None:
        print("WARN: JobQueue is None (missing job-queue extra). Auto-today disabled.")
    else:
        app.job_queue.run_daily(auto_today_job, time=AUTO_TODAY_AT, name="auto_today_11_ist")

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=URL_PATH,
        webhook_url=f"{base_url}/{URL_PATH}",
    )


if __name__ == "__main__":
    main()
