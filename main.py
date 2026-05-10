import os
import base64
from typing import Any

import httpx
from fastapi import FastAPI, Request
import uvicorn

# ── PayPal configuration ──
PAYPAL_BASE = "https://api-m.sandbox.paypal.com"  # zmień na https://api-m.paypal.com dla live

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


async def answer_callback(callback_query_id: str, text: str = ""):
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("answerCallbackQuery"), json={"callback_query_id": callback_query_id, "text": text})


async def remove_buttons(chat_id: int, message_id: int):
    async with httpx.AsyncClient() as c:
        await c.post(
            bot_url("editMessageReplyMarkup"),
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )


# ── PayPal API ──
async def paypal_get_token() -> str:
    cid = os.environ["PAYPAL_CLIENT_ID"]
    secret = os.environ["PAYPAL_CLIENT_SECRET"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "client_credentials"},
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def paypal_create_order(price: str) -> tuple[str, str]:
    token = await paypal_get_token()
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": "PLN", "value": f"{price}.00"}, "description": f"Pakiet {price}zl"}],
        "application_context": {
            "brand_name": "olix_303",
            "user_action": "PAY_NOW",
            "return_url": "https://t.me/olix_03_bot",
            "cancel_url": "https://t.me/olix_03_bot",
        },
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        order = r.json()
    order_id = order["id"]
    approve_url = next(l["href"] for l in order["links"] if l["rel"] == "approve")
    return order_id, approve_url


async def paypal_check_and_capture(order_id: str) -> bool:
    token = await paypal_get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as c:
        status_r = await c.get(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}", headers=headers)
        status_r.raise_for_status()
        status = status_r.json()["status"]
        if status == "COMPLETED":
            return True
        if status == "APPROVED":
            cap = await c.post(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture", headers=headers)
            cap.raise_for_status()
            return cap.json()["status"] == "COMPLETED"
    return False


# ── Keyboards ──
def welcome_keyboard():
    return {
        "inline_keyboard": [
            [{"text": pkg["label"], "callback_data": f"pkg:{price}"}]
            for price, pkg in PACKAGES.items()
        ]
    }


def payment_method_keyboard(price: str):
    return {
        "inline_keyboard": [
            [{"text": "💳 BLIK", "callback_data": f"blik:{price}"}],
            [{"text": "💰 PayPal", "callback_data": f"pp:{price}"}],
        ]
    }


def blik_paid_keyboard(price: str):
    return {"inline_keyboard": [[{"text": "✅ Zapłaciłem", "callback_data": f"paid:{price}"}]]}


def paypal_paid_keyboard(order_id: str, price: str):
    return {"inline_keyboard": [[{"text": "✅ Zapłaciłem (PayPal)", "callback_data": f"ppck:{order_id}:{price}"}]]}


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
    await send_message(chat_id, f"Pakiet: <b>{pkg['label']}</b>\n\nWybierz metodę płatności:", payment_method_keyboard(price))


async def handle_blik(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    text = f"💳 <b>Płatność BLIK</b>\n\nPakiet: <b>{pkg['label']}</b>\n\nWyślij kwotę na numer:\n<b>533003463</b>\n\nPo dokonaniu płatności kliknij przycisk:"
    await send_message(chat_id, text, blik_paid_keyboard(price))


async def handle_paypal_start(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Tworzę zamówienie PayPal...")
    await remove_buttons(chat_id, msg_id)
    order_id, approve_url = await paypal_create_order(price)
    pkg = PACKAGES[price]
    text = (
        f"💰 <b>Płatność PayPal</b>\n\n"
        f"Pakiet: <b>{pkg['label']}</b>\n\n"
        f'Kliknij link, aby zapłacić:\n'
        f'<a href="{approve_url}">🔗 Zapłać przez PayPal</a>\n\n'
        f"Po zapłaceniu kliknij przycisk:"
    )
    await send_message(chat_id, text, paypal_paid_keyboard(order_id, price))


async def handle_paypal_check(chat_id, order_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Sprawdzam płatność...")
    paid = await paypal_check_and_capture(order_id)
    if paid:
        await remove_buttons(chat_id, msg_id)
        link = PACKAGES[price]["link"]
        await send_message(chat_id, f"✅ <b>Płatność potwierdzona!</b>\n\nOto Twój dostęp:\n{link}")
    else:
        await send_message(chat_id, "⚠️ Płatność jeszcze nie doszła.\n\nOtwórz link powyżej, zapłać i kliknij przycisk ponownie.")


async def handle_paid(chat_id, price, cb_id, username, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    await send_message(chat_id, "⏳ Czekaj na weryfikację...")
    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    pkg = PACKAGES[price]
    text = f"💰 <b>Nowa płatność do weryfikacji</b>\n\nUżytkownik: <b>{username}</b>\nPakiet: <b>{pkg['label']}</b>\n\nCzy potwierdzasz?"
    await send_message(owner_id, text, admin_keyboard(chat_id, price))


async def handle_confirm(user_chat_id, price, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność potwierdzona ✅")
    link = PACKAGES[price]["link"]
    await send_message(user_chat_id, f"✅ <b>Płatność potwierdzona!</b>\n\nOto Twój dostęp:\n{link}")


async def handle_reject(user_chat_id, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność odrzucona ❌")
    await send_message(user_chat_id, "❌ Płatność nie została potwierdzona.\n\nSkontaktuj się z administratorem lub spróbuj ponownie.")


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
        elif d.startswith("blik:"):
            await handle_blik(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("pp:"):
            await handle_paypal_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("ppck:"):
            parts = d.split(":")
            await handle_paypal_check(chat_id, parts[1], parts[2], cb_id, msg_id)
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
