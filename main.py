import os
import re
import json
import random
import string
import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, Request
import uvicorn

# ── Konfiguracja Tipply ──
TIPPLY_LINK = os.environ.get("TIPPLY_LINK", "https://tipply.pl/@olcia_020")
# Sekret dodawany do URL webhooka jako zabezpieczenie przed fałszywymi zgłoszeniami wpłat.
# Jeśli Tipply nie pozwala dopisać sekretu do URL, ustaw go pusty i poszukaj innej metody
# weryfikacji (np. nagłówka/podpisu), jeśli Tipply coś takiego udostępnia.
TIPPLY_WEBHOOK_SECRET = os.environ.get("TIPPLY_WEBHOOK_SECRET", "")

# ── Konfiguracja pakietów ──
PACKAGES = {
    "20": {"label": "20zł - 2 zdjęcia i 1 film", "link": "https://mega.nz/folder/YUpkFbbR#zf6yaH--NH24zUq7aGs4cg"},
    "40": {"label": "40zł - 4 zdjęcia i 2 filmy", "link": "https://mega.nz/folder/RBwTBAyA#qMyQy7VbRNRba8dku4Z34w"},
    "60": {"label": "60zł - 6 zdjęć i 4 filmy", "link": "https://mega.nz/folder/lRRhTQDR#6a6q_58Tchz3RC4GzdPvew"},
    "80": {"label": "80zł - 10 zdjęć i 6 filmów", "link": "https://mega.nz/folder/JZ5CUJSQ#HQyJkLLqmWIupXi9frnKvw"},
    "100": {"label": "100zł - 20 zdjęć i 10 filmów", "link": "https://mega.nz/folder/wVgQHApI#Z8k-PSDN-fqeU_4DYjKenQ"},
    "350": {"label": "350zł - Cały folder (60GB)", "link": "https://mega.nz/folder/wIg2QLrD#xzaf8oGVHJUvgaEiLFPyPg"},
}

# ── Przechowywanie zamówień ──
# Prosty plik JSON jako trwałość między restartami (na Railway bez podpiętego
# volume dysk jest efemeryczny przy nowym deployu — dla większej pewności
# docelowo lepiej użyć bazy danych, ale na start to wystarczy).
ORDERS_FILE = "orders.json"


def load_orders() -> dict:
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_orders():
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(ORDERS, f, ensure_ascii=False, indent=2)


ORDERS: dict[str, dict] = load_orders()

app = FastAPI()


def generate_order_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=6))
        if code not in ORDERS:
            return code


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
    await asyncio.sleep(delay)
    await remove_buttons(chat_id, message_id)


# ── Klawiatury ──
def pay_keyboard():
    return {"inline_keyboard": [[{"text": "💸 Zapłać", "callback_data": "pay"}]]}


def admin_choose_package_keyboard(code: str):
    rows = [[{"text": pkg["label"], "callback_data": f"ok2:{code}:{price}"}] for price, pkg in PACKAGES.items()]
    rows.append([{"text": "❌ Odrzuć", "callback_data": f"no2:{code}"}])
    return {"inline_keyboard": rows}


# ── Handlery Telegram ──
async def handle_start(chat_id: int):
    await send_message(chat_id, "💿 Hejka!", pay_keyboard())


async def handle_pay(chat_id: int, cb_id: str, username: str, msg_id: int):
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)

    code = generate_order_code()
    ORDERS[code] = {"chat_id": chat_id, "username": username, "status": "pending"}
    save_orders()

    text = (
        f"💸 Zapłać czym tylko chcesz:\n{TIPPLY_LINK}\n\n"
        f"⚠️ W polu <b>Wiadomość</b> na Tipply wpisz koniecznie ten kod:\n"
        f"<code>{code}</code>\n\n"
        f"Bez kodu nie będziemy w stanie automatycznie przypisać wpłaty do Ciebie.\n"
        f"Po zaksięgowaniu wpłaty dostęp zostanie wysłany tutaj."
    )
    await send_message(chat_id, text)


