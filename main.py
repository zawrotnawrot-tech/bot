import os
import re
import base64
import asyncio
import json
from datetime import datetime
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

# ── Weryfikacja wieku (tylko deklaracja, bez sprawdzania dowodem) ──
VERIFIED_ADULTS: set[int] = set()

# ── Historia sprzedaży ──
HISTORY_FILE = "history.json"

# Tymczasowo trzyma dane o oczekującej płatności (do momentu potwierdzenia przez admina).
PENDING_SALES: dict[int, dict[str, str]] = {}

# Trzyma chat_id użytkowników, od których bot oczekuje wklejenia kodu paysafecard,
# wraz z ceną wybranego pakietu.
AWAITING_PSC_CODE: dict[int, str] = {}

PSC_CODE_PATTERN = re.compile(r"^\d{16}$")


def load_history() -> dict[str, Any]:
    if not os.path.exists(HISTORY_FILE):
        data = {"sales": []}
        save_history(data)
        return data
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        data = {"sales": []}
        save_history(data)
        return data


def save_history(data: dict[str, Any]) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_sale(price: str, telegram_id: int, username: str) -> None:
    now = datetime.now()
    history = load_history()
    history["sales"].append(
        {
            "date": now.strftime("%Y-%m-%d %H:%M:%S"),
            "month": now.strftime("%Y-%m"),
            "package": PACKAGES[price]["label"],
            "price": price,
            "telegram_id": telegram_id,
            "username": username,
        }
    )
    save_history(history)


def get_current_month_summary() -> tuple[dict[str, int], int]:
    current_month = datetime.now().strftime("%Y-%m")
    history = load_history()
    counts = {price: 0 for price in PACKAGES}
    total = 0
    for sale in history["sales"]:
        if sale.get("month") == current_month and sale.get("price") in counts:
            counts[sale["price"]] += 1
            total += 1
    return counts, total


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


async def delete_message(chat_id: int, message_id: int):
    """Usuwa wiadomość z czatu (np. żeby kod PSC nie zalegał w historii)."""
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("deleteMessage"), json={"chat_id": chat_id, "message_id": message_id})


async def delayed_remove_buttons(chat_id: int, message_id: int, delay: int = 300):
    await asyncio.sleep(delay)
    await remove_buttons(chat_id, message_id)


async def delayed_delete_message(chat_id: int, message_id: int, delay: int = 300):
    """Po czasie delay usuwa całą wiadomość (np. u admina, bo zawiera kod PSC)."""
    await asyncio.sleep(delay)
    await delete_message(chat_id, message_id)


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
            [{"text": "🔢 Kod BLIK", "callback_data": f"blik:{price}"}],
            [{"text": "🎫 Paysafecard", "callback_data": f"psc:{price}"}],
            [{"text": "💰 PayPal", "callback_data": f"pp:{price}"}],
        ]
    }


def psc_force_reply():
    return {
        "force_reply": True,
        "input_field_placeholder": "Kod PSC (16 cyfr)",
    }


def paypal_buttons(order_id: str, price: str, approve_url: str):
    return {
        "inline_keyboard": [
            [{"text": "💳 Zapłać przez PayPal", "url": approve_url}],
            [{"text": "✅ Zapłaciłem (PayPal)", "callback_data": f"ppck:{order_id}:{price}"}],
        ]
    }


def admin_keyboard(user_chat_id: int, price: str):
    return {
        "inline_keyboard": [
            [{"text": "✅ Potwierdź", "callback_data": f"ok:{user_chat_id}:{price}"}],
            [{"text": "❌ Odrzuć", "callback_data": f"no:{user_chat_id}"}],
        ]
    }


# ── Handlers: weryfikacja wieku ──
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


# ── Handlers: sprzedaż / płatności ──
async def handle_start(chat_id: int):
    await send_message(chat_id, "💿 Hejka!\nWybierz pakiet:", welcome_keyboard())


async def handle_package(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id, f"Wybrałeś pakiet za {price}zł")
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    await send_message(chat_id, f"Pakiet: {pkg['label']}\n\nWybierz metodę płatności:", payment_method_keyboard(price))


