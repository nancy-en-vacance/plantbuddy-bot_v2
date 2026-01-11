import os
from typing import Dict, List

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -------- In-memory storage (пока без БД) --------
PLANTS: Dict[int, List[str]] = {}  # user_id -> [plant names]


def _get_user_plants(user_id: int) -> List[str]:
    return PLANTS.setdefault(user_id, [])


# -------- /add_plant conversation --------
ASK_NAME = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant — добавить растение\n"
        "/plants — показать список растений"
    )


async def add_plant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ASK_NAME


async def add_plant_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Название пустое. Напиши имя растения 🙂")
        return ASK_NAME

    user_id = update.effective_user.id
    plants = _get_user_plants(user_id)

    # простая защита от дублей по точному совпадению
    if name in plants:
        await update.message.reply_text(f"У тебя уже есть «{name}». Хочешь другое имя?")
        return ASK_NAME

    plants.append(name)
    await update.message.reply_text(f"Добавлено 🌱: {name}\n\nПосмотреть список: /plants")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отмена.")
    return ConversationHandler.END


async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    plants = _get_user_plants(user_id)

    if not plants:
        await update.message.reply_text("Список пуст. Добавь первое растение: /add_plant")
        return

    lines = "\n".join([f"{i+1}. {p}" for i, p in enumerate(plants)])
    await update.message.reply_text("Твои растения:\n" + lines)


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].strip().rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    async def post_init(app: Application) -> None:
        await app.bot.set_webhook(url=webhook_url)
        print("WEBHOOK SET TO:", webhook_url)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plants", plants_cmd))

    add_plant_conv = ConversationHandler(
        entry_points=[CommandHandler("add_plant", add_plant_cmd)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plant_name)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(add_plant_conv)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
