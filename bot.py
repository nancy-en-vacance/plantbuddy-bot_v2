
def verify_telegram_init_data(init_data: str, bot_token: str) -> dict:
    """
    Verifies Telegram WebApp initData signature.

    Telegram algorithm:
    - Parse initData querystring to key/value pairs
    - Take 'hash' (hex) and exclude it from the check string
    - data_check_string = "\n".join("key=value" for keys sorted lexicographically)
    - secret_key = sha256(bot_token)
    - computed_hash = hmac_sha256(secret_key, data_check_string).hexdigest()
    """
    if not init_data:
        raise ValueError("Missing initData")

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    data = {k: v for k, v in pairs}

    received_hash = data.get("hash")
    if not received_hash:
        raise ValueError("Missing hash")

    data.pop("hash", None)

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))

        # Telegram WebApp secret: HMAC_SHA256(key="WebAppData", msg=bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        print("INITDATA_VERIFY_FAIL: hash_mismatch")
        raise ValueError("Bad initData signature")

    return data


def extract_user_id_from_init_data(data: dict) -> int:
    if "user" in data:
        user_obj = json.loads(data["user"])
        if isinstance(user_obj, dict) and "id" in user_obj:
            return int(user_obj["id"])
    if "user_id" in data:
        return int(data["user_id"])
    raise ValueError("No user id in initData")


# --- PlantBuddy unified ASGI app (FastAPI + Telegram webhook) ---
import os
import json
import hmac
import hashlib
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import storage  # existing storage.py

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")


# Inline WebApp opener (hard-reset friendly)
def build_open_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(MENU_APP, web_app=WebAppInfo(url=f"{BASE_URL}/app?v=18"))]]
    )

if not BOT_TOKEN or not BASE_URL:
    raise RuntimeError("BOT_TOKEN and BASE_URL must be set")

app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()

MENU_TODAY = "📅План на сегодня"
MENU_WATER = "💧Отметить полив"
MENU_PHOTO = "💬Спросить про растение"
MENU_PLANTS = "🪴Посмотреть все растения"
MENU_NORMS = "💦Узнать частоту полива"
MENU_APP = "🧾Открыть PlantBuddy"

# ---------------- Bot UI (reply keyboard) ----------------
def build_bot_menu() -> ReplyKeyboardMarkup:
    # Важно: Mini App открываем НЕ web_app-кнопкой в reply keyboard (Telegram кеширует).
    # Вместо этого: текстовая кнопка -> бот присылает inline WebApp кнопку (cmd_open).
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_APP)],
            [KeyboardButton(MENU_PHOTO)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )

# ---------------- Photo analysis (MVP) ----------------
async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_photo"] = True
    await update.message.reply_text(
        "Пришли фото растения — я посмотрю и подскажу🌿\n"
        "Небольшой дисклеймер: это не диагноз, а помощь по уходу."
    )

def _load_prompt() -> str:
    try:
        here = Path(__file__).resolve().parent
        p = (here / "prompt.txt")
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return "You are a plant care assistant. Provide calm, practical plant care advice."

async def _analyze_with_openai(image_bytes: bytes) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "Сейчас анализ по фото не настроен: не найден OPENAI_API_KEY в переменных окружения."

    prompt = _load_prompt()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # Responses API (newer SDKs)
        resp = client.responses.create(
            model=model,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }],
        )
        out_text = getattr(resp, "output_text", None)
        if out_text:
            return out_text.strip()

        # Fallback: try to parse output blocks
        try:
            chunks = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        chunks.append(getattr(c, "text", ""))
            joined = "\n".join([x for x in chunks if x]).strip()
            return joined or "Не получилось получить ответ от модели."
        except Exception:
            return "Не получилось получить ответ от модели."
    except Exception as e:
        return f"Ошибка анализа по фото: {type(e).__name__}"

async def handle_plant_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_photo"):
        return
    context.user_data["awaiting_photo"] = False

    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        image_bytes = await tg_file.download_as_bytearray()
        answer = await _analyze_with_openai(bytes(image_bytes))
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"Не смогла обработать фото: {type(e).__name__}")



