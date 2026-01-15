# bot.py — FINAL, aligned with existing storage.py (no guesses)
import os
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === storage API (EXACT) ===
from storage import (
    init_db,
    add_plant,
    list_plants,
    set_norm,
    get_norms,
    log_water_many,
    compute_today,
    get_last_sent,
    set_last_sent,
    db_check,
)

# === config ===
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Kolkata")  # UTC+5:30
AUTO_HOUR = 11


# ---------- helpers ----------
def format_plants(rows):
    return "\n".join(f"{i+1}. {name}" for i, (_, name) in enumerate(rows))


def format_norms(rows):
    return "\n".join(f"{name} — раз в {days} дн." for name, days in rows)


def format_today(res):
    overdue, today_list, unknown = res
    lines = []
    if overdue:
        lines.append("⚠️Просрочено:")
        for name, days in overdue:
            lines.append(f"— {name} ({days} дн.)")
    if today_list:
        lines.append("⏰Сегодня:")
        for name in today_list:
            lines.append(f"— {name}")
    if unknown:
        lines.append("ℹ️Нужно настроить:")
        for name in unknown:
            lines.append(f"— {name}")
    if not overdue and not today_list:
        lines.append("Сегодня поливать ничего не нужно😉")
    return "\n".join(lines)


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌱 PlantBuddy\n"
        "Помню, когда поливать твои растения\n\n"
        "Команды:\n"
        "/add_plant — добавить растение\n"
        "/plants — список активных\n"
        "/set_norms — задать норму полива\n"
        "/norms — показать нормы\n"
        "/today — что поливать сегодня\n"
        "/water — отметить полив\n"
        "/cancel — отмена текущего ввода\n"
        "/db — проверка базы"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Ок, отменили✅ Что делаем дальше?🙂")


async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cnt = db_check(update.effective_user.id)
    await update.message.reply_text(f"База жива✅ У тебя растений: {cnt}")


async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Пока пусто! Добавим растение через /add_plant?")
    else:
        await update.message.reply_text("Твои растения🥰\n\n" + format_plants(rows))


async def cmd_add_plant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["await_add_plant"] = True
    await update.message.reply_text("Как назовём растение?🌱\nНапиши одним сообщением (например: Калатея)")


async def cmd_set_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Сначала добавим хотя бы одно растение👇🏻\nКоманда: /add_plant")
        return
    context.user_data.clear()
    context.user_data["await_set_norm"] = True
    await update.message.reply_text(
        "Ок, зададим норму полива 💧\n\n"
        f"{format_plants(rows)}\n\n"
        "Введи так: номер дни\n"
        "Например: 2 5 (раз в 5 дней)"
    )


async def cmd_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_norms(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Нормы пока не заданы🤔\nХочешь — сделаем через /set_norms")
    else:
        await update.message.reply_text("Твои нормы полива💧\n\n" + format_norms(rows))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = compute_today(update.effective_user.id, date.today())
    await update.message.reply_text("План на сегодня:\n\n" + format_today(res))


async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text("У тебя пока нет растений🌿\nДобавь через /add_plant")
        return
    context.user_data.clear()
    context.user_data["await_water"] = True
    await update.message.reply_text(
        "Какие растения полили? 💧\n\n"
        f"{format_plants(rows)}\n\n"
        "Напиши номера через запятую (например: 1,3)\n"
        "Если передумала — /cancel"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # --- add plant flow ---
    if context.user_data.get("await_add_plant"):
        name = text.strip()
        if not name:
            await update.message.reply_text("Хм, пустое имя🤔\nНапиши название растения, например: Фикус")
            return
        add_plant(user_id, name)
        context.user_data.clear()
        await update.message.reply_text(f"Добавила: {name} ✅\nХочешь задать норму? /set_norms")
        return

    # --- set norms flow ---
    if context.user_data.get("await_set_norm"):
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text("Я не поняла формат😅\nПример: 2 5 (номер и дни)")
            return
        idx = int(parts[0]) - 1
        days = int(parts[1])
        if days <= 0 or days > 365:
            await update.message.reply_text("Дни выглядят странно🤔\nДавай число от 1 до 365 (например: 7)")
            return
        rows = list_plants(user_id)
        if not (0 <= idx < len(rows)):
            await update.message.reply_text("Кажется, такого номера нет🤔\nПроверь список и попробуй ещё раз")
            return
        plant_id, plant_name = rows[idx]
        ok = set_norm(user_id, plant_id, days)
        context.user_data.clear()
        if ok:
            await update.message.reply_text(f"Норма для «{plant_name}» — раз в {days} дн.✅")
        else:
            await update.message.reply_text("Хм, не получилось поставить норму🤔 Попробуй ещё раз: /set_norms")
        return

    # --- water flow ---
    if context.user_data.get("await_water"):
        nums = text.replace(" ", "").split(",")
        rows = list_plants(user_id)
        ids = []
        for n in nums:
            if n.isdigit():
                idx = int(n) - 1
                if 0 <= idx < len(rows):
                    ids.append(rows[idx][0])
        if ids:
            log_water_many(user_id, ids, datetime.now(TZ))
            await update.message.reply_text("Полив отметила💧✅")
            context.user_data.clear()
        else:
            await update.message.reply_text("Я не смогла распознать номера😅\nПример: 1,3\nЕсли передумала — /cancel")
        return


# ---------- auto today ----------
async def auto_today_loop(app: Application):
    # NOTE: currently disabled; enable by scheduling from post_init
    # python-telegram-bot 20.x doesn't have bot.wait_until_ready().
    # Do a lightweight API call once; even if it fails, keep the loop alive.
    try:
        await app.bot.get_me()
    except Exception:
        pass

    while True:
        now = datetime.now(TZ)
        target = now.replace(hour=AUTO_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        await asyncio.sleep((target - now).total_seconds())

        try:
            # TODO: auto-today is intentionally disabled for now (single-user + no chat_id persistence)
            pass
        except Exception as e:
            # Don't let the background task die silently
            print(f"[auto_today_loop] error: {e!r}")


async def post_init(app: Application):
    # auto-today loop temporarily disabled (no background tasks)
    return

# ---------- main ----------
def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add_plant", cmd_add_plant))
    app.add_handler(CommandHandler("set_norms", cmd_set_norms))
    app.add_handler(CommandHandler("plants", cmd_plants))
    app.add_handler(CommandHandler("norms", cmd_norms))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("water", cmd_water))
    app.add_handler(CommandHandler("db", cmd_db))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
    )


if __name__ == "__main__":
    main()
