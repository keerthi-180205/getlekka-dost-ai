"""
main.py - the web server that Meta talks to.

GetLekka: a shop owner's credit book on WhatsApp. This file is only the DOOR:
it receives what Meta sends, hands it to pipeline.py (the brain), and sends back
the replies. It contains no business rules.

Two doors (endpoints), both at /webhook:
  GET  /webhook  -> Meta checks "is this URL really yours?" (one-time setup)
  POST /webhook  -> Meta delivers every new WhatsApp message here

Run it with:  uvicorn app.main:app --reload --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, Response

import traceback

from app import audio_store, config, db, ledger, pipeline, replies, whatsapp


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # make sure the database table exists when the server starts
    yield


app = FastAPI(title="GetLekka", lifespan=lifespan)


# ---------- Door 1: Meta's one-time verification ----------
@app.get("/webhook")
def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    """
    Meta sends: ?hub.mode=subscribe&hub.verify_token=YOUR_WORD&hub.challenge=12345
    If the word matches our VERIFY_TOKEN, we send back the challenge number.
    That proves to Meta that this server belongs to us.
    """
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        print("[webhook] verified by Meta")
        return PlainTextResponse(challenge)
    return Response(status_code=403)  # wrong word -> refuse


# ---------- Door 2: every incoming message ----------
@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks):
    """
    Meta's JSON is deeply nested: entry -> changes -> value -> messages.
    We pull out each message and handle it in the BACKGROUND,
    so we can answer Meta with "200 OK" immediately.
    (If we are slow, Meta thinks we failed and sends the message again.)
    """
    data = await request.json()

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Note: Meta also sends "statuses" (delivered / read ticks).
            # Those have no "messages" key, so this loop simply skips them.
            for message in value.get("messages", []):
                background.add_task(handle_message, message)

    return {"status": "ok"}


def handle_message(message: dict) -> None:
    """
    One message from the owner: a text, a voice note, or a button tap.
    Skip duplicates, work out the replies (pipeline.py), send them.
    """
    wa_msg_id = message["id"]
    phone = message["from"]
    msg_type = message.get("type")
    body = message.get("text", {}).get("body") if msg_type == "text" else None

    print(f"[in] {phone} ({msg_type}): {body}")

    # Skip duplicates (Meta retries)
    if not db.save_incoming(wa_msg_id, phone, msg_type, body):
        print(f"[skip] duplicate message {wa_msg_id}")
        return

    shop_id = ledger.get_or_create_shop(phone)   # the owner's phone number IS the shop
    message_id = db.get_message_id(wa_msg_id)

    try:
        out = _work_out_replies(shop_id, message, msg_type, body, wa_msg_id, message_id)
    except Exception:  # noqa: BLE001 - last safety net: the owner must always get an answer
        # (This runs in the background; an uncaught error would be invisible and silent.)
        traceback.print_exc()
        out = [replies.Reply(replies.TECH_PROBLEM)]

    for reply in out:
        _deliver(phone, reply)


def _work_out_replies(shop_id, message, msg_type, body, wa_msg_id, message_id) -> list[replies.Reply]:
    """Pick the right pipeline function for this kind of message."""
    if msg_type == "text":
        return pipeline.handle_text(shop_id, body or "", message_id=message_id)

    if msg_type == "audio":
        data, mime_type = whatsapp.download_media(message.get("audio", {}).get("id", ""))
        path = audio_store.save_audio(wa_msg_id, data, mime_type)
        return pipeline.handle_voice(shop_id, path, message_id=message_id)

    if msg_type == "interactive":       # the owner tapped a button
        tapped = message.get("interactive", {}).get("button_reply")
        if tapped:
            return pipeline.handle_button(shop_id, tapped.get("id", ""))

    return [replies.Reply(replies.UNSUPPORTED)]


def _deliver(phone: str, reply: replies.Reply) -> None:
    """Send one reply (with its buttons, if any) and save it in the database."""
    buttons = [{"id": b.id, "title": b.title} for b in reply.buttons]
    text = reply.text

    if buttons and len(text) > replies.BODY_WITH_BUTTONS_MAX:
        # WhatsApp allows only 1024 characters above buttons. Send the long text
        # as an ordinary message, then the buttons under a short line.
        out_id = whatsapp.send_text(phone, text)
        db.save_outgoing(out_id, phone, "text", text)
        text = replies.PRESS_BELOW

    if buttons:
        out_id = whatsapp.send_buttons(phone, text, buttons)
        db.save_outgoing(out_id, phone, "interactive", text, buttons)
    else:
        out_id = whatsapp.send_text(phone, text)
        db.save_outgoing(out_id, phone, "text", text)
    print(f"[out] {phone}: {text}")
