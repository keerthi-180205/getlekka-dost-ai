"""
simulate.py - chat with your GetLekka server from the terminal, no WhatsApp needed.

How it works:
  1. You type a message, send a voice file, or tap a button.
  2. This script wraps it in the SAME JSON shape Meta sends, and POSTs it
     to your server's /webhook - so your server can't tell the difference.
  3. Your server saves its reply in dukaandost.db (practice mode skips Meta).
  4. This script reads the reply from the database and prints it.

Run it (server already running with SIMULATE=true in .env, in a second terminal,
from this folder):
    python simulate.py

What you can type:
    ಸುರೇಶ್ ಗೆ 650 ಸಾಲ        any text: sent as a text message
    /voice path/to/note.ogg   send a recorded voice note (.ogg .m4a .mp3 .wav ...)
    /tap 1                    press button number 1 of the LAST reply (e.g. Undo, an answer)
    exit                      quit

Replies with buttons are shown like:
    [1] ↩️ ರದ್ದು ಮಾಡಿ
    [2] ...
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

SERVER_URL = "http://127.0.0.1:8000/webhook"
DB_PATH = "dukaandost.db"
FAKE_PHONE = "919999999999"  # pretend shop owner's number


# ---------- reading the server's replies ----------

def latest_out_id() -> int:
    """ID of the newest reply already in the database (so we only show new ones)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE direction = 'out'"
        ).fetchone()
    return row[0]


def wait_for_replies(after_id: int, timeout: float = 120.0, quiet: float = 1.5) -> list[tuple[str, list[dict]]]:
    """
    Wait for the server's replies to FAKE_PHONE. Returns [(text, buttons), ...].

    A voice note can take a while (speech + AI), so we wait up to `timeout`
    seconds for the FIRST reply, then `quiet` seconds of silence so replies
    that arrive one after another are all shown.
    """
    deadline = time.time() + timeout
    replies: list[tuple[str, list[dict]]] = []
    last_change = None
    while time.time() < deadline:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT body, buttons FROM messages "
                "WHERE direction = 'out' AND phone = ? AND id > ? ORDER BY id",
                (FAKE_PHONE, after_id),
            ).fetchall()
        current = [(body, json.loads(buttons) if buttons else []) for body, buttons in rows]
        if len(current) != len(replies):
            replies, last_change = current, time.time()
        elif replies and time.time() - last_change >= quiet:
            return replies
        time.sleep(0.3)
    return replies


def format_reply(text: str, buttons: list[dict]) -> str:
    """The reply as printed in the terminal, with numbered buttons underneath."""
    lines = [text]
    lines += [f"    [{number}] {button['title']}" for number, button in enumerate(buttons, start=1)]
    return "\n".join(lines)


# ---------- building what Meta would send ----------

def _wrap(message: dict) -> dict:
    """Put one message inside Meta's nested envelope (entry -> changes -> value -> messages)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "SIMULATED",
            "changes": [{
                "field": "messages",
                "value": {"messaging_product": "whatsapp", "messages": [message]},
            }],
        }],
    }


def _base(msg_type: str) -> dict:
    return {
        "id": f"wamid.sim.{uuid.uuid4().hex}",
        "from": FAKE_PHONE,
        "timestamp": str(int(time.time())),
        "type": msg_type,
    }


def fake_meta_payload(text: str) -> dict:
    """The JSON Meta sends for an incoming text message."""
    return _wrap({**_base("text"), "text": {"body": text}})


def fake_voice_payload(path: str) -> dict:
    """
    The JSON Meta sends for a voice note. In practice mode the server reads the
    file straight from your disk when the media id looks like 'sim:<full path>'.
    """
    full_path = str(Path(path).expanduser().resolve())
    return _wrap({**_base("audio"), "audio": {"id": f"sim:{full_path}", "mime_type": "audio/ogg", "voice": True}})


def fake_button_payload(button_id: str, title: str = "") -> dict:
    """The JSON Meta sends when the owner taps a reply button."""
    return _wrap({**_base("interactive"), "interactive": {
        "type": "button_reply", "button_reply": {"id": button_id, "title": title}}})


# ---------- the chat loop ----------

def parse_command(line: str, last_buttons: list[dict]) -> tuple[str, dict | str | None]:
    """
    Turn what you typed into an action. Returns (kind, value):
        ("send", payload)   post this to the server
        ("error", message)  tell the user what was wrong
        ("skip", None)      nothing to do
    """
    line = line.strip()
    if not line:
        return "skip", None

    if line.startswith("/voice"):
        path = line[len("/voice"):].strip()
        if not path:
            return "error", "Usage: /voice path/to/note.ogg"
        if not Path(path).expanduser().is_file():
            return "error", f"File not found: {path}"
        return "send", fake_voice_payload(path)

    if line.startswith("/tap"):
        choice = line[len("/tap"):].strip()
        if not last_buttons:
            return "error", "There are no buttons to tap in the last reply."
        if not choice.isdigit() or not 1 <= int(choice) <= len(last_buttons):
            return "error", f"Usage: /tap N   (N from 1 to {len(last_buttons)})"
        button = last_buttons[int(choice) - 1]
        return "send", fake_button_payload(button["id"], button["title"])

    return "send", fake_meta_payload(line)


def main() -> None:
    print("GetLekka simulator.")
    print("Type a message, '/voice file.ogg' to send a voice note, '/tap 1' to press a button, or 'exit'.\n")
    last_buttons: list[dict] = []
    while True:
        line = input("You: ").strip()
        if line.lower() in {"exit", "quit"}:
            break

        kind, value = parse_command(line, last_buttons)
        if kind == "skip":
            continue
        if kind == "error":
            print(f"!! {value}\n")
            continue

        before = latest_out_id()
        try:
            httpx.post(SERVER_URL, json=value, timeout=10)
        except httpx.ConnectError:
            print("!! Server not running. Start it first: uvicorn app.main:app --reload --port 8000\n")
            continue

        replies = wait_for_replies(before)
        if not replies:
            print("GetLekka: (no reply - check the server terminal for errors)\n")
            continue
        for text, buttons in replies:
            print(f"GetLekka: {format_reply(text, buttons)}\n")
        last_buttons = replies[-1][1] or last_buttons   # /tap works on the newest buttons shown


if __name__ == "__main__":
    main()
