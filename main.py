import os
import asyncio
import html
import traceback
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

# ── Produkty z wysyłką ──
ITEMS = {
    "skarpetki": {"label": "Skarpetki - 60zł", "button": "🧦 Skarpetki - 60zł", "price": 60},
    "majtki": {"label": "Majtki - 90zł", "button": "🩲 Majtki - 90zł", "price": 90},
}

TIPPLY_LINK = "https://tipply.pl/@olcia_020"

# Nagranie z poradnikiem InPost (file_id z Telegrama albo bezpośredni URL).
# Ustaw w zmiennej środowiskowej INPOST_GUIDE_VIDEO.
# Jak zdobyć file_id: wyślij nagranie do bota z konta właściciela - bot odpowie file_id.
def guide_video() -> str:
    return os.environ.get("INPOST_GUIDE_VIDEO", "")


# ── Stan użytkowników (w pamięci) ──
# STATE[chat_id] = {"step": str, "price": str | None, "item": str | None, "file_id": str | None}
STATE: dict[int, dict[str, Any]] = {}

# kroki, w których bot czeka na zdjęcie
WAIT_STEPS = {"ss_wait", "ss_ready", "qr_wait", "qr_ready"}

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


async def send_photo(chat_id: int, file_id: str, caption: str, reply_markup: dict | None = None):
    body: dict[str, Any] = {"chat_id": chat_id, "photo": file_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("sendPhoto"), json=body)


async def send_video(chat_id: int, video: str, caption: str, reply_markup: dict | None = None):
    if not video:
        # brak nagrania - wyślij sam tekst, żeby użytkownik nie utknął
        await send_message(chat_id, caption + "\n\n<i>(Nagranie zostanie dodane wkrótce)</i>", reply_markup)
        return
    body: dict[str, Any] = {"chat_id": chat_id, "video": video, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as c:
        await c.post(bot_url("sendVideo"), json=body)


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
    rows = [
        [{"text": pkg["label"], "callback_data": f"pkg:{price}"}]
        for price, pkg in PACKAGES.items()
    ]
    for key, item in ITEMS.items():
        rows.append([{"text": item["button"], "callback_data": f"item:{key}"}])
    return {"inline_keyboard": rows}


def pay_keyboard(price: str):
    return {"inline_keyboard": [[{"text": "💸 Zapłać", "callback_data": f"pay:{price}"}]]}


def next_keyboard(price: str):
    return {"inline_keyboard": [[{"text": "➡️ Przejdź dalej", "callback_data": f"go:{price}"}]]}


def ss_ok_keyboard(price: str):
    return {"inline_keyboard": [[{"text": "✅ OK", "callback_data": f"ssok:{price}"}]]}


def admin_keyboard(user_chat_id: int, price: str):
    return {
        "inline_keyboard": [
            [{"text": "✅ Potwierdź", "callback_data": f"ok:{user_chat_id}:{price}"}],
            [{"text": "❌ Odrzuć", "callback_data": f"no:{user_chat_id}"}],
        ]
    }


# -- klawiatury ścieżki z wysyłką --
def ship_keyboard(item: str):
    return {"inline_keyboard": [[{"text": "📦 Wysyłka do paczkomatu", "callback_data": f"ship:{item}"}]]}


def guide_keyboard(item: str):
    return {"inline_keyboard": [[{"text": "📲 Jak nadać paczkę w aplikacji InPost", "callback_data": f"guide:{item}"}]]}


def done_keyboard(item: str):
    return {"inline_keyboard": [[{"text": "✅ Zrobione", "callback_data": f"done:{item}"}]]}


def qr_pay_keyboard(item: str):
    return {"inline_keyboard": [[{"text": "💸 Zapłać", "callback_data": f"qrpay:{item}"}]]}


def qr_paid_keyboard(item: str):
    return {"inline_keyboard": [[{"text": "✅ Zapłaciłem", "callback_data": f"qrpaid:{item}"}]]}


# ── Helpers ──
def user_display(cb_from: dict) -> str:
    name = cb_from.get("first_name", "user")
    uname = cb_from.get("username")
    text = html.escape(name)
    if uname:
        text += f" (@{html.escape(uname)})"
    return text


def extract_photo_id(msg: dict) -> str | None:
    photos = msg.get("photo")
    if photos:
        return photos[-1]["file_id"]  # największa rozdzielczość
    return None


async def expired(chat_id: int, cb_id: str):
    await answer_callback(cb_id, "Sesja wygasła")
    await send_message(chat_id, "⚠️ Ta sesja wygasła. Wpisz /start i zacznij od nowa.")


# ── Handlers: start ──
async def handle_start(chat_id: int):
    STATE.pop(chat_id, None)
    await send_message(chat_id, "💿 Hejka!\nWybierz pakiet lub produkt:", welcome_keyboard())


# ── Handlers: pakiety ──
async def handle_package(chat_id, price, cb_id, msg_id):
    if price not in PACKAGES:
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id, f"Wybrałeś pakiet za {price}zł")
    await remove_buttons(chat_id, msg_id)
    STATE[chat_id] = {"step": "pkg_selected", "price": price, "item": None, "file_id": None}
    pkg = PACKAGES[price]
    await send_message(chat_id, f"Pakiet: {pkg['label']}\n\nZapłać czym tylko chcesz:", pay_keyboard(price))