async def handle_admin_confirm(code: str, price: str, cb_id: str, admin_chat_id: int, msg_id: int):
    await answer_callback(cb_id, "Potwierdzone ✅")
    order = ORDERS.get(code)
    if not order:
        await send_message(admin_chat_id, f"⚠️ Nie znaleziono zamówienia o kodzie {code}.")
        return
    order["status"] = "fulfilled"
    save_orders()
    link = PACKAGES[price]["link"]
    await send_message(order["chat_id"], f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


async def handle_admin_reject(code: str, cb_id: str, admin_chat_id: int, msg_id: int):
    await answer_callback(cb_id, "Odrzucone ❌")
    order = ORDERS.get(code)
    if order:
        order["status"] = "rejected"
        save_orders()
        await send_message(
            order["chat_id"],
            "❌ Płatność nie została potwierdzona.\n\nSkontaktuj się z administratorem lub spróbuj ponownie.",
        )
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


# ── Webhook Telegram ──
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        cb_id = cb["id"]
        username = cb["from"].get("first_name", "user")
        d = cb.get("data", "")

        if d == "pay":
            await handle_pay(chat_id, cb_id, username, msg_id)
        elif d.startswith("ok2:"):
            parts = d.split(":")
            await handle_admin_confirm(parts[1], parts[2], cb_id, chat_id, msg_id)
        elif d.startswith("no2:"):
            await handle_admin_reject(d.split(":")[1], cb_id, chat_id, msg_id)

    elif "message" in data:
        chat_id = data["message"]["chat"]["id"]
        await handle_start(chat_id)

    return {"ok": True}


# ── Webhook Tipply (wpłaty) ──
def extract_field(payload: dict, keys: list[str]):
    for k in keys:
        if k in payload and payload[k] not in (None, ""):
            return payload[k]
    # niektóre platformy zagnieżdżają dane w "data" lub "donation"
    for wrapper in ("data", "donation", "payload"):
        if isinstance(payload.get(wrapper), dict):
            for k in keys:
                if k in payload[wrapper] and payload[wrapper][k] not in (None, ""):
                    return payload[wrapper][k]
    return None


def normalize_amount(raw) -> str | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).replace(",", ".").replace("zł", "").strip())
    except ValueError:
        return None
    if value == int(value):
        return str(int(value))
    return str(value)


@app.post("/tipply-webhook")
async def tipply_webhook(request: Request, secret: str = ""):
    # Podstawowa ochrona przed fałszywymi zgłoszeniami. Ustaw TIPPLY_WEBHOOK_SECRET
    # i skonfiguruj URL webhooka w Tipply jako .../tipply-webhook?secret=TWOJ_SEKRET
    if TIPPLY_WEBHOOK_SECRET and secret != TIPPLY_WEBHOOK_SECRET:
        return {"ok": False, "error": "invalid secret"}

    payload = await request.json()

    nick = extract_field(payload, ["nick", "name", "username", "donor", "from"]) or "nieznany"
    raw_amount = extract_field(payload, ["amount", "kwota", "value", "sum"])
    message = extract_field(payload, ["message", "wiadomosc", "wiadomość", "comment", "description"]) or ""
    amount = normalize_amount(raw_amount)

    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])

    # szukamy w wiadomości 6-znakowego kodu zamówienia pasującego do zapisanych kodów
    found_code = None
    for candidate in re.findall(r"[A-Za-z0-9]{6}", message):
        candidate = candidate.upper()
        if candidate in ORDERS:
            found_code = candidate
            break

    code_display = found_code or "nierozpoznany"
    base_text = (
        f"💸 Nowa wpłata\n"
        f"Nick: {nick}\n"
        f"Kwota: {amount or raw_amount} zł\n"
        f"Wiadomość: {message}\n"
        f"Kod zamówienia: {code_display}"
    )

    if not found_code:
        # brak rozpoznanego kodu — tylko powiadomienie, bez automatycznej akcji
        await send_message(owner_id, base_text + "\n\n⚠️ Nie udało się dopasować kodu do żadnego zamówienia.")
        return {"ok": True}

    order = ORDERS[found_code]
    if order["status"] != "pending":
        await send_message(owner_id, base_text + f"\n\nℹ️ To zamówienie ma już status: {order['status']}.")
        return {"ok": True}

    if amount in PACKAGES:
        # kwota pasuje do konkretnego pakietu -> automatyczna wysyłka linku
        order["status"] = "fulfilled"
        save_orders()
        link = PACKAGES[amount]["link"]
        await send_message(order["chat_id"], f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
        await send_message(owner_id, base_text + f"\n\n✅ Automatycznie wysłano link (pakiet {amount}zł).")
    else:
        # nietypowa kwota -> tylko powiadomienie + ręczne potwierdzenie/odrzucenie
        await send_message(
            owner_id,
            base_text + "\n\n❓ Kwota nie pasuje do żadnego pakietu — potwierdź ręcznie i wybierz pakiet:",
            admin_choose_package_keyboard(found_code),
        )

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
