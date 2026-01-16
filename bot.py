# bot.py — Photo v1 (flow A): /photo -> choose plant -> send photo -> save tg file_id
import os
import html as _html
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Set, Optional, Tuple, List

import asyncio
import base64
from openai import OpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import json
import hashlib
import hmac
from urllib.parse import parse_qsl
import time

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

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
    add_plant_photo,
    get_plant_context,
    get_last_photo_for_plant,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Asia/Kolkata")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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
        lines = []
        for i, (_, name) in enumerate(rows, start=1):
            lines.append(f"{i}. {UX._esc(name)}")
        return "<i>\n" + "\n".join(lines) + "\n</i>"

    START = (
        "🌱<b>PlantBuddy</b>\n"
        "<b>Помню, когда поливать твои растения!</b>\n\n"
        "Можно писать команды или нажимать кнопки снизу👇\n\n"
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
        "/photo — спросить про растение\n"
        "/cancel — отменить действие"
    )


    CANCEL_OK = "<b>Ок, остановились🌿</b>\n\nЕсли что — я тут."
    EMPTY_LIST = "Пока тут пусто🌱\n\nДобавь растение через /add_plant"

    @staticmethod
    def db_ok(count: int) -> str:
        return f"<b>DB OK</b>🌿\nРастений в базе: {count}"

    @staticmethod
    def plants(rows) -> str:
        return "<b>Твои растения🌿</b>\n\n" + UX.plants_list(rows)

    ADD_PROMPT = (
        "<b>Добавим новое растение🌱</b>\n\n"
        "Напиши название растения.\n\n"
        "Если передумала — /cancel"
    )
    ADD_DONE = "<b>Готово🌱</b>\n\nРастение добавила."
    ADD_EMPTY = "<b>Хм, пусто🤔</b>\n\nНапиши название растения.\n\nЕсли передумала — /cancel"

    @staticmethod
    def rename_prompt(rows) -> str:
        return (
            "<b>Какое растение переименовать?✏️</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши так:\n"
            "номер новое_название\n"
            "например: 2 Спатифиллум большой\n\n"
            "Если передумала — /cancel"
        )

    RENAME_BAD_FORMAT = (
        "<b>Хм, я не поняла🤔</b>\n\n"
        "Попробуй так:\n<i>2 Спатифиллум большой</i>\n\n"
        "Если передумала — /cancel"
    )
    RENAME_NO_SUCH = "<b>Хм, такого номера нет🤔</b>\n\nПроверь список выше."
    RENAME_DONE = "<b>Готово🌱</b>\n\nРастение переименовала."
    RENAME_FAIL = "<b>Не получилось🤔</b>\n\nВозможно, такое имя уже есть."

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
        "<b>Хм, я не поняла🤔</b>\n\n"
        "Попробуй так:\n<i>1 5</i>\n\n"
        "Если передумала — /cancel"
    )
    NORM_NO_SUCH = "<b>Такого номера нет🤔</b>"
    NORM_DONE = "<b>Готово🌱</b>\n\nНорму сохранила."

    @staticmethod
    def norms(rows) -> str:
        lines = ["<b>Нормы полива 💧</b>\n", "<i>"]
        for name, days in rows:
            lines.append(f"{UX._esc(name)} — раз в {int(days)} дн.")
        lines.append("</i>")
        return "\n".join(lines)

    @staticmethod
    def today(res) -> str:
        overdue, today_list, unknown = res
        lines = ["🌿<b>Сегодня по растениям</b>\n"]

        if today_list:
            lines.append("⏰ <b>Пора полить:</b>")
            lines.append("<i>")
            for name in today_list:
                lines.append(f"• {UX._esc(name)}")
            lines.append("</i>\n")

        if overdue:
            lines.append("🧡 <b>Просрочено:</b>")
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
                "🌿<b>Сегодня по растениям</b>\n\n"
                "Сегодня можно выдохнуть😌\n"
                "Поливать ничего не нужно"
            )

        return "\n".join(lines).strip()

    WATER_DONE = "<b>Готово 💧</b>\n\nПолив отметила."
    WATER_BAD = "<b>Не поняла номера 🤔</b>\n\nПопробуй так: <i>1,3</i>"

    @staticmethod
    def archive_prompt(rows) -> str:
        return (
            "<b>Хочешь убрать растение из активных?🗂️</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 2)\n\n"
            "Если передумала — /cancel"
        )

    ARCHIVE_EMPTY = "Тут пока пусто🌿"

    @staticmethod
    def archive_done(n: int) -> str:
        if n == 1:
            return "<b>Готово🌱</b>\n\nРастение убрала в архив."
        if 2 <= n <= 4:
            return f"<b>Готово🌱</b>\n\nУбрала в архив {n} растения."
        return f"<b>Готово🌱</b>\n\nУбрала в архив {n} растений."

    @staticmethod
    def archived_list(rows) -> str:
        return "<b>Растения в архиве🗂️</b>\n\n" + UX.plants_list(rows)

    NO_ARCHIVED = "<b>В архиве пока пусто🌿</b>"

    @staticmethod
    def restore_prompt(rows) -> str:
        return (
            "<b>Хочешь вернуть растение из архива?🌿</b>\n\n"
            f"{UX.plants_list(rows)}\n\n"
            "Напиши номера через запятую (например: 1)\n\n"
            "Если передумала — /cancel"
        )

    RESTORE_BAD = "<b>Хм, я не поняла🤔</b>\n\nПопробуй так:\n<i>1</i>"
    RESTORE_DONE = "<b>Готово🌱</b>\n\nРастение снова активное."

    # --- water inline polish ---
    @staticmethod
    def water_screen(selected_count: int, selected_preview: str = "") -> str:
        if selected_count == 0:
            return "<b>Какие растения полила? 💧</b>\n\nВыбери из списка ниже👇"
        preview = f" — <i>{UX._esc(selected_preview)}</i>" if selected_preview else ""
        return f"<b>Выбрано: {selected_count}</b>{preview}\n\nВыбери ещё или нажми «Готово»✅"

    # --- photo flow ---
    PHOTO_CHOOSE = "<b>К какому растению это фото? 📸</b>\n\nВыбери из списка ниже👇"
    PHOTO_SEND = "<b>Ок👌</b>\n\nТеперь пришли фото этого растения 📸\n\nЕсли передумала — /cancel"
    PHOTO_SAVED = "<b>Приняла 📸</b>\n\nФото сохранила."
    PHOTO_ANALYZE_OFFER = "Хочешь — могу аккуратно прикинуть, что с растением?🧐"
    ANALYZE_WORKING = "<b>Смотрю фото👀</b>\n\nМинутку, я рядом."
    ANALYZE_NO_PHOTO = "<b>У меня нет фото для анализа 🤔</b>\n\nСначала пришли фото через /photo."
    ANALYZE_NO_KEY = "<b>Нужен ключ OpenAI 🤔</b>\n\nДобавь переменную окружения <i>OPENAI_API_KEY</i> на Render."
    ANALYZE_ERROR = "<b>Упс🤔</b>\n\nНе получилось проанализировать фото. Попробуй ещё раз чуть позже."
    PHOTO_EXPECTED = "<b>Жду фото 📸</b>\n\nПришли фото растения.\n\nЕсли передумала — /cancel"


