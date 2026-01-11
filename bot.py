import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from storage import init_db, get_conn


# -------- /add_plant conversation --------
ASK_NAME = 1


def add_plant(user_id: int, name: str) -> bool:
    """Returns True if inserted, False if already exists or any insert error."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO plants (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
        return True
    except Exception:
        return False


def list_plants(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM plants WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant — добавить растение\n"
        "/plants — показать список растений\n"
        "/cancel — отменить добавление"
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
    ok = add_plant(user_id, name)

    if not ok:
        await update.message.reply_text(f"«{name}» уже есть. Хочешь другое имя?")
        return ASK_NAME

    await update.message.reply_text(f"Добавлено 🌱: {name}\n\nПосмотреть список: /plants")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отмена.")
    return ConversationHandler.END


async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    plants = list_plants(user_id)

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

    # init sqlite
    init_db()

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