def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_APP, web_app=WebAppInfo(url=f"{BASE_URL}/app?v=18"))],
            [KeyboardButton(MENU_TODAY), KeyboardButton(MENU_WATER)],
            [KeyboardButton(MENU_PHOTO), KeyboardButton(MENU_PLANTS)],
            [KeyboardButton(MENU_NORMS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "**Помню, когда поливать твои растения🌿**\n\nОткрой приложение или выбери действие в меню ниже."
    if update.message:
        await update.message.reply_text("Обновляю интерфейс…", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(text, reply_markup=build_open_inline(), parse_mode="Markdown")
        await update.message.reply_text("Меню:", reply_markup=build_bot_menu())

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(MessageHandler(filters.Regex(f"^{MENU_APP}$"), cmd_open))
tg_app.add_handler(MessageHandler(filters.Regex(f"^{MENU_PHOTO}$"), cmd_photo))
tg_app.add_handler(MessageHandler(filters.PHOTO, handle_plant_photo))

async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Открываю PlantBuddy…", reply_markup=build_open_inline())

tg_app.add_handler(CommandHandler("open", cmd_open))

async def cmd_reset_kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Сбрасываю клавиатуру…", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("Готово.", reply_markup=build_main_menu())

tg_app.add_handler(CommandHandler("reset_kb", cmd_reset_kb))



def get_user_id_from_request(req: Request) -> int:
    init_data = req.headers.get("X-Telegram-InitData", "")
    print(f"X-Telegram-InitData len={len(init_data)}", flush=True)

    try:
        data = verify_telegram_init_data(init_data, BOT_TOKEN)
    except ValueError as e:
        # Do not leak initData; keep details minimal
        raise HTTPException(status_code=401, detail=str(e))

    # Optional: expiry check (10 min)
    try:
        auth_date = int(data.get("auth_date", "0"))
        now = int(datetime.now(tz=timezone.utc).timestamp())
        if auth_date and (now - auth_date > 60 * 10):
            raise HTTPException(status_code=401, detail="initData expired")
    except Exception:
        pass

    user_id = extract_user_id_from_init_data(data)
    return int(user_id)


@app.on_event("startup")
async def _startup():
    await tg_app.initialize()
    await tg_app.bot.set_webhook(url=f"{BASE_URL}/webhook")
    # Hard reset: добавляем кнопку в меню чата (работает стабильнее на iOS, чем reply keyboard)
    try:
        await tg_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🧾Открыть PlantBuddy",
                web_app=WebAppInfo(url=f"{BASE_URL}/app?v=18")
            )
        )
    except Exception:
        pass


@app.on_event("shutdown")
async def _shutdown():
    try:
        await tg_app.shutdown()
    except Exception:
        pass


APP_VERSION = "mvp-v18-photo"

@app.get("/debug/version")
async def debug_version():
    return {"version": APP_VERSION, "base_url": BASE_URL}

@app.api_route("/", methods=["GET","HEAD"])
async def root():
    return {"ok": True}


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


@app.get("/api/ping")
async def api_ping():
    return {"ok": True}


@app.get("/api/today")
async def api_today(request: Request):
    """Return today's plant cards for the Mini App.

    NOTE: storage.py in this repo returns tuples for list_plants();
    here we query the DB directly to get (id, name, norm, last) in one go.
    """
    user_id = get_user_id_from_request(request)

    now = datetime.now(timezone.utc)
    items: list[dict] = []

    with storage.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, water_every_days, last_watered_at
                FROM plants
                WHERE user_id = %s AND active = TRUE
                ORDER BY id
                """,
                (user_id,),
            )
            rows = cur.fetchall() or []

    for pid, name, norm, last in rows:
        # last_watered_at comes as datetime (usually tz-aware) or None
        last_iso = None
        days_since = None
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            last_iso = last.astimezone(timezone.utc).isoformat()
            days_since = (now - last).days

        status = "unknown"
        due_in = None

        if norm is not None:
            # if never watered but norm exists -> due now
            if days_since is None:
                status = "due"
                due_in = 0
            else:
                due_in = int(norm) - int(days_since)
                if due_in < 0:
                    status = "overdue"
                elif due_in == 0:
                    status = "due"
                else:
                    status = "ok"

        items.append(
            {
                "id": int(pid),
                "name": str(name),
                "norm_days": int(norm) if norm is not None else None,
                "last_watered_at": last_iso,
                "days_since_last_watering": int(days_since) if days_since is not None else None,
                "due_in_days": int(due_in) if due_in is not None else None,
                "status": status,
            }
        )

    return JSONResponse(content={"items": items})


@app.post("/api/water")
async def api_water(request: Request):
    user_id = get_user_id_from_request(request)
    payload = await request.json()
    plant_ids = payload.get("plant_ids", [])
    if not isinstance(plant_ids, list):
        raise HTTPException(status_code=400, detail="plant_ids must be a list")

    now = datetime.now(timezone.utc)
    updates: Dict[int, datetime] = {}
    for pid in plant_ids:
        try:
            updates[int(pid)] = now
        except Exception:
            continue

    updated = storage.set_last_watered_bulk(user_id, updates) if updates else 0
    return JSONResponse({"ok": True, "updated": updated})


@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