# =========================
# Main menu (ReplyKeyboard)
# =========================
MENU_TODAY = "📅План на сегодня"
MENU_WATER = "💧Отметить полив"
MENU_PHOTO = "💬Спросить про растение"
MENU_PLANTS = "🪴Посмотреть все растения"
MENU_NORMS = "💦Узнать частоту полива"
MENU_APP = "🧾Открыть PlantBuddy"

def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_APP, web_app=WebAppInfo(url=f"{BASE_URL}/app"))],
            [KeyboardButton(MENU_TODAY), KeyboardButton(MENU_WATER)],
            [KeyboardButton(MENU_PHOTO), KeyboardButton(MENU_PLANTS)],
            [KeyboardButton(MENU_NORMS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )


# =========================
# Inline keyboard: /water selection (same as v6)
# =========================
CB_W_TOGGLE = "w:tg"
CB_W_DONE = "w:dn"
CB_W_CANCEL = "w:cn"

def _water_state_key() -> str:
    return "water_sel_ids"

def _water_rows_key() -> str:
    return "water_rows_cache"

def _get_water_selected(context: ContextTypes.DEFAULT_TYPE) -> Set[int]:
    return set(context.user_data.get(_water_state_key(), set()))

def _set_water_selected(context: ContextTypes.DEFAULT_TYPE, ids: Set[int]):
    context.user_data[_water_state_key()] = set(ids)

def _cache_water_rows(context: ContextTypes.DEFAULT_TYPE, rows: List[Tuple[int, str]]):
    context.user_data[_water_rows_key()] = rows

def _get_cached_water_rows(context: ContextTypes.DEFAULT_TYPE) -> Optional[List[Tuple[int, str]]]:
    return context.user_data.get(_water_rows_key())

def _selected_preview(rows: List[Tuple[int, str]], selected: Set[int], max_items: int = 3) -> str:
    names = []
    for pid, name in rows:
        if pid in selected:
            names.append(name)
        if len(names) >= max_items:
            break
    if not names:
        return ""
    if len(selected) > max_items:
        return ", ".join(names) + "…"
    return ", ".join(names)

def build_water_keyboard(rows: List[Tuple[int, str]], selected: Set[int]) -> InlineKeyboardMarkup:
    grid: List[List[InlineKeyboardButton]] = []
    row_buf: List[InlineKeyboardButton] = []
    for pid, name in rows:
        label = f"{'✅ ' if pid in selected else ''}{name}"
        btn = InlineKeyboardButton(label, callback_data=f"{CB_W_TOGGLE}:{pid}")
        row_buf.append(btn)
        if len(row_buf) == 2:
            grid.append(row_buf)
            row_buf = []
    if row_buf:
        grid.append(row_buf)
    grid.append([
        InlineKeyboardButton("✅Готово", callback_data=CB_W_DONE),
        InlineKeyboardButton("🌱Не сейчас", callback_data=CB_W_CANCEL),
    ])
    return InlineKeyboardMarkup(grid)


# =========================
# Inline keyboard: /photo plant choice
# =========================
CB_P_TOGGLE = "p:ch"  # p:ch:<plant_id>
CB_P_CANCEL = "p:cn"

def build_photo_keyboard(rows: List[Tuple[int, str]]) -> InlineKeyboardMarkup:
    grid: List[List[InlineKeyboardButton]] = []
    row_buf: List[InlineKeyboardButton] = []
    for pid, name in rows:
        btn = InlineKeyboardButton(name, callback_data=f"{CB_P_TOGGLE}:{pid}")
        row_buf.append(btn)
        if len(row_buf) == 2:
            grid.append(row_buf)
            row_buf = []
    if row_buf:
        grid.append(row_buf)
    grid.append([InlineKeyboardButton("🌱Не сейчас", callback_data=CB_P_CANCEL)])
    return InlineKeyboardMarkup(grid)


CB_A_RUN = "an:run"  # an:run:<plant_id>
CB_A_CANCEL = "an:cn"

def build_analyze_keyboard(plant_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠Проанализировать", callback_data=f"{CB_A_RUN}:{plant_id}")],
        [InlineKeyboardButton("🌱Не сейчас", callback_data=CB_A_CANCEL)],
    ])



# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(UX.START, parse_mode=UX.PARSE_MODE, reply_markup=build_main_menu())

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE, reply_markup=build_main_menu())

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
    text = UX.today(res)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💧 Отметить полив", callback_data="go:water")]])
    await update.message.reply_text(text, parse_mode=UX.PARSE_MODE, reply_markup=keyboard)
    # keep main menu visible
    await update.message.reply_text("Выбери следующее действие👇", reply_markup=build_main_menu())

async def cmd_water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    context.user_data.clear()
    context.user_data["await_water_buttons"] = True
    _cache_water_rows(context, rows)
    _set_water_selected(context, set())
    await update.message.reply_text(
        UX.water_screen(0),
        parse_mode=UX.PARSE_MODE,
        reply_markup=build_water_keyboard(rows, set()),
    )
    await update.message.reply_text(
        "Если удобнее текстом — напиши номера через запятую (например: 1,3)\n\nЕсли передумала — /cancel",
        parse_mode=UX.PARSE_MODE,
    )

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

# ---------- photo ----------
async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_plants(update.effective_user.id)
    if not rows:
        await update.message.reply_text(UX.EMPTY_LIST)
        return
    context.user_data.clear()
    context.user_data["await_photo_pick"] = True
    await update.message.reply_text(
        UX.PHOTO_CHOOSE,
        parse_mode=UX.PARSE_MODE,
        reply_markup=build_photo_keyboard(rows),
    )

def _parse_indices_csv(text: str, n_rows: int):
    parts = (text or "").replace(" ", "").split(",")
    idxs = []
    for p in parts:
        if p.isdigit():
            i = int(p) - 1
            if 0 <= i < n_rows:
                idxs.append(i)
    seen = set()
    out = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

