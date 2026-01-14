import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from storage import init_db, list_plants_today


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой 🌱\n\n"
        "Команды:\n"
        "/today — что полить сегодня\n"
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    overdue, today, upcoming, unknown = list_plants_today(user_id)

    lines = ["Сегодня 🌱\n"]

    if overdue:
        lines.append("🟥 Просрочено:")
        for name, days in overdue:
            lines.append(f"• {name} (на {days} дн.)")
        lines.append("")

    if today:
        lines.append("🟨 Сегодня:")
        for name in today:
            lines.append(f"• {name}")
        lines.append("")

    if upcoming:
        lines.append("🟩 Пока не нужно:")
        for name, days in upcoming:
            lines.append(f"• {name} (через {days} дн.)")
        lines.append("")

    if unknown:
        lines.append("⚪ Нет данных:")
        for name in unknown:
            lines.append(f"• {name}")

    await update.message.reply_text("\n".join(lines))


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    init_db()

    async def post_init(app: Application):
        await app.bot.set_webhook(webhook_url)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
