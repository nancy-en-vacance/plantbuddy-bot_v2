import os
from datetime import datetime, timezone, timedelta, date, time

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
    set_norm,
    get_norms,
    log_water,
    set_last_watered_bulk,
    compute_due_lists,
    get_last_sent_local_date,
    set_last_sent_local_date,
    db_check,
)

# ---- настройки "жёстко" под тебя ----
IST_OFFSET = timedelta(minutes=330)          # UTC+5:30
AUTO_HOUR = 11
AUTO_MINUTE = 0

# твой чат id можно не задавать, но для авто-режима удобно:
# если задан OWNER_CHAT_ID, бот будет отправлять авто-сводку только туда
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")  # optional

INIT_LAST_INPUT = 1
SETNORM_PICK = 10
SETNORM_DAYS = 11
WATER_PICK = 20


def local_now() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def local_today() -> date:
    return local_now().date()


def local_time_now() -> time:
    return local_now().time()


def _format_today(overdue, due_today, unknown) -> str:
    lines = []

    if overdue:
        # отдельно "вчера"
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

    if due_today:
        lines.append("🟨 Сегодня:")
        for name in due_today:
            lines.append(f"• {name}")
        lines.append("")

    if not overdue and not due_today:
        lines.append("Сегодня полив не нужен ✅")

    if unknown:
        lines.append("\n⚪ Нет данных (норма или последний полив не заданы):")
        for name in unknown:
            lines.append(f"• {name}")

    return "\n".join(lines).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я живой 🌱\n\n"
        "Команды:\n"
        "/add_plant — добавить растение (после команды пришли название)\n"
        "/plants — список растений\n"
        "/set_norm — задать норму полива (выбор + дни)\n"
        "/norms — показать нормы\n"
        "/init_last — массово задать последний полив (разные даты)\n"
        "/water — отметить полив (выбор)\n"
        "/today — что полить сегодня\n"
        "/autotoday — авто-сводка (для внешнего cron)\n"
        "/db — проверка базы\n"
        "/ping — проверка связи"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong ✅")


# ---------------- /db ----------------
async def db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    ok, cnt = db_check(user_id)
    await update.message.reply_text(f"DB OK ✅ plants for you: {cnt}")


# ---------------- /plants ----------------
async def plants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь первое растение: /add_plant")
        return
    text = "Твои растения:\n" + "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(plants)])
    await update.message.reply_text(text)


# ---------------- /add_plant (2 шага: команда -> текст) ----------------
ADDPLANT_INPUT = 2

async def add_plant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок. Напиши название растения одним сообщением.")
    return ADDPLANT_INPUT


async def add_plant_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Название пустое. Напиши ещё раз.")
        return ADDPLANT_INPUT
    add_plant(user_id, name)
    await update.message.reply_text(f"Добавлено ✅: {name}")
    return ConversationHandler.END


# ---------------- /norms ----------------
async def norms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    norms = get_norms(user_id)
    if not norms:
        await update.message.reply_text("Норм пока нет. Задай через /set_norm")
        return
    lines = ["Нормы полива:"]
    for i, (name, d) in enumerate(norms, 1):
        lines.append(f"{i}. {name} — раз в {d} дн.")
    await update.message.reply_text("\n".join(lines))


# ---------------- /set_norm (выбор растения -> дни) ----------------
async def set_norm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь: /add_plant")
        return ConversationHandler.END
    context.user_data["setnorm_plants"] = plants
    msg = "Выбери номер растения:\n" + "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(plants)])
    await update.message.reply_text(msg)
    return SETNORM_PICK


async def set_norm_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plants = context.user_data.get("setnorm_plants", [])
    t = (update.message.text or "").strip()
    if not t.isdigit():
        await update.message.reply_text("Нужен номер. Например: 3")
        return SETNORM_PICK
    idx = int(t) - 1
    if idx < 0 or idx >= len(plants):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return SETNORM_PICK
    context.user_data["setnorm_plant_id"] = plants[idx][0]
    context.user_data["setnorm_plant_name"] = plants[idx][1]
    await update.message.reply_text("Теперь введи норму (кол-во дней), например: 5")
    return SETNORM_DAYS


async def set_norm_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    t = (update.message.text or "").strip()
    if not t.isdigit():
        await update.message.reply_text("Нужны дни числом. Например: 7")
        return SETNORM_DAYS
    days = int(t)
    if days <= 0 or days > 365:
        await update.message.reply_text("Дни должны быть от 1 до 365.")
        return SETNORM_DAYS

    plant_id = int(context.user_data["setnorm_plant_id"])
    plant_name = context.user_data["setnorm_plant_name"]

    ok = set_norm(user_id, plant_id, days)
    if ok:
        await update.message.reply_text(f"Ок ✅ {plant_name} — раз в {days} дн.")
    else:
        await update.message.reply_text("Не получилось обновить норму 😕")
    return ConversationHandler.END


# ---------------- /water (выбор растения) ----------------
async def water_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь: /add_plant")
        return ConversationHandler.END
    context.user_data["water_plants"] = plants
    msg = "Какое растение полила? Введи номер:\n" + "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(plants)])
    await update.message.reply_text(msg)
    return WATER_PICK