# ---------- callbacks ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    if data == "go:water":
        rows = list_plants(update.effective_user.id)
        if not rows:
            await q.edit_message_text(UX.EMPTY_LIST, parse_mode=UX.PARSE_MODE)
            return
        context.user_data.clear()
        context.user_data["await_water_buttons"] = True
        _cache_water_rows(context, rows)
        _set_water_selected(context, set())
        try:
            await q.edit_message_text(
                UX.water_screen(0),
                parse_mode=UX.PARSE_MODE,
                reply_markup=build_water_keyboard(rows, set()),
            )
        except Exception:
            await q.message.reply_text(
                UX.water_screen(0),
                parse_mode=UX.PARSE_MODE,
                reply_markup=build_water_keyboard(rows, set()),
            )
        return

    if data.startswith(f"{CB_W_TOGGLE}:"):
        if not context.user_data.get("await_water_buttons"):
            return
        try:
            pid = int(data.split(":")[-1])
        except Exception:
            return
        selected = _get_water_selected(context)
        if pid in selected:
            selected.remove(pid)
        else:
            selected.add(pid)
        _set_water_selected(context, selected)
        rows = _get_cached_water_rows(context) or list_plants(update.effective_user.id)
        _cache_water_rows(context, rows)
        preview = _selected_preview(rows, selected)
        try:
            await q.edit_message_text(
                UX.water_screen(len(selected), preview),
                parse_mode=UX.PARSE_MODE,
                reply_markup=build_water_keyboard(rows, selected),
            )
        except Exception:
            await q.edit_message_reply_markup(reply_markup=build_water_keyboard(rows, selected))
        return

    if data == CB_W_CANCEL:
        context.user_data.clear()
        try:
            await q.edit_message_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        return

    if data == CB_W_DONE:
        if not context.user_data.get("await_water_buttons"):
            return
        selected = _get_water_selected(context)
        if not selected:
            await q.message.reply_text("<b>Выбери хотя бы одно растение🤔</b>", parse_mode=UX.PARSE_MODE)
            return
        log_water_many(update.effective_user.id, list(selected), datetime.now(TZ))
        context.user_data.clear()
        try:
            await q.edit_message_text(UX.WATER_DONE, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.WATER_DONE, parse_mode=UX.PARSE_MODE)
        return

    # photo plant choice
    if data == CB_P_CANCEL:
        context.user_data.clear()
        try:
            await q.edit_message_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        return

    if data.startswith(f"{CB_P_TOGGLE}:"):
        if not context.user_data.get("await_photo_pick"):
            return
        try:
            pid = int(data.split(":")[-1])
        except Exception:
            return
        # store chosen plant_id and wait for photo
        context.user_data.clear()
        context.user_data["await_photo_upload"] = True
        context.user_data["photo_plant_id"] = pid
        try:
            await q.edit_message_text(UX.PHOTO_SEND, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.PHOTO_SEND, parse_mode=UX.PARSE_MODE)
        return

    # analyze
    if data == CB_A_CANCEL:
        try:
            await q.edit_message_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE)
        return

    if data.startswith(f"{CB_A_RUN}:"):
        try:
            pid = int(data.split(":")[-1])
        except Exception:
            return
        try:
            await q.edit_message_text(UX.ANALYZE_WORKING, parse_mode=UX.PARSE_MODE)
        except Exception:
            await q.message.reply_text(UX.ANALYZE_WORKING, parse_mode=UX.PARSE_MODE)

        try:
            analysis_text = await _analyze_latest_photo(update.effective_user.id, pid, context)
        except Exception:
            analysis_text = UX.ANALYZE_ERROR

        await q.message.reply_text(analysis_text, parse_mode=UX.PARSE_MODE)
        return