async def handle_pay(chat_id, price, cb_id, msg_id):
    st = STATE.get(chat_id)
    if price not in PACKAGES or not st or st.get("price") != price or st["step"] != "pkg_selected":
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "pkg_paying"
    pkg = PACKAGES[price]
    text = (
        f"💸 Płatność\n\n"
        f"Pakiet: {pkg['label']}\n\n"
        f"Zapłać tutaj:\n{TIPPLY_LINK}\n\n"
        f"Po zapłaceniu pojawi się wiadomość <b>„Płatność zakończona sukcesem”</b>. "
        f"Zrób zrzut ekranu tej wiadomości - będzie potrzebny w następnym kroku.\n\n"
        f"Po dokonaniu płatności kliknij przycisk:"
    )
    await send_message(chat_id, text, next_keyboard(price))


async def handle_go(chat_id, price, cb_id, msg_id):
    st = STATE.get(chat_id)
    if price not in PACKAGES or not st or st.get("price") != price or st["step"] != "pkg_paying":
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "ss_wait"
    st["file_id"] = None
    await send_message(
        chat_id,
        "📸 Wyślij teraz <b>zrzut ekranu</b> z wiadomością „Płatność zakończona sukcesem”.\n\n"
        "Wyślij go jako zdjęcie (nie jako plik). Nie przejdziesz dalej, dopóki go nie wyślesz.",
    )


async def handle_ss_ok(chat_id, price, cb_id, username, msg_id):
    st = STATE.get(chat_id)
    if (
        price not in PACKAGES
        or not st
        or st.get("price") != price
        or st["step"] != "ss_ready"
        or not st.get("file_id")
    ):
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    file_id = st["file_id"]
    STATE.pop(chat_id, None)
    await send_message(chat_id, "⏳ Czekaj na weryfikację...")
    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    pkg = PACKAGES[price]
    caption = f"💰 Nowa płatność do weryfikacji\n\nUżytkownik: {username}\nPakiet: {pkg['label']}\n\nCzy potwierdzasz?"
    await send_photo(owner_id, file_id, caption, admin_keyboard(chat_id, price))


# ── Handlers: admin ──
async def handle_confirm(user_chat_id, price, cb_id, admin_chat_id, msg_id):
    if price not in PACKAGES:
        return await answer_callback(cb_id, "Błędny pakiet")
    await answer_callback(cb_id, "Płatność potwierdzona ✅")
    link = PACKAGES[price]["link"]
    await send_message(user_chat_id, f"✅ Płatność potwierdzona!\n\nOto Twój dostęp:\n{link}")
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


async def handle_reject(user_chat_id, cb_id, admin_chat_id, msg_id):
    await answer_callback(cb_id, "Płatność odrzucona ❌")
    await send_message(user_chat_id, "❌ Płatność nie została potwierdzona.\n\nSkontaktuj się z administratorem lub spróbuj ponownie.")
    asyncio.create_task(delayed_remove_buttons(admin_chat_id, msg_id, 300))


