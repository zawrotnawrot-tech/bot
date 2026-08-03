import os
import base64
import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, Request
import uvicorn

# ── PayPal configuration ──
PAYPAL_BASE = "https://api-m.sandbox.paypal.com"  # zmień na https://api-m.paypal.com dla live

# ── Stripe configuration ──
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_AUTH = (STRIPE_SECRET_KEY, "")

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

# ── Weryfikacja wieku (tylko deklaracja, bez sprawdzania dowodem) ──
# Trzyma w pamięci ID czatów, które potwierdziły że mają 18+.
VERIFIED_ADULTS: set[int] = set()


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


# ── Stripe API (nieaktywne dopóki nie ustawisz STRIPE_SECRET_KEY) ──
async def stripe_create_checkout_session(price: str) -> tuple[str, str]:
    data = {
        "mode": "payment",
        "success_url": "https://t.me/olix_03_bot",
        "cancel_url": "https://t.me/olix_03_bot",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "pln",
        "line_items[0][price_data][unit_amount]": str(int(price) * 100),
        "line_items[0][price_data][product_data][name]": f"Pakiet {price}zl",
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{STRIPE_API_BASE}/checkout/sessions", data=data, auth=STRIPE_AUTH)
        r.raise_for_status()
        session = r.json()
    return session["id"], session["url"]


async def stripe_check_session_paid(session_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{STRIPE_API_BASE}/checkout/sessions/{session_id}", auth=STRIPE_AUTH)
        r.raise_for_status()
        session = r.json()
    return session.get("payment_status") == "paid"


# ── Keyboards ──
def age_gate_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "✅ Tak, mam 18 lat lub więcej", "callback_data": "wiek_tak"}],
            [{"text": "❌ Nie, nie mam 18 lat", "callback_data": "wiek_nie"}],
        ]
    }


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
            [{"text": "💰 PayPal", "callback_data": f"pp:{price}"}],
            [{"text": "💳 Stripe", "callback_data": f"st:{price}"}],
        ]
    }


def paypal_buttons(order_id: str, price: str, approve_url: str):
    return {
        "inline_keyboard": [
            [{"text": "💳 Zapłać przez PayPal", "url": approve_url}],
            [{"text": "✅ Zapłaciłem (PayPal)", "callback_data": f"ppck:{order_id}:{price}"}],
        ]
    }


def stripe_buttons(session_id: str, price: str, checkout_url: str):
    return {
        "inline_keyboard": [
            [{"text": "💳 Zapłać przez Stripe", "url": checkout_url}],
            [{"text": "✅ Zapłaciłem (Stripe)", "callback_data": f"stck:{session_id}:{price}"}],
        ]
    }


# ── Handlers ──
async def send_age_gate(chat_id: int):
    await send_message(
        chat_id,
        "🔞 Weryfikacja wieku\n\nTa oferta może zawierać treści dla osób pełnoletnich.\nPotwierdź swój wiek, aby kontynuować:",
        age_gate_keyboard(),
    )


async def handle_age_yes(chat_id, cb_id, msg_id):
    VERIFIED_ADULTS.add(chat_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    await handle_start(chat_id)


async def handle_age_no(chat_id, cb_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    await send_message(chat_id, "🚫 Przykro nam, ten bot jest dostępny tylko dla osób pełnoletnich (18+).")


async def handle_start(chat_id: int):
    await send_message(chat_id, "💿 Hejka!\nWybierz pakiet:", welcome_keyboard())


async def handle_package(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, f"Wybrałeś pakiet za {price}zł")
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    await send_message(chat_id, f"Pakiet: {pkg['label']}\n\nWybierz metodę płatności:", payment_method_keyboard(price))


async def handle_paypal_start(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Tworzę zamówienie PayPal...")
    await remove_buttons(chat_id, msg_id)
    order_id, approve_url = await paypal_create_order(price)
    pkg = PACKAGES[price]
    text = (
        f"💰 Płatność PayPal\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Kliknij przycisk poniżej, aby zapłacić przez PayPal.\n"
        f"Po zapłaceniu kliknij ✅ Zapłaciłem."
    )
    await send_message(chat_id, text, paypal_buttons(order_id, price, approve_url))


async def handle_paypal_check(chat_id, order_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Sprawdzam płatność...")
    paid = await paypal_check_and_capture(order_id)
    if paid:
        await remove_buttons(chat_id, msg_id)
        link = PACKAGES[price]["link"]
        await send_message(chat_id, f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
    else:
        await send_message(chat_id, "⚠️ Płatność jeszcze nie doszła.\n\nOtwórz link powyżej, zapłać i kliknij przycisk ponownie.")


async def handle_stripe_start(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Tworzę zamówienie Stripe...")
    await remove_buttons(chat_id, msg_id)
    session_id, checkout_url = await stripe_create_checkout_session(price)
    pkg = PACKAGES[price]
    text = (
        f"💳 Płatność Stripe\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Kliknij przycisk poniżej, aby zapłacić przez Stripe.\n"
        f"Po zapłaceniu kliknij ✅ Zapłaciłem."
    )
    await send_message(chat_id, text, stripe_buttons(session_id, price, checkout_url))


async def handle_stripe_check(chat_id, session_id, price, cb_id, msg_id):
    await answer_callback(cb_id, "Sprawdzam płatność...")
    paid = await stripe_check_session_paid(session_id)
    if paid:
        await remove_buttons(chat_id, msg_id)
        link = PACKAGES[price]["link"]
        await send_message(chat_id, f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
    else:
        await send_message(chat_id, "⚠️ Płatność jeszcze nie doszła.\n\nOtwórz link powyżej, zapłać i kliknij przycisk ponownie.")


# ── Webhook ──
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        cb_id = cb["id"]
        d = cb.get("data", "")

        if d == "wiek_tak":
            await handle_age_yes(chat_id, cb_id, msg_id)
            return {"ok": True}
        if d == "wiek_nie":
            await handle_age_no(chat_id, cb_id, msg_id)
            return {"ok": True}

        if chat_id not in VERIFIED_ADULTS:
            await answer_callback(cb_id)
            await send_age_gate(chat_id)
            return {"ok": True}

        if d.startswith("pkg:"):
            await handle_package(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("pp:"):
            await handle_paypal_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("ppck:"):
            parts = d.split(":")
            await handle_paypal_check(chat_id, parts[1], parts[2], cb_id, msg_id)
        elif d.startswith("st:"):
            await handle_stripe_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("stck:"):
            parts = d.split(":")
            await handle_stripe_check(chat_id, parts[1], parts[2], cb_id, msg_id)

    elif "message" in data:
        chat_id = data["message"]["chat"]["id"]
        if chat_id not in VERIFIED_ADULTS:
            await send_age_gate(chat_id)
        else:
            await handle_start(chat_id)

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
