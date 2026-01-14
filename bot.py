# bot.py — syntax-safe minimal version
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from storage import (
    ensure_schema,
    list_plants,
    list_norms,
    record_watering,
    last_watered,
    plants_today,
    db_ok,
    save_chat_id,
    get_chat_id,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Kolkata")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    text = (
        "PlantBuddy 🌱 готов\n"
        "Команды:\n"
        "/plants\n"
        "/norms\n"
        "/today\n"
        "/water\n"
        "/last_watered\n"
        "/db"
    )
    await update.message.reply_text(text)

async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, cnt = db_ok(update.effective_user.id)
    await update.message.reply_text(
        "DB OK ✅ plants for you: " + str(cnt) if ok else "DB ERROR ❌"
    )

async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = list_plants(update.effective_user.id)
    await update.message.reply_text(txt or "Список пуст.")

async def cmd_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = list_norms(update.effective_user.id)
    await update.message.reply_text(txt or "Нормы не заданы.")

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(plants_today(update.effective_user.id, TZ))

async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_water"] = True
    await update.message.reply_text("Введи номера растений через запятую (1,3,5)")

async def handle_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_water"):
        return
    result = record_watering(update.effective_user.id, update.message.text, TZ)
    context.user_data.clear()
    await update.message.reply_text(result)

async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = last_watered(update.effective_user.id, TZ)
    await update.message.reply_text(txt or "Нет данных.")

async def auto_today(app: Application):
    await app.bot.wait_until_ready()
    chat_id = get_chat_id()
    if not chat_id:
        return
    while True:
        now = datetime.now(TZ)
        target = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await app.bot.send_message(chat_id=chat_id, text=plants_today(None, TZ))
        await asyncio.sleep(86400)

async def post_init(app: Application):
    asyncio.create_task(auto_today(app))

def main():
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plants", cmd_plants))
    app.add_handler(CommandHandler("norms", cmd_norms))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("water", cmd_water))
    app.add_handler(CommandHandler("last_watered", cmd_last))
    app.add_handler(CommandHandler("db", cmd_db))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_water))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=BASE_URL + "/webhook",
    )

if __name__ == "__main__":
    main()
