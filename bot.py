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
    rename_plant,
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
        lines.append("Просрочено:")
        for name, days in overdue:
            lines.append(f"— {name} ({days} дн.)")
    if today_list:
        lines.append("Сегодня:")
        for name in today_list:
            lines.append(f"— {name}")
    if not lines:
        lines.append("Сегодня поливать ничего не нужно 🌿")
    return "\n".join(lines)


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "PlantBuddy 🌱\n\n"
        "Команды:\n"
        "/add_plant — добавить растение\n"
        "/plants — список растений\n"
        "/rename_plant — переименовать растение\n"
        "/set_norms — задать норму\n"
        "/norms — показать нормы\n"
        "/today — что поливать сегодня\n"
        "/water — отметить полив\n"
        "/db — проверка базы"
    )


async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cnt = db_check(update.effective_user.id)
    await update.message.reply_text(f"DB OK ✅ plants for you: {cnt}")


async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Список пуст.")
    else:
        await update.message.reply_text(format_plants(rows))


async def cmd_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_norms(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Нормы не заданы.")
    else:
        await update.message.reply_text(format_norms(rows))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = compute_today(update.effective_user.id, date.today())
    await update.message.reply_text(format_today(res))


async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["await_water"] = True
    await update.message.reply_text("Введи номера растений через запятую (например: 1,3)")


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Список пуст. Сначала добавь растения через /add_plant.")
        return

    context.user_data.clear()
    context.user_data["await_rename"] = True

    await update.message.reply_text(
        "Ок, что переименовать?\n\n"
        f"{format_plants(rows)}\n\n"
        "Введи так:\n"
        "номер новое_имя\n"
        "Например:\n"
        "2 Спатифиллум большой"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- rename flow ---
    if context.user_data.get("await_rename"):
        text = (update.message.text or "").strip()
        if not text:
            await update.message.reply_text("Пусто. Введи: номер новое_имя (например: 2 Спатифиллум большой)")
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            await update.message.reply_text("Формат такой: номер новое_имя (например: 2 Спатифиллум большой)")
            return

        idx = int(parts[0]) - 1
        new_name = parts[1].strip()

        rows = list_plants(update.effective_user.id)
        if not (0 <= idx < len(rows)):
            await update.message.reply_text("Нет такого номера. Проверь /plants и попробуй ещё раз.")
            return

        plant_id = rows[idx][0]
        ok = rename_plant(update.effective_user.id, plant_id, new_name)
        if ok:
            await update.message.reply_text("Переименовано ✅")
        else:
            await update.message.reply_text("Не получилось переименовать (возможно, такое имя уже есть).")

        context.user_data.clear()
        return

    # --- water flow ---
    if context.user_data.get("await_water"):
        nums = (update.message.text or "").replace(" ", "").split(",")
        rows = list_plants(update.effective_user.id)
        ids = []
        for n in nums:
            if n.isdigit():
                idx = int(n) - 1
                if 0 <= idx < len(rows):
                    ids.append(rows[idx][0])
        if ids:
            log_water_many(update.effective_user.id, ids, datetime.now(TZ))
            await update.message.reply_text("Полив отмечен ✅")
        else:
            await update.message.reply_text("Не удалось распознать номера.")
        context.user_data.clear()
        return


# ---------- auto today ----------
async def auto_today_loop(app: Application):
    await app.bot.wait_until_ready()
    while True:
        now = datetime.now(TZ)
        target = now.replace(hour=AUTO_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        # single-user bot: пока отключено (нет стабильного источника chat_id)
        try:
            pass
        finally:
            await asyncio.sleep(24 * 60 * 60)


async def post_init(app: Application):
    asyncio.create_task(auto_today_loop(app))


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
    app.add_handler(CommandHandler("plants", cmd_plants))
    app.add_handler(CommandHandler("rename_plant", cmd_rename))
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
