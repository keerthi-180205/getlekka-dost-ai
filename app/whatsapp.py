"""
whatsapp.py - talks to Meta's WhatsApp Cloud API: sends messages, downloads voice notes.

How sending works: we make an HTTP POST request to Meta with
  - WHO to send to (phone number)
  - WHAT to send (a text, or a text with tap-able buttons)
  - our access token (proof that we are allowed to send)
Meta then delivers it to the person's WhatsApp.

How a voice note arrives: Meta's webhook only gives us a media ID. We ask Meta
for a temporary download link for that ID, then download the file from it
(both requests carry our token).

SIMULATE mode (SIMULATE=true in .env) never calls Meta: replies are only saved
in the database for simulate.py to show, and a media ID written as
"sim:/path/to/file.ogg" is read from your own disk. This is for practice only.
"""
import mimetypes
import uuid
from pathlib import Path

import httpx

from app import config

API_URL = (
    f"https://graph.facebook.com/{config.GRAPH_API_VERSION}"
    f"/{config.PHONE_NUMBER_ID}/messages"
)
MEDIA_URL = f"https://graph.facebook.com/{config.GRAPH_API_VERSION}"


class MediaError(Exception):
    """A voice note could not be downloaded (clear message inside)."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"}


def _post_message(payload: dict) -> str | None:
    """
    Send one message payload to Meta.
    Returns WhatsApp's message ID if it worked, or None if it failed.
    """
    if config.SIMULATE:
        # Don't call Meta. main.py still saves this reply in the database,
        # and simulate.py reads it from there and shows it to you.
        return f"sim.{uuid.uuid4().hex}"

    response = httpx.post(API_URL, json=payload, headers=_headers(), timeout=15)
    if response.status_code != 200:
        # Print Meta's error so we can see what went wrong
        # (most common: expired token, or number not added as a test recipient)
        print(f"[send] FAILED {response.status_code}: {response.text}")
        return None
    return response.json()["messages"][0]["id"]


def send_text(to: str, body: str) -> str | None:
    """Send a plain text message. Returns WhatsApp's message ID, or None if it failed."""
    return _post_message({
        "messaging_product": "whatsapp",
        "to": to,                 # e.g. "919876543210" (country code, no +)
        "type": "text",
        "text": {"body": body},
    })


def send_buttons(to: str, body: str, buttons: list[dict]) -> str | None:
    """
    Send a text with up to 3 tap-able reply buttons.
    `buttons` is a list like [{"id": "undo:3", "title": "ರದ್ದು ಮಾಡಿ"}].
    WhatsApp limits: body 1024 characters, title 20 characters, 3 buttons
    (replies.py and main.py keep us inside those limits).
    When the owner taps one, Meta sends us an "interactive" message that
    carries the same id back.
    """
    return _post_message({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons
            ]},
        },
    })


def download_media(media_id: str) -> tuple[bytes, str]:
    """
    Download a voice note. Returns (the file's bytes, its mime type such as
    'audio/ogg; codecs=opus'). Raises MediaError if anything goes wrong.
    """
    if config.SIMULATE:
        if not media_id.startswith("sim:"):
            raise MediaError("In practice mode only simulated voice notes ('sim:<path>') can be read")
        path = Path(media_id[len("sim:"):])
        if not path.is_file():
            raise MediaError(f"Simulated voice note not found: {path}")
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "audio/ogg"

    # Step 1: ask Meta where the file is
    info = httpx.get(f"{MEDIA_URL}/{media_id}", headers=_headers(), timeout=15)
    if info.status_code != 200:
        raise MediaError(f"Meta refused the media lookup ({info.status_code}): {info.text[:200]}")
    details = info.json()
    url = details.get("url", "")
    if not url.startswith("https://"):
        raise MediaError("Meta's reply had no secure download link")

    # Step 2: download it (this link also needs our token)
    file = httpx.get(url, headers=_headers(), timeout=30)
    if file.status_code != 200:
        raise MediaError(f"The voice note download failed ({file.status_code})")
    return file.content, details.get("mime_type", "audio/ogg")
