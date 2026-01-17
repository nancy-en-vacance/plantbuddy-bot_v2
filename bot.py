# --- PlantBuddy unified ASGI app (FastAPI + Telegram webhook) ---
from pathlib import Path
import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
)
from telegram.ext import Application, CommandHandler, ContextTypes

import storage  # existing storage.py

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")

if not BOT_TOKEN or not BASE_URL:
    raise RuntimeError("BOT_TOKEN and BASE_URL must be set")

# ---------------- FastAPI app ----------------
app = FastAPI()

# ---------------- Telegram app ----------------
tg_app = Application.builder().token(BOT_TOKEN).build()

MENU_TODAY = "📅План на сегодня"
MENU_WATER = "💧Отметить полив"
MENU_PHOTO = "💬Спросить про растение"
MENU_PLANTS = "🪴Посмотреть все растения"
MENU_NORMS = "💦Узнать частоту полива"
MENU_APP = "🧾Открыть PlantBuddy"


def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_APP, web_app=WebAppInfo(url=f"{BASE_URL}/app?v=3"))],
            [KeyboardButton(MENU_TODAY), KeyboardButton(MENU_WATER)],
            [KeyboardButton(MENU_PHOTO), KeyboardButton(MENU_PLANTS)],
            [KeyboardButton(MENU_NORMS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # UX requirement: bold phrase in start message
    text = "**Помню, когда поливать твои растения🌿**\n\nНажми кнопку снизу или напиши команду."
    await update.message.reply_text(text, reply_markup=build_main_menu(), parse_mode="Markdown")


# Register minimal handler(s). You can add the rest back later; this keeps the bot usable.
tg_app.add_handler(CommandHandler("start", cmd_start))


# ---------------- initData validation ----------------
def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    data = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    # anti-replay
    auth_date = int(data.get("auth_date", "0"))
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if now - auth_date > 60 * 10:
        raise HTTPException(status_code=401, detail="initData expired")

    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Bad initData signature")

    return data


def get_user_id_from_request(req: Request) -> int:
    init_data = req.headers.get("X-Telegram-InitData", "")
    data = validate_init_data(init_data)
    user = json.loads(data.get("user", "{}"))
    uid = user.get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="No user id")
    return int(uid)


# ---------------- FastAPI lifecycle ----------------
@app.on_event("startup")
async def _startup():
    # Initialize telegram application once and set webhook
    await tg_app.initialize()
    await tg_app.bot.set_webhook(url=f"{BASE_URL}/webhook")


@app.on_event("shutdown")
async def _shutdown():
    # Graceful shutdown
    try:
        await tg_app.shutdown()
    except Exception:
        pass


@app.get("/")
async def root():
    return {"ok": True}

# ---------------- Web routes ----------------
@app.get("/app")
async def app_page():
    try:
        html = Path("app.html").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="app.html not found in repo root")
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/today")
async def api_today(request: Request):
    user_id = get_user_id_from_request(request)
    items = storage.list_plants_full(user_id)
    return JSONResponse({"items": items})


@app.post("/api/water")
async def api_water(request: Request):
    user_id = get_user_id_from_request(request)
    payload = await request.json()
    plant_ids = payload.get("plant_ids", [])
    for pid in plant_ids:
        storage.mark_watered(user_id, pid)
    return JSONResponse({"ok": True})


@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
