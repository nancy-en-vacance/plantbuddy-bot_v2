import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Я живой ✅")

def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))

print("BOOT OK")
print("BASE_URL =", base_url)
print("WEBHOOK_URL =", f"{base_url}/{url_path}")
print("PORT =", port)
    
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=f"{base_url}/{url_path}",
    )

if __name__ == "__main__":
    main()
