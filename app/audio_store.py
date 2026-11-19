"""
audio_store.py - keeps each voice note on disk as proof of what the owner said.

Why we keep them: if a customer ever disputes an entry, the original voice note
is the evidence. The path is saved on the entry's batch (batches.audio_path).

Files go in the folder named by AUDIO_DIR in .env (default: "audio") and are
named after WhatsApp's own message id, so the same message never gets saved twice.

Privacy: these recordings contain customers' names and money amounts. The
folder is listed in .gitignore so it is never uploaded by accident. Before real
pilot shops use GetLekka, tell them the notes are kept and get their consent.
"""
import os
import re
from pathlib import Path

from app.speech import MAX_AUDIO_BYTES

# The audio types WhatsApp and phones produce, and the file ending each gets.
# speech.py only accepts files with a known ending, so an unknown type becomes
# ".bin" and is refused there with a clear message.
MIME_EXTENSIONS = {
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/webm": ".webm",
}


class AudioError(Exception):
    """A voice note could not be stored (clear message inside)."""


def audio_dir() -> Path:
    """The folder voice notes are saved in (from AUDIO_DIR, default 'audio')."""
    return Path(os.getenv("AUDIO_DIR", "audio"))


def save_audio(wa_msg_id: str, data: bytes, mime_type: str) -> str:
    """
    Save a downloaded voice note and return its path.

    wa_msg_id  WhatsApp's id for the message; used for the file name
    mime_type  e.g. 'audio/ogg; codecs=opus' (anything after ';' is ignored)
    """
    if not data:
        raise AudioError("The voice note was empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError(f"The voice note is too large ({len(data)} bytes)")

    extension = MIME_EXTENSIONS.get(mime_type.split(";")[0].strip().lower(), ".bin")
    # The id comes from outside, so keep only safe characters: no slashes or
    # dots that could point outside the audio folder.
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", wa_msg_id) or "unnamed"

    folder = audio_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name}{extension}"
    path.write_bytes(data)
    return str(path)