# ---------- messages ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()


    # Main menu shortcuts (only when not inside a multi-step flow)
    if not any(k.startswith("await_") for k in context.user_data.keys()):
        if text == MENU_TODAY:
            await cmd_today(update, context)
            return
        if text == MENU_WATER:
            await cmd_water(update, context)
            return
        if text == MENU_PHOTO:
            await cmd_photo(update, context)
            return
        if text == MENU_PLANTS:
            await cmd_plants(update, context)
            return
        if text == MENU_NORMS:
            await cmd_norms(update, context)
            return

    if context.user_data.get("await_add"):
        if not text:
            await update.message.reply_text(UX.ADD_EMPTY, parse_mode=UX.PARSE_MODE)
            return
        add_plant(update.effective_user.id, text)
        context.user_data.clear()
        await update.message.reply_text(UX.ADD_DONE, parse_mode=UX.PARSE_MODE)
        return

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
        await update.message.reply_text(UX.RENAME_DONE if ok else UX.RENAME_FAIL, parse_mode=UX.PARSE_MODE)
        return

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

    # water text fallback
    if context.user_data.get("await_water_buttons"):
        rows = list_plants(update.effective_user.id)
        idxs = _parse_indices_csv(text, len(rows))
        ids = [rows[i][0] for i in idxs]
        if ids:
            log_water_many(update.effective_user.id, ids, datetime.now(TZ))
            context.user_data.clear()
            await update.message.reply_text(UX.WATER_DONE, parse_mode=UX.PARSE_MODE)
        else:
            await update.message.reply_text(UX.WATER_BAD, parse_mode=UX.PARSE_MODE)
        return

    if context.user_data.get("await_archive"):
        rows = list_plants(update.effective_user.id)
        idxs = _parse_indices_csv(text, len(rows))
        ids = [rows[i][0] for i in idxs]
        context.user_data.clear()
        if not ids:
            await update.message.reply_text(UX.WATER_BAD, parse_mode=UX.PARSE_MODE)
            return
        n = 0
        for pid in ids:
            if set_active(update.effective_user.id, pid, False):
                n += 1
        await update.message.reply_text(UX.archive_done(n), parse_mode=UX.PARSE_MODE)
        return

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

    # photo: if user sends text while waiting photo
    if context.user_data.get("await_photo_upload"):
        await update.message.reply_text(UX.PHOTO_EXPECTED, parse_mode=UX.PARSE_MODE)
        return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_photo_upload"):
        # ignore photos outside flow (or we can guide)
        return

    plant_id = context.user_data.get("photo_plant_id")
    if not plant_id:
        context.user_data.clear()
        await update.message.reply_text(UX.CANCEL_OK, parse_mode=UX.PARSE_MODE, reply_markup=build_main_menu())
        return

    # choose best size (last is usually the largest)
    photo = update.message.photo[-1]
    file_id = photo.file_id
    unique_id = getattr(photo, "file_unique_id", None)
    caption = update.message.caption if update.message.caption else None

    add_plant_photo(
        user_id=update.effective_user.id,
        plant_id=int(plant_id),
        tg_file_id=file_id,
        tg_file_unique_id=unique_id,
        caption=caption,
    )

    context.user_data.clear()
    await update.message.reply_text(UX.PHOTO_SAVED, parse_mode=UX.PARSE_MODE)
    await update.message.reply_text(
        UX.PHOTO_ANALYZE_OFFER,
        parse_mode=UX.PARSE_MODE,
        reply_markup=build_analyze_keyboard(int(plant_id)),
    )


async def _analyze_latest_photo(user_id: int, plant_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not _openai_client:
        return UX.ANALYZE_NO_KEY

    plant_ctx = get_plant_context(user_id, plant_id)
    if not plant_ctx:
        return "<b>Не нашла это растение🤔</b>"

    plant_name, norm_days, last_watered_at = plant_ctx

    photo_row = get_last_photo_for_plant(user_id, plant_id)
    if not photo_row:
        return UX.ANALYZE_NO_PHOTO

    _photo_id, tg_file_id, tg_unique_id, caption, created_at = photo_row

    tg_file = await context.bot.get_file(tg_file_id)
    data = await tg_file.download_as_bytearray()

    b64 = base64.b64encode(bytes(data)).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"

    today = datetime.now(TZ).date()
    days_since = None
    if last_watered_at:
        try:
            days_since = (today - last_watered_at.astimezone(TZ).date()).days
        except Exception:
            try:
                days_since = (today - last_watered_at.date()).days
            except Exception:
                days_since = None

    ctx_lines = [
        f"Plant name: {plant_name}",
        f"Watering norm: {norm_days} days" if norm_days else "Watering norm: unknown",
        f"Days since last watering: {days_since}" if days_since is not None else "Days since last watering: unknown",
    ]
    if caption:
        ctx_lines.append(f"User caption: {caption}")

    instructions = (
        "You are a careful plant-care assistant. Analyze the photo and give practical care advice.\n"
        "Rules:\n"
        "- Separate clearly: (1) What you can directly see in the image (facts) vs (2) hypotheses.\n"
        "- If confidence is low, ask for 1-2 specific extra photos instead of guessing.\n"
        "- Avoid dangerous chemical advice. Prefer gentle, safe steps.\n"
        "- Keep it concise.\n\n"
        "Output in Russian with this exact structure:\n"
        "1) Коротко (1-2 строки)\n"
        "2) Что вижу на фото (3-6 буллетов)\n"
        "3) Вероятные причины (2-4 пункта, с оценкой уверенности: высокая/средняя/низкая)\n"
        "4) Что сделать сейчас (с приоритетами: сегодня / на неделе / не делать)\n"
        "5) Если нужно уточнить — что доснять/спросить\n"
    )

    user_text = "Context:\n" + "\n".join(ctx_lines) + "\n\nPlease analyze the image."

    def _call_openai():
        return _openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions + "\n\n" + user_text},
                    {"type": "input_image", "image_url": data_url},
                ],
            }],
            max_output_tokens=650,
        )

    resp = await asyncio.to_thread(_call_openai)
    out = getattr(resp, "output_text", "") or ""
    out = out.strip()
    if not out:
        return UX.ANALYZE_ERROR

    safe = _html.escape(out, quote=False)
    return f"🧠 <b>Анализ фото: {UX._esc(plant_name)}</b>\n\n<i>{safe}</i>"


