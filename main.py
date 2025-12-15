import os
import json
import logging
from datetime import datetime, timezone

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

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
BASE_URL = ((os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()).rstrip("/")

FREE_SENDS_PER_DAY = int(os.getenv("FREE_SENDS_PER_DAY", "5"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty.")
if not WEBAPP_URL:
    raise RuntimeError("WEBAPP_URL is empty.")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "<REDACTED>")
            record.msg = msg
            record.args = ()
        except Exception:
            pass
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# чтобы токен не светился в логах httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().addFilter(RedactFilter([BOT_TOKEN, WEBHOOK_SECRET]))

log = logging.getLogger("unitcalc-tma")

# --- лимитер в памяти процесса ---
USAGE: dict[int, dict[str, object]] = {}


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_and_inc(user_id: int) -> tuple[bool, int]:
    day = _today_key()
    rec = USAGE.get(user_id)
    if not rec or rec.get("day") != day:
        rec = {"day": day, "count": 0}
        USAGE[user_id] = rec

    count = int(rec.get("count", 0))
    if count >= FREE_SENDS_PER_DAY:
        return (False, 0)

    count += 1
    rec["count"] = count
    remaining = max(0, FREE_SENDS_PER_DAY - count)
    return (True, remaining)


def make_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧮 Открыть калькулятор", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "Ок, открывай калькулятор кнопкой ниже 👇\n\n"
        f"Бесплатно: {FREE_SENDS_PER_DAY} отправок/день.",
        reply_markup=make_keyboard(),
    )


async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text("Лови калькулятор 👇", reply_markup=make_keyboard())


async def cmd_pro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "💳 Pro скоро будет.\n\n"
        "Pro снимет лимит и добавит сохранение/экспорт.\n"
        "Пока просто напиши «хочу Pro»."
    )


def _fmt_rub(x) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except Exception:
        return "—"


def _fmt_pct(x) -> str:
    try:
        return f"{float(x):.1f}".replace(".", ",")
    except Exception:
        return "—"


async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.web_app_data or not user:
        return

    user_id = user.id
    allowed, remaining = _check_and_inc(user_id)

    log.info("web_app_data received: user_id=%s allowed=%s remaining=%s", user_id, allowed, remaining)

    if not allowed:
        await msg.reply_text(
            f"Лимит бесплатных отправок на сегодня исчерпан: {FREE_SENDS_PER_DAY}/{FREE_SENDS_PER_DAY}.\n"
            "Хочешь Pro — напиши /pro"
        )
        log.info("limit message sent to user_id=%s", user_id)
        return

    raw = msg.web_app_data.data or ""
    try:
        payload = json.loads(raw)
    except Exception:
        await msg.reply_text("Получил данные, но не смог распарсить. Попробуй ещё раз.")
        log.info("json parse failed for user_id=%s", user_id)
        return

    profit = payload.get("profit")
    margin = payload.get("margin")
    ad_max = payload.get("adMax")
    p_be = payload.get("pBe")

    try:
        p = float(profit) if profit is not None else None
    except Exception:
        p = None

    status = "✅ В плюсе" if (p is not None and p > 0) else ("⚠️ В ноль" if p == 0 else "❌ В минус")

    text = (
        f"📌 Юнит-экономика\n"
        f"{status}\n\n"
        f"• Прибыль: {_fmt_rub(profit)} ₽\n"
        f"• Маржа: {_fmt_pct(margin)} %\n"
        f"• Max CPO: {_fmt_rub(ad_max)} ₽\n"
        f"• Цена безубыточности: {_fmt_rub(p_be)} ₽\n\n"
        f"Осталось бесплатных отправок сегодня: {remaining}"
    )
    await msg.reply_text(text)
    log.info("report sent to user_id=%s", user_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("PTB error handler caught exception: %s", context.error)


def build_telegram_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("calc", cmd_calc))
    app.add_handler(CommandHandler("pro", cmd_pro))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))
    app.add_error_handler(on_error)
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

        # ВАЖНО: обрабатываем апдейт сразу (не через очередь),
        # чтобы точно отрабатывало на мобилке.
        await telegram_app.process_update(update)

    except Exception as e:
        log.exception("Failed to process update: %s", e)
        return PlainTextResponse("Error", status_code=500)

    return Response(status_code=200)


async def on_startup() -> None:
    await telegram_app.initialize()
    await telegram_app.start()

    if not BASE_URL:
        log.warning("BASE_URL is empty. Webhook will NOT be set automatically.")
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
    # НЕ удаляем webhook на shutdown (Render может рестартить процесс)
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