# ── Handlers: skarpetki / majtki ──
async def handle_item(chat_id, item, cb_id, msg_id):
    if item not in ITEMS:
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id, f"Wybrałeś: {ITEMS[item]['label']}")
    await remove_buttons(chat_id, msg_id)
    STATE[chat_id] = {"step": "item_selected", "price": None, "item": item, "file_id": None}
    await send_message(
        chat_id,
        f"Wybrałeś: <b>{ITEMS[item]['label']}</b>\n\nWybierz sposób dostawy:",
        ship_keyboard(item),
    )


async def handle_ship(chat_id, item, cb_id, msg_id):
    st = STATE.get(chat_id)
    if item not in ITEMS or not st or st.get("item") != item or st["step"] != "item_selected":
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "shipping_info"
    text = (
        "📦 <b>Wysyłka do paczkomatu</b>\n\n"
        "1️⃣ Nadaj paczkę w aplikacji InPost.\n"
        "2️⃣ Wyślij tutaj zdjęcie <b>kodu QR z nadania paczki</b>.\n\n"
        "⚠️ <b>Uwaga:</b> paczkę trzeba opłacić (koszt nadania) w aplikacji InPost.\n\n"
        "Poniżej znajdziesz poradnik, jak nadać paczkę w aplikacji InPost:"
    )
    await send_message(chat_id, text, guide_keyboard(item))


async def handle_guide(chat_id, item, cb_id, msg_id):
    st = STATE.get(chat_id)
    if item not in ITEMS or not st or st.get("item") != item or st["step"] != "shipping_info":
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "guide_sent"
    await send_video(
        chat_id,
        guide_video(),
        "📲 <b>Jak nadać paczkę w aplikacji InPost</b>\n\nObejrzyj nagranie, nadaj paczkę, a potem kliknij „Zrobione”.",
        done_keyboard(item),
    )


async def handle_done(chat_id, item, cb_id, msg_id):
    st = STATE.get(chat_id)
    if item not in ITEMS or not st or st.get("item") != item or st["step"] != "guide_sent":
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "qr_wait"
    st["file_id"] = None
    await send_message(
        chat_id,
        "📸 Wyślij teraz <b>zdjęcie kodu QR z nadania paczki</b>.\n\n"
        "Wyślij je jako zdjęcie (nie jako plik). Nie przejdziesz dalej, dopóki go nie wyślesz.",
    )


async def handle_qr_pay(chat_id, item, cb_id, msg_id):
    st = STATE.get(chat_id)
    if (
        item not in ITEMS
        or not st
        or st.get("item") != item
        or st["step"] != "qr_ready"
        or not st.get("file_id")
    ):
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    st["step"] = "qr_paying"
    text = (
        f"💸 Płatność\n\n"
        f"Produkt: {ITEMS[item]['label']}\n\n"
        f"Zapłać tutaj:\n{TIPPLY_LINK}\n\n"
        f"Po dokonaniu płatności kliknij przycisk poniżej:"
    )
    await send_message(chat_id, text, qr_paid_keyboard(item))


async def handle_qr_paid(chat_id, item, cb_id, username, msg_id):
    st = STATE.get(chat_id)
    if (
        item not in ITEMS
        or not st
        or st.get("item") != item
        or st["step"] != "qr_paying"
        or not st.get("file_id")
    ):
        return await expired(chat_id, cb_id)
    await answer_callback(cb_id)
    await remove_buttons(chat_id, msg_id)
    file_id = st["file_id"]
    STATE.pop(chat_id, None)
    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    caption = (
        f"📦 Nowe zamówienie z wysyłką\n\n"
        f"Użytkownik: {username}\n"
        f"Produkt: {ITEMS[item]['label']}\n"
        f"Chat ID: {chat_id}\n\n"
        f"Poniżej kod QR z nadania paczki."
    )
    await send_photo(owner_id, file_id, caption)
    await send_message(
        chat_id,
        "✅ <b>Płatność potwierdzona!</b>\n\nPaczka zostanie nadana nie dłużej niż do 5 dni roboczych.",
    )


