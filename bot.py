import os
import re
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

from storage import (
    init_db, list_plants,
    compute_today, get_last_sent, set_last_sent,
    log_water_many
)

IST = timedelta(hours=5, minutes=30)

WATER_INPUT = 100


def local_today():
    return (datetime.now(timezone.utc) + IST).date()


def _format_today(overdue, today_list, unknown) -> str:
    lines = []

    if overdue:
        yesterday = [name for name, days in overdue if days == 1]
        older = [(name, days) for name, days in overdue if days != 1]

        if yesterday:
            lines.append("🟥 Вчера нужно было полить:")
            for name in yesterday:
                lines.append(f"• {name}")
            lines.append("")

        if older:
            lines.append("🟥 Просрочено:")
            for name, days in older:
                lines.append(f"• {name} (на {days} дн.)")
            lines.append("")

    if today_list:
        lines.append("🟨 Сегодня:")
        for name in today_list:
            lines.append(f"• {name}")
        lines.append("")

    if not overdue and not today_list:
        lines.append("Сегодня полив не нужен ✅")

    if unknown:
        lines.append("\n⚪ Нет данных (норма или последний полив не заданы):")
        for name in unknown:
            lines.append(f"• {name}")

    return "\n".join(lines).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я живой 🌱\n\n"
        "Команды:\n"
        "/today — что полить сегодня\n"
        "/water — отметить полив (можно несколько)\n"
        "/autotoday — авто-сводка (для внешнего cron)\n"
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    overdue, today_list, unknown = compute_today(user_id, local_today())
    await update.message.reply_text(_format_today(overdue, today_list, unknown))


async def autotoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = local_today()

    if get_last_sent(user_id) == today:
        await update.message.reply_text("Уже отправляла сегодня ✅")
        return

    overdue, today_list, unknown = compute_today(user_id, today)
    if not overdue and not today_list:
        set_last_sent(user_id, today)
        return

    await update.message.reply_text("⏰ Напоминание\n\n" + _format_today(overdue, today_list, unknown))
    set_last_sent(user_id, today)


# -------- /water (множественный выбор) --------
def _parse_multi_numbers(text: str):
    # принимает "1,3,5" / "1, 3, 5" / "1 3 5"
    parts = re.split(r"[,\s]+", text.strip())
    nums = []
    for p in parts:
        if not p:
            continue
        if not p.isdigit():
            return None
        nums.append(int(p))
    return nums


async def water_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь растения сначала.")
        return ConversationHandler.END

    context.user_data["plants_for_water"] = plants
    msg = "Что ты полила? Введи номера через запятую:\n\n" + "\n".join(
        f"{i+1}. {name}" for i, (_, name) in enumerate(plants)
    ) + "\n\nПример: 1,3,5"
    await update.message.reply_text(msg)
    return WATER_INPUT


async def water_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("plants_for_water", [])
    nums = _parse_multi_numbers(update.message.text or "")
    if not nums:
        await update.message.reply_text("Не поняла. Введи номера через запятую. Пример: 1,3,5")
        return WATER_INPUT

    # уникальные, в порядке ввода
    seen = set()
    cleaned = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            cleaned.append(n)

    # валидируем
    bad = [n for n in cleaned if n < 1 or n > len(plants)]
    if bad:
        await update.message.reply_text(f"Есть неверные номера: {', '.join(map(str, bad))}. Попробуй ещё раз.")
        return WATER_INPUT

    # переводим номера в plant_id
    selected = [plants[n-1] for n in cleaned]  # (id, name)
    plant_ids = [int(pid) for pid, _ in selected]
    when = datetime.now(timezone.utc)

    updated = log_water_many(user_id, plant_ids, when)

    names = [name for _, name in selected]
    await update.message.reply_text(
        "Зафиксировала полив ✅\n" + "\n".join(f"• {n}" for n in names) +
        f"\n\nОбновлено: {updated}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отмена.")
    return ConversationHandler.END


def main():
    init_db()

    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].strip().rstrip("/")
    port = int(os.environ.get("PORT", 10000))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    async def post_init(app: Application):
        await app.bot.set_webhook(url=webhook_url)
        print("WEBHOOK SET TO:", webhook_url)
        print("PORT:", port)

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("autotoday", autotoday))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("water", water_cmd)],
        states={WATER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url
    )


if __name__ == "__main__":
    main()
