import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
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


async def answer_callback(callback_query_id: str):
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("answerCallbackQuery"), json={"callback_query_id": callback_query_id})


async def remove_buttons(chat_id: int, message_id: int):
    async with httpx.AsyncClient() as c:
        await c.post(
            bot_url("editMessageReplyMarkup"),
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )


# ── Keyboards ──
def welcome_keyboard():
    return {
        "inline_keyboard": [
            [{"text": pkg["label"], "callback_data": f"pkg:{price}"}]
            for price, pkg in PACKAGES.items()
        ]
    }


def paid_keyboard(price: str):
    return {
        "inline_keyboard": [[{"text": "✅ Zapłaciłem", "callback_data": f"paid:{price}"}]]
    }


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
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)

    text = f"💳 <b>Płatność BLIK</b>\n\nWyślij {price}zł na numer:\n<b>533003463</b>"
    await send_message(chat_id, text, paid_keyboard(price))


async def handle_paid(chat_id, price, cb_id, username, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)

    await send_message(chat_id, "⏳ Czekaj na weryfikację...")

    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])

    text = f"💰 {username} zapłacił {price}zł"
    await send_message(owner_id, text, admin_keyboard(chat_id, price))


async def handle_confirm(user_chat_id, price, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(admin_chat_id, msg_id)

    link = PACKAGES[price]["link"]
    await send_message(user_chat_id, f"✅ Oto dostęp:\n{link}")


async def handle_reject(user_chat_id, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(admin_chat_id, msg_id)

    await send_message(user_chat_id, "❌ Płatność odrzucona")


# ── Webhook ──
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        cb_id = cb["id"]
        username = cb["from"].get("first_name", "user")
        d = cb.get("data", "")

        if d.startswith("pkg:"):
            await handle_package(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("paid:"):
            await handle_paid(chat_id, d.split(":")[1], cb_id, username, msg_id)
        elif d.startswith("ok:"):
            parts = d.split(":")
            await handle_confirm(int(parts[1]), parts[2], cb_id, chat_id, msg_id)
        elif d.startswith("no:"):
            await handle_reject(int(d.split(":")[1]), cb_id, chat_id, msg_id)

    elif "message" in data:
        chat_id = data["message"]["chat"]["id"]
        await handle_start(chat_id)

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