async def water_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("water_plants", [])
    t = (update.message.text or "").strip()
    if not t.isdigit():
        await update.message.reply_text("Нужен номер. Например: 2")
        return WATER_PICK
    idx = int(t) - 1
    if idx < 0 or idx >= len(plants):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return WATER_PICK

    plant_id, name = plants[idx]
    when = datetime.now(timezone.utc)
    ok = log_water(user_id, int(plant_id), when)
    if ok:
        await update.message.reply_text(f"Зафиксировала ✅ {name}")
    else:
        await update.message.reply_text("Не получилось записать полив 😕")
    return ConversationHandler.END


# ---------------- /init_last (массовый ввод разных дат) ----------------
def _parse_date(text: str):
    t = text.strip().lower()
    if t == "today":
        return datetime.now(timezone.utc)
    try:
        d = date.fromisoformat(text.strip())
        return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
    except Exception:
        return None


async def init_last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = list_plants(user_id)
    if not plants:
        await update.message.reply_text("Список пуст. Добавь: /add_plant")
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
        + "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(plants)])
    )
    return INIT_LAST_INPUT


async def init_last_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    plants = context.user_data.get("init_last_plants", [])
    text = (update.message.text or "").strip()

    updates = {}
    bad = []

    for line in text.splitlines():
        if "=" not in line:
            bad.append(line)
            continue
        left, right = line.split("=", 1)
        left = left.strip()
        right = right.strip()

        if not left.isdigit():
            bad.append(line)
            continue
        idx = int(left) - 1
        if idx < 0 or idx >= len(plants):
            bad.append(line)
            continue

        dt = _parse_date(right)
        if not dt:
            bad.append(line)
            continue

        plant_id = int(plants[idx][0])
        updates[plant_id] = dt

    if not updates:
        await update.message.reply_text("Не распознала строки. Формат: 1=2026-01-10 или 2=today")
        return INIT_LAST_INPUT

    cnt = set_last_watered_bulk(user_id, updates)
    msg = f"Инициализация завершена ✅\nОбновлено растений: {cnt}"
    if bad:
        msg += "\n\nПропустила (не распознала):\n" + "\n".join([f"• {x}" for x in bad[:10]])
    await update.message.reply_text(msg)
    return ConversationHandler.END


# ---------------- /today + /autotoday ----------------
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    d = local_today()
    overdue, due_today, unknown = compute_due_lists(user_id, d)
    await update.message.reply_text(_format_today(overdue, due_today, unknown))


def _eligible_for_auto(now_local: datetime) -> bool:
    # отправляем только после 11:00 по IST (включительно)
    return (now_local.hour, now_local.minute) >= (AUTO_HOUR, AUTO_MINUTE)


async def autotoday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Эту команду будет дёргать бесплатный внешний cron.
    Она:
      - проверяет local time >= 11:00
      - проверяет, не слали ли уже сегодня
      - если есть overdue/today -> отправляет сводку в OWNER_CHAT_ID (если задан) иначе в текущий чат
      - пишет last_sent_local_date, чтобы не спамить
    """
    user_id = update.effective_user.id
    now_l = local_now()
    d = now_l.date()

    if not _eligible_for_auto(now_l):
        await update.message.reply_text("Ещё рано для авто-сводки. (ждём 11:00 IST)")
        return

    last_sent = get_last_sent_local_date(user_id)
    if last_sent == d:
        await update.message.reply_text("Авто-сводка уже отправлялась сегодня ✅")
        return

    overdue, due_today, unknown = compute_due_lists(user_id, d)

    # если вообще нечего поливать — можно не спамить
    if not overdue and not due_today:
        set_last_sent_local_date(user_id, d)
        await update.message.reply_text("Сегодня полив не нужен ✅ (авто отметила день)")
        return

    text = "⏰ Напоминание (11:00 IST)\n\n" + _format_today(overdue, due_today, unknown)

    # куда отправлять
    target_chat_id = int(OWNER_CHAT_ID) if OWNER_CHAT_ID else update.effective_chat.id
    await context.bot.send_message(chat_id=target_chat_id, text=text)

    set_last_sent_local_date(user_id, d)
    await update.message.reply_text("Ок ✅ авто-сводка отправлена")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Не знаю такую команду. Нажми /start")


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    base_url = os.environ["BASE_URL"].strip().rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    url_path = "webhook"
    webhook_url = f"{base_url}/{url_path}"

    init_db()

    async def post_init(app: Application):
        await app.bot.set_webhook(url=webhook_url)
        print("WEBHOOK SET TO:", webhook_url)
        print("PORT:", port)

    app = Application.builder().token(token).post_init(post_init).build()

    # простые команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("db", db_cmd))
    app.add_handler(CommandHandler("plants", plants_cmd))
    app.add_handler(CommandHandler("norms", norms_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("autotoday", autotoday_cmd))

    # диалоги
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add_plant", add_plant_cmd)],
        states={ADDPLANT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plant_input)]},
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("set_norm", set_norm_cmd)],
        states={
            SETNORM_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norm_pick)],
            SETNORM_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norm_days)],
        },
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("water", water_cmd)],
        states={WATER_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_pick)]},
        fallbacks=[],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("init_last", init_last_cmd)],
        states={INIT_LAST_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_last_input)]},
        fallbacks=[],
    ))

    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