# =========================
# FastAPI (ASGI) server: Mini App + Telegram webhook
# =========================

ASGI_TTL_SECONDS = int(os.environ.get("TMA_TTL_SECONDS", "300"))  # 5 minutes default

def _validate_and_get_user_id(init_data: str) -> int:
    """Validates Telegram Mini App initData and returns Telegram user_id.

    Follows Telegram WebApp data validation algorithm.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    # Parse querystring-like initData
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    # Build data_check_string
    data_check_items = []
    for k in sorted(pairs.keys()):
        data_check_items.append(f"{k}={pairs[k]}")
    data_check_string = "\n".join(data_check_items)

    # secret_key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Bad initData hash")

    # Anti-replay: auth_date freshness
    auth_date_raw = pairs.get("auth_date")
    if auth_date_raw and auth_date_raw.isdigit():
        auth_date = int(auth_date_raw)
        if int(time.time()) - auth_date > ASGI_TTL_SECONDS:
            raise HTTPException(status_code=401, detail="initData expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing user")

    try:
        user_obj = json.loads(user_raw)
        user_id = int(user_obj.get("id"))
    except Exception:
        raise HTTPException(status_code=401, detail="Bad user payload")

    return user_id


def _build_today_payload(user_id: int):
    today = datetime.now(TZ).date()
    rows = list_plants(user_id)  # [(id, name)]
    items = []
    due_count = 0
    overdue_count = 0
    setup_count = 0

    for pid, name in rows:
        ctx = get_plant_context(user_id, pid)  # (name, norm_days, last_watered_at)
        if not ctx:
            continue
        _pname, norm_days, last_watered_at = ctx

        status = "ok"
        days_since = None
        overdue_days = None
        due_in_days = None

        if not norm_days or not last_watered_at:
            status = "setup"
            setup_count += 1
        else:
            try:
                last_local = last_watered_at.astimezone(TZ)
            except Exception:
                last_local = last_watered_at
            last_date = last_local.date()
            days_since = (today - last_date).days
            due_date = last_date + timedelta(days=int(norm_days))
            if due_date < today:
                status = "overdue"
                overdue_days = (today - due_date).days
                overdue_count += 1
            elif due_date == today:
                status = "due"
                due_count += 1
            else:
                status = "ok"
                due_in_days = (due_date - today).days

        items.append({
            "id": int(pid),
            "name": str(name),
            "status": status,  # ok / due / overdue / setup
            "norm_days": int(norm_days) if norm_days else None,
            "days_since": int(days_since) if days_since is not None else None,
            "overdue_days": int(overdue_days) if overdue_days is not None else None,
            "due_in_days": int(due_in_days) if due_in_days is not None else None,
        })

    # Sort like a tracker: overdue, due, setup, ok
    order = {"overdue": 0, "due": 1, "setup": 2, "ok": 3}
    items.sort(key=lambda x: (order.get(x["status"], 9), x["name"].lower()))

    return {
        "date": today.isoformat(),
        "summary": {
            "overdue": overdue_count,
            "due": due_count,
            "setup": setup_count,
            "total": len(items),
        },
        "items": items,
    }


def _mini_app_html() -> str:
    # Minimal tracker UI, no build step. Uses Telegram WebApp JS.
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PlantBuddy</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    body{margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,Arial,sans-serif;background:#0b0f14;color:#e8eef5}
    .wrap{max-width:720px;margin:0 auto;padding:16px}
    h1{font-size:22px;margin:0 0 6px 0}
    .sub{opacity:.75;margin:0 0 14px 0;font-size:14px}
    .pill{display:inline-block;padding:6px 10px;border-radius:999px;background:#111a24;margin-right:8px;font-size:13px}
    .card{background:#0f1621;border:1px solid #1b2838;border-radius:14px;padding:12px 12px;margin:10px 0;display:flex;gap:10px;align-items:flex-start}
    .cb{margin-top:2px;transform:scale(1.2)}
    .title{font-size:16px;margin:0 0 4px 0}
    .meta{font-size:13px;opacity:.75;margin:0}
    .tag{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid #24354b;opacity:.9}
    .row{display:flex;justify-content:space-between;gap:10px;align-items:center}
    .btn{width:100%;margin-top:14px;padding:14px 14px;border-radius:12px;border:0;background:#1f6feb;color:white;font-size:16px;font-weight:600}
    .btn:disabled{opacity:.5}
    .err{margin-top:10px;color:#ff6b6b;font-size:13px;white-space:pre-wrap}
    .info{margin:14px 0 0 0;font-size:13px;opacity:.8}
  </style>
</head>
<body>
<div class="wrap">
  <div class="row">
    <div>
      <h1>📅Сегодня</h1>
      <p class="sub" id="sub">Загружаю…</p>
    </div>
  </div>

  <div id="pills"></div>
  <div id="list"></div>

  <button id="waterBtn" class="btn" disabled>✅Отметить полив</button>

  <p class="info">ℹ️Анализ фото отправляет изображение в OpenAI(только если ты нажмёшь анализ в боте).</p>
  <div id="err" class="err"></div>
</div>

<script>
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }

  const initData = tg ? (tg.initData || "") : "";
  const elSub = document.getElementById("sub");
  const elPills = document.getElementById("pills");
  const elList = document.getElementById("list");
  const elErr = document.getElementById("err");
  const waterBtn = document.getElementById("waterBtn");

  const selected = new Set();

  function pill(text){
    const s=document.createElement("span");
    s.className="pill";
    s.textContent=text;
    return s;
  }

  function statusTag(status, item){
    let t = "";
    if (status==="overdue") t = "Просрочено";
    else if (status==="due") t = "Пора";
    else if (status==="setup") t = "Нужно настроить";
    else t = "Ок";
    const span=document.createElement("span");
    span.className="tag";
    span.textContent=t;
    return span;
  }

  function metaText(item){
    const parts=[];
    if (item.norm_days) parts.push(`норма ${item.norm_days} дн`);
    if (item.days_since!==null && item.days_since!==undefined) parts.push(`последний полив ${item.days_since} дн назад`);
    if (item.status==="overdue" && item.overdue_days!==null) parts.push(`просрочено на ${item.overdue_days} дн`);
    if (item.status==="ok" && item.due_in_days!==null) parts.push(`следующий полив через ${item.due_in_days} дн`);
    return parts.join(" · ");
  }

  function render(data){
    elSub.textContent = `Всего: ${data.summary.total} · Пора: ${data.summary.due} · Просрочено: ${data.summary.overdue} · Настроить: ${data.summary.setup}`;
    elPills.innerHTML="";
    elPills.appendChild(pill(`Пора: ${data.summary.due}`));
    elPills.appendChild(pill(`Просрочено: ${data.summary.overdue}`));
    elPills.appendChild(pill(`Настроить: ${data.summary.setup}`));

    elList.innerHTML="";
    data.items.forEach(item=>{
      const card=document.createElement("div");
      card.className="card";

      const cb=document.createElement("input");
      cb.type="checkbox";
      cb.className="cb";
      cb.checked = selected.has(item.id);
      cb.addEventListener("change", ()=>{
        if (cb.checked) selected.add(item.id); else selected.delete(item.id);
        waterBtn.disabled = selected.size===0;
      });

      const box=document.createElement("div");
      box.style.flex="1";

      const row=document.createElement("div");
      row.className="row";

      const t=document.createElement("div");
      const h=document.createElement("p");
      h.className="title";
      h.textContent=item.name;
      t.appendChild(h);

      const tag=statusTag(item.status,item);
      row.appendChild(t);
      row.appendChild(tag);

      const meta=document.createElement("p");
      meta.className="meta";
      meta.textContent=metaText(item);

      box.appendChild(row);
      box.appendChild(meta);

      card.appendChild(cb);
      card.appendChild(box);

      elList.appendChild(card);
    });
    waterBtn.disabled = selected.size===0;
  }

  async function load(){
    elErr.textContent="";
    try{
      const r = await fetch("/api/today", { headers: { "X-Telegram-InitData": initData } });
      const j = await r.json();
      if (!r.ok) throw new Error(JSON.stringify(j));
      render(j);
    }catch(e){
      elSub.textContent="Не получилось загрузить данные";
      elErr.textContent = "Ошибка загрузки:" + String(e.message||e);
    }
  }

  waterBtn.addEventListener("click", async ()=>{
    elErr.textContent="";
    waterBtn.disabled = true;
    try{
      const r = await fetch("/api/water", {
        method:"POST",
        headers: { "Content-Type":"application/json", "X-Telegram-InitData": initData },
        body: JSON.stringify({ plant_ids: Array.from(selected) })
      });
      const j = await r.json();
      if (!r.ok) throw new Error(JSON.stringify(j));
      selected.clear();
      await load();
    }catch(e){
      elErr.textContent = "Ошибка:" + String(e.message||e);
      waterBtn.disabled = selected.size===0;
    }
  });

  load();
</script>
</body>
</html>"""