# ── Handler: wiadomości (zdjęcia / tekst) ──
async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "") or ""

    # Właściciel wysyła nagranie -> bot zwraca file_id (do INPOST_GUIDE_VIDEO)
    owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])
    if chat_id == owner_id and (msg.get("video") or msg.get("document")):
        media = msg.get("video") or msg.get("document")
        await send_message(
            chat_id,
            "file_id nagrania:\n<code>" + html.escape(media["file_id"]) + "</code>\n\n"
            "Ustaw go w zmiennej środowiskowej INPOST_GUIDE_VIDEO.",
        )
        return

    if text.startswith("/start"):
        await handle_start(chat_id)
        return

    st = STATE.get(chat_id)
    if st and st["step"] in WAIT_STEPS:
        file_id = extract_photo_id(msg)
        if not file_id:
            await send_message(chat_id, "⚠️ Musisz wysłać <b>zdjęcie</b> (nie tekst, wideo ani plik), żeby przejść dalej.")
            return

        if st["step"] in ("ss_wait", "ss_ready"):
            st["file_id"] = file_id
            st["step"] = "ss_ready"
            await send_message(
                chat_id,
                "✅ Zdjęcie otrzymane.\n\nKliknij OK, aby wysłać je do weryfikacji:",
                ss_ok_keyboard(st["price"]),
            )
        else:  # qr_wait / qr_ready
            st["file_id"] = file_id
            st["step"] = "qr_ready"
            await send_message(
                chat_id,
                "✅ Zdjęcie kodu QR otrzymane.\n\nPamiętaj, że paczkę trzeba opłacić. Kliknij Zapłać:",
                qr_pay_keyboard(st["item"]),
            )
        return

    # każda inna wiadomość -> menu startowe
    await handle_start(chat_id)


# ── Webhook ──
@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()

        if "callback_query" in data:
            cb = data["callback_query"]
            if not cb.get("message"):
                return {"ok": True}
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            cb_id = cb["id"]
            username = user_display(cb["from"])
            d = cb.get("data", "")
            parts = d.split(":")
            action = parts[0]
            owner_id = int(os.environ["TELEGRAM_OWNER_CHAT_ID"])

            # przyciski admina - tylko dla właściciela
            if action in ("ok", "no"):
                if chat_id != owner_id:
                    await answer_callback(cb_id, "Brak uprawnień")
                    return {"ok": True}
                if action == "ok" and len(parts) == 3:
                    await handle_confirm(int(parts[1]), parts[2], cb_id, chat_id, msg_id)
                elif action == "no" and len(parts) == 2:
                    await handle_reject(int(parts[1]), cb_id, chat_id, msg_id)

            # pakiety
            elif action == "pkg" and len(parts) == 2:
                await handle_package(chat_id, parts[1], cb_id, msg_id)
            elif action == "pay" and len(parts) == 2:
                await handle_pay(chat_id, parts[1], cb_id, msg_id)
            elif action == "go" and len(parts) == 2:
                await handle_go(chat_id, parts[1], cb_id, msg_id)
            elif action == "ssok" and len(parts) == 2:
                await handle_ss_ok(chat_id, parts[1], cb_id, username, msg_id)

            # skarpetki / majtki
            elif action == "item" and len(parts) == 2:
                await handle_item(chat_id, parts[1], cb_id, msg_id)
            elif action == "ship" and len(parts) == 2:
                await handle_ship(chat_id, parts[1], cb_id, msg_id)
            elif action == "guide" and len(parts) == 2:
                await handle_guide(chat_id, parts[1], cb_id, msg_id)
            elif action == "done" and len(parts) == 2:
                await handle_done(chat_id, parts[1], cb_id, msg_id)
            elif action == "qrpay" and len(parts) == 2:
                await handle_qr_pay(chat_id, parts[1], cb_id, msg_id)
            elif action == "qrpaid" and len(parts) == 2:
                await handle_qr_paid(chat_id, parts[1], cb_id, username, msg_id)
            else:
                await answer_callback(cb_id)

        elif "message" in data:
            await handle_message(data["message"])

    except Exception:
        traceback.print_exc()

    return {"ok": True}


# ── Run ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
