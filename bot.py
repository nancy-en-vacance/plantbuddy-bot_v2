import os
from datetime import datetime, timezone, date
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)

from storage import init_db, list_plants, set_last_watered_bulk

INIT_LAST_INPUT = 1


def _format_plants(plants):
    return "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(plants)])


def _parse_date(text: str) -> datetime | None:
    if text.lower() == "today":
        return datetime.now(timezone.utc)
    try:
        d = date.fromisoformat(text)
        return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------- /init_last ----------------
async def init_last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)

    if not plants:
        await update.message.reply_text("Список растений пуст.")
        return ConversationHandler.END

    context.user_data["init_last_plants"] = plants

    await update.message.reply_text(
        "Введи даты последнего полива в формате:\n"
        "номер=дата\n\n"
        "Пример:\n"
        "1=2026-01-10\n"
        "2=today\n"
        "4=2026-01-08\n\n"
        "Текущий список:\n"
        + _format_plants(plants)
    )
    return INIT_LAST_INPUT


async def init_last_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("init_last_plants", [])
    text = update.message.text.strip()

    updates = {}
    errors = []

    for line in text.splitlines():
        if "=" not in line:
            errors.append(line)
            continue

        left, right = line.split("=", 1)
        if not left.isdigit():
            errors.append(line)
            continue

        idx = int(left) - 1
        if idx < 0 or idx >= len(plants):
            errors.append(line)
            continue

        dt = _parse_date(right.strip())
        if not dt:
            errors.append(line)
            continue

        plant_id = plants[idx][0]
        updates[plant_id] = dt

    if not updates:
        await update.message.reply_text(
            "Не удалось распознать ни одной строки 😕\n"
            "Формат: 1=2026-01-10 или 2=today"
        )
        return INIT_LAST_INPUT

    applied = set_last_watered_bulk(user_id, updates)

    await update.message.reply_text(
        f"Инициализация завершена ✅\n"
        f"Обновлено растений: {len(applied)}\n"
        f"Можно продолжать 👉 /water или /today"
    )
    return ConversationHandler.END


# ---------------- main ----------------
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

    init_last_conv = ConversationHandler(
        entry_points=[CommandHandler("init_last", init_last_cmd)],
        states={
            INIT_LAST_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, init_last_input)
            ]
        },
        fallbacks=[],
    )

    app.add_handler(init_last_conv)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
