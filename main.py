import os
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- optional .env for local dev ---
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("unitcalc-tma")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()  # e.g. https://gri85.github.io/unitcalc-tma/
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()  # any random string
BASE_URL = ((os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()).rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set it as environment variable (Render) or in .env (local).")
if not WEBAPP_URL:
    raise RuntimeError("WEBAPP_URL is empty. Set it as environment variable (Render) or in .env (local).")


def make_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧮 Открыть калькулятор", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "Ок, открывай калькулятор кнопкой ниже 👇",
        reply_markup=make_keyboard(),
    )


async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "Лови калькулятор 👇",
        reply_markup=make_keyboard(),
    )


async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.web_app_data:
        return

    raw = msg.web_app_data.data or ""
    payload = None
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}

    # Здесь позже сделаем красивый разбор юнит-экономики.
    await msg.reply_text(f"✅ Данные получены:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")


def build_telegram_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("calc", cmd_calc))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))
    return app


telegram_app = build_telegram_app()


async def homepage(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


async def telegram_webhook(request: Request) -> Response:
    if request.method != "POST":
        return PlainTextResponse("Method not allowed", status_code=405)

    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return PlainTextResponse("Unauthorized", status_code=401)

    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("Bad JSON", status_code=400)

    try:
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.update_queue.put(update)
    except Exception as e:
        log.exception("Failed to enqueue update: %s", e)
        return PlainTextResponse("Error", status_code=500)

    return Response(status_code=200)


async def on_startup() -> None:
    await telegram_app.initialize()
    await telegram_app.start()

    # На Render BASE_URL обычно берётся из RENDER_EXTERNAL_URL
    if not BASE_URL:
        log.warning("BASE_URL is empty. Webhook will NOT be set automatically.")
        log.warning("Set PUBLIC_BASE_URL (or rely on RENDER_EXTERNAL_URL on Render).")
        return

    webhook_url = f"{BASE_URL}/telegram/webhook"
    log.info("Setting webhook to %s", webhook_url)

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


async def on_shutdown() -> None:
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    await telegram_app.stop()
    await telegram_app.shutdown()


routes = [
    Route("/", endpoint=homepage, methods=["GET"]),
    Route("/health", endpoint=health, methods=["GET"]),
    Route("/telegram/webhook", endpoint=telegram_webhook, methods=["POST"]),
]

app = Starlette(routes=routes, on_startup=[on_startup], on_shutdown=[on_shutdown])


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