# Exported ASGI app for uvicorn: "uvicorn bot:app --host 0.0.0.0 --port $PORT"
app = FastAPI()

# Telegram Application (python-telegram-bot)
telegram_app: Optional[Application] = None


@app.on_event("startup")
async def _startup():
    global telegram_app
    init_db()

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cmd_cancel))
    telegram_app.add_handler(CommandHandler("add_plant", cmd_add_plant))
    telegram_app.add_handler(CommandHandler("plants", cmd_plants))
    telegram_app.add_handler(CommandHandler("rename_plant", cmd_rename))
    telegram_app.add_handler(CommandHandler("set_norms", cmd_set_norms))
    telegram_app.add_handler(CommandHandler("norms", cmd_norms))
    telegram_app.add_handler(CommandHandler("today", cmd_today))
    telegram_app.add_handler(CommandHandler("water", cmd_water))
    telegram_app.add_handler(CommandHandler("archive", cmd_archive))
    telegram_app.add_handler(CommandHandler("archived", cmd_archived))
    telegram_app.add_handler(CommandHandler("restore", cmd_restore))
    telegram_app.add_handler(CommandHandler("photo", cmd_photo))
    telegram_app.add_handler(CommandHandler("db", cmd_db))

    telegram_app.add_handler(CallbackQueryHandler(on_callback))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await telegram_app.initialize()
    await telegram_app.start()

    # Ensure webhook points to our FastAPI route
    await telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook")


