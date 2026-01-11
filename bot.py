import os
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Я живой ✅")


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    # принудительно ставим webhook при каждом запуске (без сюрпризов после redeploy)
    Bot(token).set_webhook(url=webhook_url)
    print("WEBHOOK SET TO:", webhook_url)
    print("PORT:", port)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