async def handle_blik_start(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    text = (
        f"🔢 Płatność kod BLIK\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Wygeneruj kod BLIK w swojej aplikacji bankowej i wyślij go tutaj:\n"
        f"https://t.me/olix_303"
    )
    await send_message(chat_id, text)


async def handle_psc_start(chat_id, price, cb_id, msg_id):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    pkg = PACKAGES[price]
    AWAITING_PSC_CODE[chat_id] = price
    text = (
        f"🎫 Płatność Paysafecard\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Kup kod PSC o wartości {price} zł, a następnie odpowiedz na tę wiadomość, "
        f"wklejając kod (dokładnie 16 cyfr, bez spacji i liter)."
    )
    await send_message(chat_id, text, psc_force_reply())


async def handle_psc_code(chat_id: int, msg_id: int, username: str, text: str):
    price = AWAITING_PSC_CODE.get(chat_id)
    if price is None:
        return  # bot nie oczekuje teraz kodu od tego użytkownika

    code = text.strip().replace(" ", "")

    if not PSC_CODE_PATTERN.fullmatch(code):
        await send_message(
            chat_id,
            "⚠️ Kod musi składać się z dokładnie 16 cyfr (same liczby, bez spacji i liter).\n"
            "Wklej poprawny kod, odpowiadając na poprzednią wiadomość:",
        )
        return

    del AWAITING_PSC_CODE[chat_id]
    await send_message(chat_id, "⏳ Czekaj na weryfikację...")

    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    pkg = PACKAGES[price]
    PENDING_SALES[chat_id] = {"username": username, "price": price}

    admin_info_text = (
        f"🎫 Nowa płatność PSC do weryfikacji\n\n"
        f"Użytkownik: {username}\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Kod poniżej (osobna wiadomość — łatwo skopiować):"
    )
    await send_message(owner_id, admin_info_text)

    # Kod w osobnej wiadomości, żeby jedno tapnięcie/kliknięcie kopiowało tylko jego,
    # bez żadnego dodatkowego tekstu. Przyciski potwierdzenia są przy tej wiadomości.
    await send_message(owner_id, f"<code>{code}</code>", admin_keyboard(chat_id, price))

    # Usuń wiadomość z kodem z czatu użytkownika, żeby nie zalegała w historii.
    await delete_message(chat_id, msg_id)


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


async def handle_confirm(user_chat_id, price, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność potwierdzona ✅")
    link = PACKAGES[price]["link"]
    await send_message(user_chat_id, f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")

    pending = PENDING_SALES.pop(user_chat_id, None)
    username = pending["username"] if pending and pending.get("price") == price else "unknown"
    record_sale(price, user_chat_id, username)

    # Wiadomość u admina może zawierać kod PSC — usuń ją po chwili zamiast tylko chować przyciski.
    asyncio.create_task(delayed_delete_message(admin_chat_id, msg_id, 300))


async def handle_reject(user_chat_id, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność odrzucona ❌")
    await send_message(user_chat_id, "❌ Płatność nie została potwierdzona.\n\nSkontaktuj się z administratorem lub spróbuj ponownie.")
    PENDING_SALES.pop(user_chat_id, None)
    asyncio.create_task(delayed_delete_message(admin_chat_id, msg_id, 300))


# ── Handler: /history ──
async def handle_history_command(chat_id: int):
    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    if chat_id != owner_id:
        return

    counts, total = get_current_month_summary()
    lines = ["📊 Historia sprzedaży", "", "Bieżący miesiąc:"]
    for price in PACKAGES:
        lines.append(f"{price} zł - {counts[price]}")
    lines.append("")
    lines.append("Łącznie:")
    lines.append(f"{total} sprzedaży")
    await send_message(chat_id, "\n".join(lines))


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
        elif d.startswith("blik:"):
            await handle_blik_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("psc:"):
            await handle_psc_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("pp:"):
            await handle_paypal_start(chat_id, d.split(":")[1], cb_id, msg_id)
        elif d.startswith("ppck:"):
            parts = d.split(":")
            await handle_paypal_check(chat_id, parts[1], parts[2], cb_id, msg_id)
        elif d.startswith("ok:"):
            parts = d.split(":")
            await handle_confirm(int(parts[1]), parts[2], cb_id, chat_id, msg_id)
        elif d.startswith("no:"):
            await handle_reject(int(d.split(":")[1]), cb_id, chat_id, msg_id)

    elif "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        msg_id = msg["message_id"]
        text = msg.get("text", "")
        username = msg.get("from", {}).get("first_name", "user")

        if text == "/history":
            await handle_history_command(chat_id)
        elif chat_id not in VERIFIED_ADULTS:
            await send_age_gate(chat_id)
        elif chat_id in AWAITING_PSC_CODE:
            await handle_psc_code(chat_id, msg_id, username, text)
        else:
            await handle_start(chat_id)

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
