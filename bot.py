# bot.py — v4 full (archive + restore + centralized UX, auto tasks disabled)
import os
import html as _html
from datetime import datetime, date
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from storage import (
    init_db,
    add_plant,
    list_plants,
    rename_plant,
    set_norm,
    get_norms,
    log_water_many,
    compute_today,
    db_check,
    list_plants_archived,
    set_active,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Kolkata")


# =========================
# UX layer (constants/templates)
# =========================
class UX:
    PARSE_MODE = "HTML"

    @staticmethod
    def _esc(s: str) -> str:
        return _html.escape(s or "", quote=False)

    @staticmethod
    def plants_list(rows) -> str:
        # rows: List[(id, name)]
        lines = []
        for i, (_, name) in enumerate(rows, start=1):
            lines.append(f"{i}. {UX._esc(name)}")
        return "<i>\n" + "\n".join(lines) + "\n</i>"

    # --- generic blocks ---
    START = (
        "🌱 <b>PlantBuddy</b>\n"
        "Помню, когда поливать твои растения 🌿\n\n"
        "/add_plant — добавить растение\n"
        "/plants — список активных\n"
        "/rename_plant — переименовать\n"
        "/set_norms — задать норму полива\n"
        "/norms — показать нормы\n"
        "/today — что поливать сегодня\n"
        "/water — отметить полив\n"
        "/archive — убрать в архив\n"
        "/archived — показать архив\n"
        "/restore — вернуть из архива\n"
        "/cancel — отменить действие"
    )

    CANCEL_OK = "<b>Ок, отменили ✅</b>\n\nНичего не делаем."

    @staticmethod
    def db_ok(count: int) -> str:
        return f"<b>DB OK</b> 🌿\nРастений в базе: {count}"

    EMPTY_LIST = "Список пуст."

    # --- plants ---
    @staticmethod
    def plants(rows) -> str:
        return "<b>Твои растения 🌿</b>\n\n" + UX.plants_list(rows)

    # --- add plant ---
    ADD_PROMPT = (
        "<b>Добавим новое растение 🌱</b>\n\n"
        "Напиши название растения.\n\n"
        "Если передумала — /cancel"
    )
    ADD_DONE = "<b>Готово 🌱</b>\n\nРастение добавила."
    ADD_EMPTY = "<b>Хм, пусто 🤔</b>\n\nНапиши название растения.\n\nЕсли передумала — /cancel"

    # --- rename plant ---
    @staticmethod
    def rename_prompt(rows) -> str:
        return (
            "<b>Какое растение переименовать? ✏️</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши так:\n"
            "номер новое_название\n"
            "например: 2 Спатифиллум большой\n\n"
            "Если передумала — /cancel"
        )

    RENAME_BAD_FORMAT = (
        "<b>Хм, я не поняла 🤔</b>\n\n"
        "Попробуй так:\n<i>2 Спатифиллум большой</i>\n\n"
        "Если передумала — /cancel"
    )
    RENAME_NO_SUCH = "<b>Хм, такого номера нет 🤔</b>\n\nПроверь список выше."
    RENAME_DONE = "<b>Готово 🌱</b>\n\nРастение переименовала."
    RENAME_FAIL = "<b>Не получилось 🤔</b>\n\nВозможно, такое имя уже есть."

    # --- set norms ---
    @staticmethod
    def set_norms_prompt(rows) -> str:
        return (
            "<b>Зададим норму полива 💧</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши так:\n"
            "номер дни\n"
            "например: 1 5\n\n"
            "Если передумала — /cancel"
        )

    NORM_BAD_FORMAT = (
        "<b>Хм, я не поняла 🤔</b>\n\n"
        "Попробуй так:\n<i>1 5</i>\n\n"
        "Если передумала — /cancel"
    )
    NORM_NO_SUCH = "<b>Такого номера нет 🤔</b>"
    NORM_DONE = "<b>Готово 🌱</b>\n\nНорму сохранила."

    # --- norms list ---
    @staticmethod
    def norms(rows) -> str:
        lines = ["<b>Нормы полива 💧</b>\n", "<i>"]
        for name, days in rows:
            lines.append(f"{UX._esc(name)} — раз в {int(days)} дн.")
        lines.append("</i>")
        return "\n".join(lines)

    # --- today ---
    @staticmethod
    def today(res) -> str:
        overdue, today_list, unknown = res
        lines = ["🌿 <b>Сегодня по растениям</b>\n"]

        if today_list:
            lines.append("⏰ <b>Пора полить:</b>")
            lines.append("<i>")
            for name in today_list:
                lines.append(f"• {UX._esc(name)}")
            lines.append("</i>\n")

        if overdue:
            lines.append("⚠️ <b>Просрочено:</b>")
            lines.append("<i>")
            for name, days in overdue:
                lines.append(f"• {UX._esc(name)} — {int(days)} дн.")
            lines.append("</i>\n")

        if unknown:
            lines.append("ℹ️ <b>Нужно настроить:</b>")
            lines.append("<i>")
            for name in unknown:
                lines.append(f"• {UX._esc(name)}")
            lines.append("</i>")

        if not (today_list or overdue or unknown):
            return (
                "🌿 <b>Сегодня по растениям</b>\n\n"
                "Сегодня можно выдохнуть 😌\n"
                "Поливать ничего не нужно"
            )

        return "\n".join(lines).strip()

    # --- water ---
    @staticmethod
    def water_prompt(rows) -> str:
        return (
            "<b>Какие растения полила? 💧</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 1,3)\n\n"
            "Если передумала — /cancel"
        )

    WATER_DONE = "<b>Готово 💧</b>\n\nПолив отметила."
    WATER_BAD = "<b>Хм, я не поняла 🤔</b>\n\nПопробуй так:\n<i>1,3</i>"

    # --- archive ---
    @staticmethod
    def archive_prompt(rows) -> str:
        return (
            "<b>Хочешь убрать растение из активных? 🗂️</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 2)\n\n"
            "Если передумала — /cancel"
        )

    ARCHIVE_EMPTY = "Список пуст."

    @staticmethod
    def archive_done(n: int) -> str:
        if n == 1:
            return "<b>Готово 🌱</b>\n\nРастение убрала в архив."
        if 2 <= n <= 4:
            return f"<b>Готово 🌱</b>\n\nУбрала в архив {n} растения."
        return f"<b>Готово 🌱</b>\n\nУбрала в архив {n} растений."

    @staticmethod
    def archived_list(rows) -> str:
        return "<b>Растения в архиве 🗂️</b>\n\n" + UX.plants_list(rows)

    NO_ARCHIVED = "<b>В архиве пока пусто 🗂️</b>"

    # --- restore ---
    @staticmethod
    def restore_prompt(rows) -> str:
        return (
            "<b>Хочешь вернуть растение из архива? 🌿</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 1)\n\n"
            "Если передумала — /cancel"
        )

    RESTORE_BAD = "<b>Хм, я не поняла 🤔</b>\n\nПопробуй так:\n<i>1</i>"
    RESTORE_DONE = "<b>Готово 🌱</b>\n\nРастение снова активное."


# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(UX.START, parse_mode=UX.PARSE_MODE)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)


async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cnt = db_check(update.effective_user.id)
    await update.message.reply_text(UX.db_ok(cnt), parse_mode=UX.PARSE_MODE)


async def cmd_plants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    await update.message.reply_text(UX.plants(rows), parse_mode=UX.PARSE_MODE)


async def cmd_add_plant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["await_add"] = True
    await update.message.reply_text(UX.ADD_PROMPT, parse_mode=UX.PARSE_MODE)


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    context.user_data.clear()
    context.user_data["await_rename"] = True
    await update.message.reply_text(UX.rename_prompt(rows), parse_mode=UX.PARSE_MODE)


async def cmd_set_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    context.user_data.clear()
    context.user_data["await_norm"] = True
    await update.message.reply_text(UX.set_norms_prompt(rows), parse_mode=UX.PARSE_MODE)


async def cmd_norms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_norms(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Нормы не заданы.")
        return
    await update.message.reply_text(UX.norms(rows), parse_mode=UX.PARSE_MODE)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = compute_today(update.effective_user.id, date.today())
    await update.message.reply_text(UX.today(res), parse_mode=UX.PARSE_MODE)


async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    context.user_data.clear()
    context.user_data["await_water"] = True
    await update.message.reply_text(UX.water_prompt(rows), parse_mode=UX.PARSE_MODE)


async def cmd_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.ARCHIVE_EMPTY, parse_mode=UX.PARSE_MODE)
        return

    context.user_data.clear()
    context.user_data["await_archive"] = True
    await update.message.reply_text(UX.archive_prompt(rows), parse_mode=UX.PARSE_MODE)


async def cmd_archived(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants_archived(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.NO_ARCHIVED, parse_mode=UX.PARSE_MODE)
        return
    await update.message.reply_text(UX.archived_list(rows), parse_mode=UX.PARSE_MODE)


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants_archived(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.NO_ARCHIVED, parse_mode=UX.PARSE_MODE)
        return

    context.user_data.clear()
    context.user_data["await_restore"] = True
    await update.message.reply_text(UX.restore_prompt(rows), parse_mode=UX.PARSE_MODE)


def _parse_indices_csv(text: str, n_rows: int):
    parts = (text or "").replace(" ", "").split(",")
    idxs = []
    for p in parts:
        if p.isdigit():
            i = int(p) - 1
            if 0 <= i < n_rows:
                idxs.append(i)
    # remove duplicates, keep order
    seen = set()
    out = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # --- add plant ---
    if context.user_data.get("await_add"):
        if not text:
            await update.message.reply_text(UX.ADD_EMPTY, parse_mode=UX.PARSE_MODE)
            return
        add_plant(update.effective_user.id, text)
        context.user_data.clear()
        await update.message.reply_text(UX.ADD_DONE, parse_mode=UX.PARSE_MODE)
        return

    # --- rename ---
    if context.user_data.get("await_rename"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            await update.message.reply_text(UX.RENAME_BAD_FORMAT, parse_mode=UX.PARSE_MODE)
            return

        idx = int(parts[0]) - 1
        new_name = parts[1]
        rows = list_plants(update.effective_user.id)

        if not (0 <= idx < len(rows)):
            await update.message.reply_text(UX.RENAME_NO_SUCH, parse_mode=UX.PARSE_MODE)
            return

        ok = rename_plant(update.effective_user.id, rows[idx][0], new_name)
        context.user_data.clear()

        await update.message.reply_text(
            UX.RENAME_DONE if ok else UX.RENAME_FAIL,
            parse_mode=UX.PARSE_MODE,
        )
        return

    # --- set norms ---
    if context.user_data.get("await_norm"):
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text(UX.NORM_BAD_FORMAT, parse_mode=UX.PARSE_MODE)
            return

        idx = int(parts[0]) - 1
        days = int(parts[1])
        rows = list_plants(update.effective_user.id)

        if not (0 <= idx < len(rows)):
            await update.message.reply_text(UX.NORM_NO_SUCH, parse_mode=UX.PARSE_MODE)
            return

        set_norm(update.effective_user.id, rows[idx][0], days)
        context.user_data.clear()
        await update.message.reply_text(UX.NORM_DONE, parse_mode=UX.PARSE_MODE)
        return

    # --- water ---
    if context.user_data.get("await_water"):
        rows = list_plants(update.effective_user.id)
        idxs = _parse_indices_csv(text, len(rows))
        ids = [rows[i][0] for i in idxs]
        context.user_data.clear()

        if ids:
            log_water_many(update.effective_user.id, ids, datetime.now(TZ))
            await update.message.reply_text(UX.WATER_DONE, parse_mode=UX.PARSE_MODE)
        else:
            await update.message.reply_text(UX.WATER_BAD, parse_mode=UX.PARSE_MODE)
        return

    # --- archive ---
    if context.user_data.get("await_archive"):
        rows = list_plants(update.effective_user.id)
        idxs = _parse_indices_csv(text, len(rows))
        ids = [rows[i][0] for i in idxs]
        context.user_data.clear()

        if not ids:
            await update.message.reply_text(UX.WATER_BAD, parse_mode=UX.PARSE_MODE)  # same "1,3" hint fits
            return

        n = 0
        for pid in ids:
            if set_active(update.effective_user.id, pid, False):
                n += 1

        await update.message.reply_text(UX.archive_done(n), parse_mode=UX.PARSE_MODE)
        return

    # --- restore ---
    if context.user_data.get("await_restore"):
        rows = list_plants_archived(update.effective_user.id)
        idxs = _parse_indices_csv(text, len(rows))
        ids = [rows[i][0] for i in idxs]
        context.user_data.clear()

        if not ids:
            await update.message.reply_text(UX.RESTORE_BAD, parse_mode=UX.PARSE_MODE)
            return

        for pid in ids:
            set_active(update.effective_user.id, pid, True)

        await update.message.reply_text(UX.RESTORE_DONE, parse_mode=UX.PARSE_MODE)
        return


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add_plant", cmd_add_plant))
    app.add_handler(CommandHandler("plants", cmd_plants))
    app.add_handler(CommandHandler("rename_plant", cmd_rename))
    app.add_handler(CommandHandler("set_norms", cmd_set_norms))
    app.add_handler(CommandHandler("norms", cmd_norms))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("water", cmd_water))
    app.add_handler(CommandHandler("archive", cmd_archive))
    app.add_handler(CommandHandler("archived", cmd_archived))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("db", cmd_db))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
    )


if __name__ == "__main__":
    main()
