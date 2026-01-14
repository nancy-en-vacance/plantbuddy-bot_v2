import os
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

from storage import (
    init_db, add_plant, list_plants, set_norm, get_norms,
    log_water, set_last_watered_bulk,
    compute_today, get_last_sent, set_last_sent, db_check
)

IST = timedelta(hours=5, minutes=30)


def local_today():
    return (datetime.now(timezone.utc) + IST).date()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я живой 🌱\n/start — справка")


async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plants = list_plants(update.effective_user.id)
    if not plants:
        await update.message.reply_text("Список пуст. /add_plant")
        return
    await update.message.reply_text(
        "Твои растения:\n" +
        "\n".join(f"{i+1}. {name}" for i, (_, name) in enumerate(plants))
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    overdue, today_list, unknown = compute_today(user_id, local_today())

    lines = []

    if overdue:
        lines.append("🟥 Просрочено:")
        for name, days in overdue:
            lines.append(f"• {name} ({days} дн.)")

    if today_list:
        lines.append("\n🟨 Сегодня:")
        for name in today_list:
            lines.append(f"• {name}")

    if not overdue and not today_list:
        lines.append("Сегодня полив не нужен ✅")

    await update.message.reply_text("\n".join(lines))


async def autotoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = local_today()

    if get_last_sent(user_id) == today:
        await update.message.reply_text("Уже отправляла сегодня ✅")
        return

    overdue, today_list, _ = compute_today(user_id, today)
    if not overdue and not today_list:
        set_last_sent(user_id, today)
        return

    await today_cmd(update, context)
    set_last_sent(user_id, today)


def main():
    init_db()

    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("autotoday", autotoday))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path="webhook",
        webhook_url=f"{os.environ['BASE_URL']}/webhook"
    )


if __name__ == "__main__":
    main()
