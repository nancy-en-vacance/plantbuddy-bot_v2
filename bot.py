# bot.py — v4 (restore from archive added)
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo
from html import escape

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
    list_plants_archived,
    set_active,
    rename_plant,
    set_norm,
    get_norms,
    log_water_many,
    compute_today,
    db_check,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Kolkata")


# ---------- UX ----------
class UX:
    @staticmethod
    def _esc(s: str) -> str:
        return escape(s)

    @staticmethod
    def plants_list(rows):
        if not rows:
            return "<i>(пусто)</i>"
        lines = [f"{i}. {UX._esc(name)}" for i, (_, name) in enumerate(rows, start=1)]
        return "<i>\n" + "\n".join(lines) + "\n</i>"

    @staticmethod
    def today(res):
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
                lines.append(f"• {UX._esc(name)} — {days} дн.")
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

    START = (
        "🌱 <b>PlantBuddy</b>\n\n"
        "/add_plant — добавить растение\n"
        "/plants — список активных\n"
        "/archive — убрать в архив\n"
        "/archived — показать архив\n"
        "/restore — вернуть из архива\n"
        "/rename_plant — переименовать\n"
        "/set_norms — задать норму\n"
        "/norms — показать нормы\n"
        "/today — что поливать сегодня\n"
        "/water — отметить полив\n"
        "/cancel — отменить действие"
    )

    CANCEL_OK = "<b>Ок, отменили ✅</b>\n\nНичего не делаем."

    @staticmethod
    def archive_prompt(rows):
        return (
            "<b>Хочешь вернуть растение из архива? 🌿</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 1)\n\n"
            "Если передумала — /cancel"
        )

    @staticmethod
    def archived_empty():
        return "<b>Архив пуст 🗂️</b>"

    @staticmethod
    def restored_ok():
        return "<b>Готово 🌱</b>\n\nРастение снова активное."


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(UX.START, parse_mode="HTML")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(UX.CANCEL_OK, parse_mode="HTML")


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants_archived(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.archived_empty(), parse_mode="HTML")
        return
    context.user_data.clear()
    context.user_data["await_restore"] = True
    await update.message.reply_text(UX.archive_prompt(rows), parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if context.user_data.get("await_restore"):
        nums = text.replace(" ", "").split(",")
        rows = list_plants_archived(update.effective_user.id)
        ids = []
        for n in nums:
            if n.isdigit():
                idx = int(n) - 1
                if 0 <= idx < len(rows):
                    ids.append(rows[idx][0])
        context.user_data.clear()
        if not ids:
            await update.message.reply_text(
                "<b>Хм, я не поняла 🤔</b>\n\nПопробуй так:\n<i>1</i>",
                parse_mode="HTML",
            )
            return
        for pid in ids:
            set_active(update.effective_user.id, pid, True)
        await update.message.reply_text(UX.restored_ok(), parse_mode="HTML")
        return


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
    )


if __name__ == "__main__":
    main()
