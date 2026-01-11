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

from storage import (
    init_db,
    add_plant,
    list_plants,
    rename_plant,
    archive_plant,
    count_plants,
    db_fingerprint,
)

# ------------------ States ------------------
ADD_ASK_NAME = 1

REN_PICK = 10
REN_NEW_NAME = 11

DEL_PICK = 20


def _format_plants(rows):
    return "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(rows)])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой ✅\n\nКоманды:\n"
        "/add_plant — добавить растение\n"
        "/plants — показать список растений\n"
        "/rename_plant — переименовать растение\n"
        "/delete_plant — удалить (архивировать) растение\n"
        "/db — диагностика базы\n"
        "/cancel — отмена"
    )


# ------------------ /db (diagnostic) ------------------
async def db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        n = count_plants(user_id)
        fp = db_fingerprint()
        await update.message.reply_text(f"DB OK ✅ plants for you: {n}\nDB: {fp}")
    except Exception as e:
        await update.message.reply_text(f"DB ERROR ❌ {type(e).__name__}: {e}")


# ------------------ /plants ------------------
async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = list_plants(user_id, active_only=True)

    if not rows:
        await update.message.reply_text("Список пуст. Добавь первое растение: /add_plant")
        return

    await update.message.reply_text("Твои растения:\n" + _format_plants(rows))


# ------------------ /add_plant ------------------
async def add_plant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Как назовём растение? (например: Monstera)")
    return ADD_ASK_NAME


async def add_plant_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Название пустое. Напиши имя растения 🙂")
        return ADD_ASK_NAME

    user_id = update.effective_user.id
    ok = add_plant(user_id, name)
    if not ok:
        await update.message.reply_text(f"«{name}» уже есть. Хочешь другое имя?")
        return ADD_ASK_NAME

    await update.message.reply_text(f"Добавлено 🌱: {name}\n\nПосмотреть список: /plants")
    return ConversationHandler.END


# ------------------ /rename_plant ------------------
async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    rows = list_plants(user_id, active_only=True)

    if not rows:
        await update.message.reply_text("Список пуст. Добавь растение: /add_plant")
        return ConversationHandler.END

    context.user_data["rename_rows"] = rows
    await update.message.reply_text(
        "Что переименовать? Ответь номером:\n" + _format_plants(rows)
    )
    return REN_PICK


async def rename_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Нужен номер из списка (например: 2).")
        return REN_PICK

    idx = int(text) - 1
    rows = context.user_data.get("rename_rows") or []
    if idx < 0 or idx >= len(rows):
        await update.message.reply_text("Номер вне диапазона. Выбери из списка.")
        return REN_PICK

    plant_id, old_name = rows[idx]
    context.user_data["rename_plant_id"] = plant_id
    context.user_data["rename_old_name"] = old_name

    await update.message.reply_text(f"Ок. Новое имя для «{old_name}»?")
    return REN_NEW_NAME


async def rename_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = (update.message.text or "").strip()
    if not new_name:
        await update.message.reply_text("Имя пустое. Напиши новое имя 🙂")
        return REN_NEW_NAME

    user_id = update.effective_user.id
    plant_id = int(context.user_data.get("rename_plant_id"))
    old_name = context.user_data.get("rename_old_name")

    ok = rename_plant(user_id, plant_id, new_name)
    if not ok:
        await update.message.reply_text(
            "Не получилось переименовать. Возможно такое имя уже есть или растение не найдено.\n"
            "Попробуй другое имя или начни заново: /rename_plant"
        )
        return ConversationHandler.END

    await update.message.reply_text(f"Готово ✅ «{old_name}» → «{new_name}»\n\n/plants")
    return ConversationHandler.END


# ------------------ /delete_plant (archive) ------------------
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    rows = list_plants(user_id, active_only=True)

    if not rows:
        await update.message.reply_text("Список пуст. Нечего удалять 🙂")
        return ConversationHandler.END

    context.user_data["delete_rows"] = rows
    await update.message.reply_text(
        "Что удалить (архивировать)? Ответь номером:\n" + _format_plants(rows)
    )
    return DEL_PICK


async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Нужен номер из списка (например: 3).")
        return DEL_PICK

    idx = int(text) - 1
    rows = context.user_data.get("delete_rows") or []
    if idx < 0 or idx >= len(rows):
        await update.message.reply_text("Номер вне диапазона. Выбери из списка.")
        return DEL_PICK

    plant_id, name = rows[idx]
    user_id = update.effective_user.id

    ok = archive_plant(user_id, int(plant_id))
    if not ok:
        await update.message.reply_text(
            "Не получилось удалить (архивировать). Попробуй ещё раз: /delete_plant"
        )
        return ConversationHandler.END

    await update.message.reply_text(f"Убрала в архив 🗑️: {name}\n\n/plants")
    return ConversationHandler.END


# ------------------ cancel ------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отмена.")
    return ConversationHandler.END


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].strip().rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    # init DB (creates tables in Neon)
    init_db()

    async def post_init(app: Application) -> None:
        await app.bot.set_webhook(url=webhook_url)
        print("WEBHOOK SET TO:", webhook_url)
        print("PORT:", port)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("db", db_cmd))

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_plant", add_plant_cmd)],
        states={
            ADD_ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plant_name)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(add_conv)

    rename_conv = ConversationHandler(
        entry_points=[CommandHandler("rename_plant", rename_cmd)],
        states={
            REN_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_pick)],
            REN_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_new_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(rename_conv)

    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("delete_plant", delete_cmd)],
        states={
            DEL_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_pick)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(delete_conv)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
