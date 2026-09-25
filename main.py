import os
import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
import uvicorn

# ── Package configuration ──
PACKAGES = {
    "20": {"label": "20zł - 2 zdjęcia i 1 film", "link": "https://mega.nz/folder/YUpkFbbR#zf6yaH--NH24zUq7aGs4cg"},
    "40": {"label": "40zł - 4 zdjęcia i 2 filmy", "link": "https://mega.nz/folder/RBwTBAyA#qMyQy7VbRNRba8dku4Z34w"},
    "60": {"label": "60zł - 6 zdjęć i 4 filmy", "link": "https://mega.nz/folder/lRRhTQDR#6a6q_58Tchz3RC4GzdPvew"},
    "80": {"label": "80zł - 10 zdjęć i 6 filmów", "link": "https://mega.nz/folder/JZ5CUJSQ#HQyJkLLqmWIupXi9frnKvw"},
    "100": {"label": "100zł - 20 zdjęć i 10 filmów", "link": "https://mega.nz/folder/wVgQHApI#Z8k-PSDN-fqeU_4DYjKenQ"},
    "350": {"label": "350zł - Cały folder (60GB)", "link": "https://mega.nz/folder/wIg2QLrD#xzaf8oGVHJUvgaEiLFPyPg"},
}

TIPPLY_LINK = "https://tipply.pl/@olcia_020"

# Sekret ustawiony w Telegramie przez setWebhook (parametr secret_token).
# Telegram odsyła go w nagłówku X-Telegram-Bot-Api-Secret-Token przy KAŻDYM
# requeście na webhook — jeśli się nie zgadza, request nie pochodzi od
# Telegrama i jest odrzucany.
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]

# ID właściciela wczytywane raz przy starcie, żeby uniknąć powtarzania
# int(os.environ[...]) w każdym handlerze.
OWNER_CHAT_ID = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])

app = FastAPI()


# ── Telegram API ──
def bot_url(method: str) -> str:
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    body: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("sendMessage"), json=body)


async def answer_callback(callback_query_id: str, text: str = ""):
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("answerCallbackQuery"), json={"callback_query_id": callback_query_id, "text": text})


async def remove_buttons(chat_id: int, message_id: int):
    async with httpx.AsyncClient() as c:
        await c.post(
            bot_url("editMessageReplyMarkup"),
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )


async def delayed_remove_buttons(chat_id: int, message_id: int, delay: int = 300):
    """Wait delay seconds then remove buttons."""
    await asyncio.sleep(delay)
    await remove_buttons(chat_id, message_id)


# ── Keyboards ──
def welcome_keyboard():
    return {
        "inline_keyboard": [
            [{"text": pkg["label"], "callback_data": f"pkg:{price}"}]
            for price, pkg in PACKAGES.items()
        ]
    }


def pay_keyboard(price: str):
    return {
        "inline_keyboard": [
            [{"text": "💰 Zapłać", "callback_data": f"pay:{price}"}],
        ]
    }


def paid_keyboard(price: str):
    return {"inline_keyboard": [[{"text": "✅ Zapłaciłem", "callback_data": f"paid:{price}"}]]}


def admin_keyboard(user_chat_id: int, price: str):
    return {
        "inline_keyboard": [
            [{"text": "✅ Potwierdź", "callback_data": f"ok:{user_chat_id}:{price}"}],
            [{"text": "❌ Odrzuć", "callback_data": f"no:{user_chat_id}"}],
        ]
    }


# ── Handlers ──
async def handle_start(chat_id: int):
    await send_message(chat_id, "💿 Hejka!\nWybierz pakiet:", welcome_keyboard())


async def handle_package(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, f"Wybrałeś pakiet za {price}zł")
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    await send_message(chat_id, f"Pakiet: {pkg['label']}\n\nZapłać czym tylko chcesz:", pay_keyboard(price))


async def handle_pay(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    text = (
        f"💰 Płatność\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Wpłać kwotę {price}zł na:\n{TIPPLY_LINK}\n\n"
        f"Po dokonaniu płatności kliknij przycisk:"
    )
    await send_message(chat_id, text, paid_keyboard(price))


async def handle_paid(chat_id, price, cb_id, username, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    await send_message(chat_id, "⏳ Czekaj na weryfikację...")
    pkg = PACKAGES[price]
    text = f"💰 Nowa płatność do weryfikacji\n\nUżytkownik: {username}\nPakiet: {pkg['label']} ({price}zł)\n\nCzy potwierdzasz?"
    await send_message(OWNER_CHAT_ID, text, admin_keyboard(chat_id, price))


async def handle_confirm(user_chat_id, price, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność potwierdzona ✅")
    link = PACKAGES[price]["link"]
    await send_message(user_chat_id, f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


async def handle_reject(user_chat_id, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność odrzucona ❌")
    await send_message(user_chat_id, "❌ Płatność nie została potwierdzona.\n\nSkontaktuj się z administratorem lub spróbuj ponownie.")
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


# ── Webhook ──
@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # 1) Odrzuć każdy request, który nie ma poprawnego sekretu Telegrama.
    #    Bez tego ktokolwiek znający adres /webhook mógłby wysłać własny,
    #    spreparowany JSON i np. udawać "ok:TWOJ_CHAT_ID:100", pomijając
    #    Ciebie jako weryfikatora płatności.
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"ok": False})

    data = await request.json()

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        cb_id = cb["id"]
        sender_id = cb["from"]["id"]
        username = cb["from"].get("first_name", "user")
        d = cb.get("data", "")

        if d.startswith("pkg:"):
            price = d.split(":")[1]
            if price in PACKAGES:
                await handle_package(chat_id, price, cb_id, msg_id)
        elif d.startswith("pay:"):
            price = d.split(":")[1]
            if price in PACKAGES:
                await handle_pay(chat_id, price, cb_id, msg_id)
        elif d.startswith("paid:"):
            price = d.split(":")[1]
            if price in PACKAGES:
                await handle_paid(chat_id, price, cb_id, username, msg_id)
        elif d.startswith("ok:"):
            # 2) Nawet gdyby ktoś zdobył sekret, tylko konto właściciela
            #    (Twoje) może potwierdzać lub odrzucać płatności.
            if sender_id != OWNER_CHAT_ID:
                await answer_callback(cb_id, "Brak uprawnień.")
                return {"ok": True}
            parts = d.split(":")
            user_chat_id, price = int(parts[1]), parts[2]
            if price in PACKAGES:
                await handle_confirm(user_chat_id, price, cb_id, chat_id, msg_id)
        elif d.startswith("no:"):
            if sender_id != OWNER_CHAT_ID:
                await answer_callback(cb_id, "Brak uprawnień.")
                return {"ok": True}
            user_chat_id = int(d.split(":")[1])
            await handle_reject(user_chat_id, cb_id, chat_id, msg_id)

    elif "message" in data:
        chat_id = data["message"]["chat"]["id"]
        await handle_start(chat_id)

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