@app.on_event("shutdown")
async def _shutdown():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.stop()
        finally:
            await telegram_app.shutdown()
        telegram_app = None


@app.get("/app", response_class=HTMLResponse)
async def mini_app():
    return HTMLResponse(_mini_app_html())


@app.get("/api/today")
async def api_today(x_telegram_initdata: Optional[str] = Header(default=None, alias="X-Telegram-InitData")):
    user_id = _validate_and_get_user_id(x_telegram_initdata or "")
    return JSONResponse(_build_today_payload(user_id))


@app.post("/api/water")
async def api_water(request: Request, x_telegram_initdata: Optional[str] = Header(default=None, alias="X-Telegram-InitData")):
    user_id = _validate_and_get_user_id(x_telegram_initdata or "")
    data = await request.json()
    plant_ids = data.get("plant_ids") or []
    try:
        plant_ids = [int(x) for x in plant_ids]
    except Exception:
        raise HTTPException(status_code=400, detail="Bad plant_ids")

    log_water_many(user_id, plant_ids, datetime.now(TZ))
    return JSONResponse({"ok": True, "updated": len(plant_ids)})


@app.post("/webhook")
async def telegram_webhook(request: Request):
    global telegram_app
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Bot not ready")

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})


# Optional local run: python bot.py
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, reload=False)
